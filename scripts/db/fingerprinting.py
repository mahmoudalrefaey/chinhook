"""Whether the index still matches the database.

A fingerprint per table is compared against the one stored with its indexed definition, so
a re-index happens when a table's shape or its contents change, and not otherwise.
"""

import hashlib
import json

from config import QDRANT_COLLECTION
from scripts.db.clients import qdrant
from scripts.db.introspection import get_table_names


def compute_table_fingerprint(conn, table_name: str) -> dict:
    """Compute fingerprint for a single table: row count + schema hash."""
    with conn.cursor() as cur:
        # Row count
        cur.execute(f'SELECT COUNT(*) FROM "{table_name}"')
        row_count = cur.fetchone()[0]

        # Schema hash (column definitions)
        cur.execute(
            """ SELECT column_name, data_type, is_nullable, column_default
             FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = %s
             ORDER BY ordinal_position """,
            (table_name,),
        )
        cols = cur.fetchall()
        schema_str = json.dumps(cols, sort_keys=True)
        schema_hash = hashlib.sha256(schema_str.encode()).hexdigest()[:16]

        # Data checksum (sample-based for large tables)
        if row_count > 10000:
            cur.execute(f'SELECT * FROM "{table_name}" TABLESAMPLE SYSTEM (10) LIMIT 1000')
        else:
            cur.execute(f'SELECT * FROM "{table_name}"')
        rows = cur.fetchall()
        data_str = json.dumps(rows, sort_keys=True, default=str)
        data_hash = hashlib.sha256(data_str.encode()).hexdigest()[:16]

    return {
        "table_name": table_name,
        "row_count": row_count,
        "schema_hash": schema_hash,
        "data_hash": data_hash,
    }


def get_db_fingerprint(conn) -> dict:
    """Get fingerprint for all tables in the database."""
    table_names = get_table_names(conn)
    fingerprint = {}
    for table_name in table_names:
        fingerprint[table_name] = compute_table_fingerprint(conn, table_name)
    return fingerprint


def get_qdrant_fingerprint() -> dict:
    """Extract stored fingerprint from Qdrant payload."""
    fingerprint = {}
    try:
        # Scroll through all points to get payload metadata
        offset = None
        while True:
            result = qdrant.scroll(
                collection_name=QDRANT_COLLECTION,
                limit=100,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            points, offset = result
            for point in points:
                payload = point.payload
                if "fingerprint" in payload:
                    fp = payload["fingerprint"]
                    fingerprint[fp["table_name"]] = fp
            if offset is None:
                break
    except Exception:
        pass
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
