"""Whether the index still matches the database's shape.

A fingerprint per table is compared against the one stored with its indexed definition, so
a re-index happens when a table's columns or its foreign keys change, and not otherwise.

Deliberately shape-only, not content-only: on a database sized in the millions of rows,
hashing every row of every table on every check would itself be the expensive operation this
exists to avoid. A column list and a foreign key list are what actually distinguish "this
table means something different now" from "someone inserted more rows into it", which is the
distinction the description and the join graph in the index care about. Row count is kept as
a coarse extra signal, read from the planner's own statistics rather than counted directly.
"""

import hashlib
import json

from chinhook.db.clients import qdrant, tables_collection
from chinhook.db.introspection import get_columns, get_foreign_keys, get_row_estimates, get_tables


def compute_table_fingerprint(
    table_name: str,
    columns: list[tuple[str, str, str, str]],
    foreign_keys: dict[str, set[str]],
    row_estimate: int,
    kind: str = "table",
) -> dict:
    """Column shape, joined tables and an approximate row count for one table."""
    joined = sorted(foreign_keys.get(table_name, set()))
    shape_hash = hashlib.sha256(
        json.dumps(
            {"columns": [list(c) for c in columns], "joined": joined, "kind": kind},
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()[:16]

    return {
        "table_name": table_name,
        "kind": kind,
        "row_estimate": row_estimate,
        "shape_hash": shape_hash,
    }


def get_db_fingerprint(conn) -> dict:
    """Fingerprint for every table and view in the schema, keyed by name."""
    tables = get_tables(conn)
    columns = get_columns(conn)
    foreign_keys = get_foreign_keys(conn)
    estimates = get_row_estimates(conn, tables=tables)
    return {
        name: compute_table_fingerprint(
            name, columns.get(name, []), foreign_keys, estimates.get(name, 0), kind
        )
        for name, kind in tables.items()
    }


def get_qdrant_fingerprint() -> dict:
    """The fingerprint stored with each indexed table, read back from Qdrant.

    Returns an empty dict only when the collection genuinely does not exist yet, meaning
    nothing has ever been indexed. A network problem or any other failure talking to Qdrant
    is raised rather than swallowed into the same empty result: "nothing indexed yet" means
    build the index, and a two-second Qdrant hiccup does not.
    """
    if not qdrant.collection_exists(tables_collection()):
        return {}

    fingerprint = {}
    offset = None
    while True:
        points, offset = qdrant.scroll(
            collection_name=tables_collection(),
            limit=100,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for point in points:
            fp = point.payload.get("fingerprint")
            if fp:
                fingerprint[fp["table_name"]] = fp
        if offset is None:
            break
    return fingerprint


def compare_fingerprints(db_fp: dict, qdrant_fp: dict) -> tuple[bool, list[str]]:
    """Compare fingerprints. Returns (needs_reindex, changed_tables)."""
    changed = []
    all_tables = set(db_fp.keys()) | set(qdrant_fp.keys())

    for table in sorted(all_tables):
        db_entry = db_fp.get(table)
        qdrant_entry = qdrant_fp.get(table)

        if db_entry is None:
            changed.append(table)  # Table dropped
        elif qdrant_entry is None:
            changed.append(table)  # New table
        elif db_entry["shape_hash"] != qdrant_entry.get("shape_hash"):
            changed.append(table)  # Columns or foreign keys changed

    return len(changed) > 0, changed
