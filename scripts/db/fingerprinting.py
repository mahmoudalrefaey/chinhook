"""Whether the index still matches the database.

A fingerprint per table is compared against the one stored with its indexed definition, so
a re-index happens when a table's shape or its contents change, and not otherwise.
"""

import hashlib
import json

from psycopg2 import sql

from config import QDRANT_COLLECTION
from scripts.db.clients import qdrant
from scripts.db.introspection import get_table_names

_LARGE_TABLE_ROWS = 10000
_SAMPLE_ROWS = 1000


def compute_table_fingerprint(conn, table_name: str, schema_name: str = "public") -> dict:
    """Row count, schema shape and a content checksum for one table.

    The content checksum is computed inside Postgres rather than in Python: the row content
    is aggregated and hashed server side, with an explicit ORDER BY so the result does not
    depend on whatever physical order the engine happens to return rows in, and, for a large
    table, a REPEATABLE seed so the sample is the same sample every time this runs rather than
    a fresh random one. Without both of those, the fingerprint of a completely unchanged table
    could differ from one check to the next, which used to trigger a real re-index, model call
    and all, for no actual change.
    """
    table_ident = sql.Identifier(schema_name, table_name)
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(table_ident))
        row_count = cur.fetchone()[0]

        cur.execute(
            """SELECT column_name, data_type, is_nullable, column_default
               FROM information_schema.columns
               WHERE table_schema = %s AND table_name = %s
               ORDER BY ordinal_position""",
            (schema_name, table_name),
        )
        cols = cur.fetchall()
        schema_hash = hashlib.sha256(
            json.dumps(cols, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]

        if row_count > _LARGE_TABLE_ROWS:
            from_clause = sql.SQL("FROM {} AS t TABLESAMPLE SYSTEM (10) REPEATABLE (42)").format(
                table_ident
            )
            limit_clause = sql.SQL("LIMIT {}").format(sql.Literal(_SAMPLE_ROWS))
        else:
            from_clause = sql.SQL("FROM {} AS t").format(table_ident)
            limit_clause = sql.SQL("")
        cur.execute(
            sql.SQL(
                "SELECT md5(coalesce(string_agg(row_text, '' ORDER BY row_text), '')) "
                "FROM (SELECT (t.*)::text AS row_text {} {}) s"
            ).format(from_clause, limit_clause)
        )
        data_hash = cur.fetchone()[0]

    return {
        "table_name": table_name,
        "row_count": row_count,
        "schema_hash": schema_hash,
        "data_hash": data_hash,
    }


def get_db_fingerprint(conn) -> dict:
    """Fingerprint for every table in the database, keyed by table name."""
    table_names = get_table_names(conn)
    return {name: compute_table_fingerprint(conn, name) for name in table_names}


def get_qdrant_fingerprint() -> dict:
    """The fingerprint stored with each indexed table, read back from Qdrant.

    Returns an empty dict only when the collection genuinely does not exist yet, meaning
    nothing has ever been indexed. A network problem or any other failure talking to Qdrant
    is raised rather than swallowed into the same empty result: the two situations call for
    different responses. "Nothing indexed yet" means build the index. A two-second Qdrant
    hiccup does not, and treating it as though it did used to turn a brief blip into a full
    rebuild, complete with a fresh model call for every table.
    """
    if not qdrant.collection_exists(QDRANT_COLLECTION):
        return {}

    fingerprint = {}
    offset = None
    while True:
        points, offset = qdrant.scroll(
            collection_name=QDRANT_COLLECTION,
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
        elif db_entry["schema_hash"] != qdrant_entry["schema_hash"]:
            changed.append(table)  # Schema changed
        elif db_entry["data_hash"] != qdrant_entry["data_hash"]:
            changed.append(table)  # Data changed
        elif db_entry["row_count"] != qdrant_entry["row_count"]:
            changed.append(table)  # Row count changed

    return len(changed) > 0, changed
