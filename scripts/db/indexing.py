"""Writing the schema into the vector store, and keeping it in step with the database."""

import hashlib

from qdrant_client.models import Distance, PointStruct, VectorParams

from config import EMBED_DIM, QDRANT_COLLECTION
from scripts.db.clients import embed, get_connection, qdrant
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


def _table_name_from_def(table_def: str) -> str:
    return table_def.split('"')[1].split('"')[0]


def index_table(conn, table_def: str, fingerprint: dict) -> PointStruct:
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


def _prune_stale_points(valid_table_names: set[str]) -> int:
    """Remove any indexed point whose table is not one of the ones just indexed.

    A full re-index only ever upserts the tables it finds. Without this, a table that was
    renamed or dropped keeps its old point in Qdrant forever, and the assistant goes on being
    offered a table that no longer exists. Comparing ids rather than names, since the id is
    what a delete call actually needs and what the point is keyed on.
    """
    valid_ids = {stable_point_id(name) for name in valid_table_names}
    stale_ids = []
    offset = None
    while True:
        points, offset = qdrant.scroll(
            collection_name=QDRANT_COLLECTION,
            limit=200,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        for point in points:
            if point.id not in valid_ids:
                stale_ids.append(point.id)
        if offset is None:
            break
    if stale_ids:
        qdrant.delete(QDRANT_COLLECTION, stale_ids)
    return len(stale_ids)


def full_reindex():
    """Full re-index of all tables, and removal of anything indexed that no longer exists."""
    ensure_collection()
    with get_connection() as conn:
        table_defs = get_table_defs(conn)
        db_fp = get_db_fingerprint(conn)

        points = []
        table_names = set()
        for table_def in table_defs:
            table_name = _table_name_from_def(table_def)
            table_names.add(table_name)
            fingerprint = db_fp.get(table_name, {"table_name": table_name})
            points.append(index_table(conn, table_def, fingerprint))

    if points:
        qdrant.upsert(QDRANT_COLLECTION, points)
    removed = _prune_stale_points(table_names)

    print(f"Full re-index complete: {len(points)} tables indexed, {removed} stale point(s) removed")
    return len(points)


def incremental_reindex(changed_tables: list[str]):
    """Re-index only the changed tables, deleting any that no longer exist."""
    ensure_collection()
    with get_connection() as conn:
        table_defs = get_table_defs(conn)
        db_fp = get_db_fingerprint(conn)

        defs_by_table = {_table_name_from_def(td): td for td in table_defs}

        points = []
        deleted = 0
        for table_name in changed_tables:
            if table_name in defs_by_table:
                fingerprint = db_fp.get(table_name, {"table_name": table_name})
                points.append(index_table(conn, defs_by_table[table_name], fingerprint))
            else:
                # Table was dropped - delete from Qdrant
                qdrant.delete(QDRANT_COLLECTION, [stable_point_id(table_name)])
                deleted += 1

    if points:
        qdrant.upsert(QDRANT_COLLECTION, points)
    print(f"Incremental re-index complete: {len(points)} table(s) updated, {deleted} removed")
    return len(points)


def check_and_index():
    """Check if re-indexing is needed and perform it.

    A collection that cannot currently be read counts as neither "up to date" nor "empty":
    get_qdrant_fingerprint raises rather than returning {} for that case, so a Qdrant hiccup
    here is left alone rather than mistaken for an empty index and answered with a full
    rebuild.
    """
    with get_connection() as conn:
        db_fp = get_db_fingerprint(conn)

    try:
        qdrant_fp = get_qdrant_fingerprint()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not read the search index to check it ({type(exc).__name__}: {exc}); leaving it as is.")
        return 0

    needs_reindex, changed = compare_fingerprints(db_fp, qdrant_fp)

    if not needs_reindex:
        print("Index is up to date. No changes detected.")
        return 0

    if not qdrant_fp:
        print("Full re-index needed")
        return full_reindex()

    print(f"Incremental re-index needed for: {changed}")
    return incremental_reindex(changed)
