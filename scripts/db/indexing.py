"""Writing the schema into the vector store, and keeping it in step with the database."""

import hashlib

from qdrant_client.models import Distance, PointStruct, VectorParams

from config import EMBED_DIM, QDRANT_COLLECTION
from scripts.db.clients import conn, embed, qdrant
from scripts.db.evidence import generate_evidence, get_sample_rows
from scripts.db.fingerprinting import (
    compare_fingerprints,
    get_db_fingerprint,
    get_qdrant_fingerprint,
)
from scripts.db.introspection import get_table_defs


def ensure_collection():
    """Create Qdrant collection if it doesn't exist."""
    if not qdrant.collection_exists(QDRANT_COLLECTION):
        qdrant.create_collection(
            QDRANT_COLLECTION,
            vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
        )


def stable_point_id(table_name: str) -> int:
    """Deterministic point id for a table, stable across process restarts.

    Python's built-in hash() is randomized per process (PYTHONHASHSEED), so the same table
    name produced a different id every run. That meant every re-index inserted a fresh
    duplicate of each table instead of updating the existing point, and retrieval quality
    degraded a little more with each restart as duplicates crowded out real variety.
    """
    return int(hashlib.sha256(table_name.encode()).hexdigest()[:8], 16)


def index_table(table_def: str, fingerprint: dict) -> PointStruct:
    """Create a point for a table with its fingerprint in payload.

    The stored table_def is enriched with automatically generated evidence about what the
    table represents, so both retrieval (which tables look relevant to a question) and the
    final SQL generation benefit from it, with no change needed in either of those places.
    """
    table_name = fingerprint["table_name"]
    point_id = stable_point_id(table_name)
    sample_rows = get_sample_rows(conn, table_name)
    evidence = generate_evidence(table_def, sample_rows)
    enriched_def = f"{table_def}\n  Represents: {evidence}" if evidence else table_def
    return PointStruct(
        id=point_id,
        vector=embed(enriched_def),
        payload={"table_def": enriched_def, "fingerprint": fingerprint},
    )


def full_reindex():
    """Full re-index of all tables."""
    ensure_collection()
    table_defs = get_table_defs(conn)
    db_fp = get_db_fingerprint(conn)

    points = []
    for table_def in table_defs:
        # Extract table name from definition
        table_name = table_def.split('"')[1].split('"')[0]
        fingerprint = db_fp.get(table_name, {"table_name": table_name})
        points.append(index_table(table_def, fingerprint))

    qdrant.upsert(QDRANT_COLLECTION, points)
    print(f"Full re-index complete: {len(points)} tables")
    return len(points)


def incremental_reindex(changed_tables: list[str]):
    """Re-index only the changed tables."""
    ensure_collection()
    table_defs = get_table_defs(conn)
    db_fp = get_db_fingerprint(conn)

    # Build lookup for table definitions
    defs_by_table = {}
    for td in table_defs:
        tn = td.split('"')[1].split('"')[0]
        defs_by_table[tn] = td

    points = []
    for table_name in changed_tables:
        if table_name in defs_by_table:
            fingerprint = db_fp.get(table_name, {"table_name": table_name})
            points.append(index_table(defs_by_table[table_name], fingerprint))
        else:
            # Table was dropped - delete from Qdrant
            point_id = stable_point_id(table_name)
            qdrant.delete(QDRANT_COLLECTION, [point_id])

    if points:
        qdrant.upsert(QDRANT_COLLECTION, points)
        print(f"Incremental re-index complete: {len(points)} tables updated")
    return len(points)


def check_and_index():
    """Check if re-indexing is needed and perform it."""
    db_fp = get_db_fingerprint(conn)
    qdrant_fp = get_qdrant_fingerprint()

    needs_reindex, changed = compare_fingerprints(db_fp, qdrant_fp)

    if not needs_reindex:
        print("Index is up to date. No changes detected.")
        return 0

    if len(changed) == len(db_fp) or not qdrant_fp:
        print("Full re-index needed")
        return full_reindex()
    else:
        print(f"Incremental re-index needed for: {changed}")
        return incremental_reindex(changed)
