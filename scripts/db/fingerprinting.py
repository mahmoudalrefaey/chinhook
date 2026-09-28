"""Whether the index still matches the database's shape.

A fingerprint per table is compared against the one stored with its indexed definition, so
a re-index happens when a table's columns or its foreign keys change, and not otherwise.

Deliberately shape-only, not content-only: on a database sized in the millions of rows,
hashing every row of every table on every check would itself be the expensive operation this
exists to avoid. A schema this size changes by migration, not by an UPDATE somewhere inside
its data, so a column list and a foreign key list are what actually distinguish "this table
means something different now" from "someone inserted more rows into it", which is the
distinction the description and the join graph in the index care about. Row count is kept as
a coarse extra signal, read from the planner's own statistics rather than counted directly,
so it costs nothing beyond a catalog lookup even on a table nobody has ever run COUNT(*)
against.
"""

import hashlib
import json

from config import QDRANT_COLLECTION_TABLES
from scripts.db.clients import qdrant
from scripts.db.introspection import get_foreign_keys, get_table_names


def compute_table_fingerprint(
    conn, table_name: str, foreign_keys: dict[str, set[str]], schema_name: str = "public"
) -> dict:
    """Column shape, joined tables and an approximate row count for one table."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT column_name, data_type, is_nullable, column_default
               FROM information_schema.columns
               WHERE table_schema = %s AND table_name = %s
               ORDER BY ordinal_position""",
            (schema_name, table_name),
        )
        cols = cur.fetchall()

        cur.execute(
            "SELECT reltuples FROM pg_class WHERE oid = %s::regclass",
            (f'"{schema_name}"."{table_name}"',),
        )
        row = cur.fetchone()
        # -1 means the planner has never analysed this table; 0 is the more honest "unknown
        # yet" starting point for a table that has never been counted at all, and it self
        # corrects the first time autovacuum or an explicit ANALYZE runs.
        row_estimate = max(0, int(row[0])) if row and row[0] is not None else 0

    joined = sorted(foreign_keys.get(table_name, set()))
    shape_hash = hashlib.sha256(
        json.dumps({"columns": cols, "joined": joined}, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]

    return {
        "table_name": table_name,
        "row_estimate": row_estimate,
        "shape_hash": shape_hash,
    }


def get_db_fingerprint(conn) -> dict:
    """Fingerprint for every table in the database, keyed by table name."""
    table_names = get_table_names(conn)
    foreign_keys = get_foreign_keys(conn)
    return {
        name: compute_table_fingerprint(conn, name, foreign_keys) for name in table_names
    }


def get_qdrant_fingerprint() -> dict:
    """The fingerprint stored with each indexed table, read back from Qdrant.

    Returns an empty dict only when the collection genuinely does not exist yet, meaning
    nothing has ever been indexed. A network problem or any other failure talking to Qdrant
    is raised rather than swallowed into the same empty result: the two situations call for
    different responses. "Nothing indexed yet" means build the index. A two-second Qdrant
    hiccup does not, and treating it as though it did used to turn a brief blip into a full
    rebuild, complete with a fresh model call for every table.
    """
    if not qdrant.collection_exists(QDRANT_COLLECTION_TABLES):
        return {}

    fingerprint = {}
    offset = None
    while True:
        points, offset = qdrant.scroll(
            collection_name=QDRANT_COLLECTION_TABLES,
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

    for table in all_tables:
        db_entry = db_fp.get(table)
        qdrant_entry = qdrant_fp.get(table)

        if db_entry is None:
            changed.append(table)  # Table dropped
        elif qdrant_entry is None:
            changed.append(table)  # New table
        elif db_entry["shape_hash"] != qdrant_entry["shape_hash"]:
            changed.append(table)  # Columns or foreign keys changed

    return len(changed) > 0, changed
