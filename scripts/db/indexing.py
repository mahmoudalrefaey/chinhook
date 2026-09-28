"""Writing the schema into the vector store, and keeping it in step with the database."""

import hashlib

from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchAny,
    PointStruct,
    VectorParams,
)

from config import EMBED_DIM, QDRANT_COLLECTION_TABLES, QDRANT_COLLECTION_VALUES
from scripts.db.clients import embed_batch, get_connection, qdrant
from scripts.db.evidence import generate_evidence, get_sample_rows
from scripts.db.fingerprinting import (
    compare_fingerprints,
    get_db_fingerprint,
    get_qdrant_fingerprint,
)
from scripts.db.introspection import get_foreign_keys, get_table_defs
from scripts.db.values import low_cardinality_values

# Both caps exist for the same reason: a schema-wide batch of embeddings or points can be
# larger than either Azure or Qdrant is willing to accept in one request. Chunking is what
# lets a table or a column with a genuinely large number of distinct values still index
# correctly instead of failing the whole run over one oversized call.
_EMBED_BATCH_SIZE = 200
_UPSERT_BATCH_SIZE = 200


def _chunks(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _embed_all(texts: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    for chunk in _chunks(texts, _EMBED_BATCH_SIZE):
        vectors.extend(embed_batch(chunk))
    return vectors


def _upsert_all(collection: str, points: list[PointStruct]) -> None:
    for chunk in _chunks(points, _UPSERT_BATCH_SIZE):
        qdrant.upsert(collection, chunk)


def ensure_collections():
    """Create both Qdrant collections if they do not exist yet, at the configured vector size.

    A collection that already exists but was built for a different embedding model, such as
    one indexed before EMBED_MODEL or EMBED_DIM changed, is dropped and recreated rather than
    left as a silent mismatch: Qdrant rejects every vector the new model produces against the
    old size, which otherwise turns "the embedding deployment changed" into every upsert in
    the next reindex failing with no clue why.
    """
    for name in (QDRANT_COLLECTION_TABLES, QDRANT_COLLECTION_VALUES):
        if qdrant.collection_exists(name):
            current_size = qdrant.get_collection(name).config.params.vectors.size
            if current_size == EMBED_DIM:
                continue
            print(
                f'Collection "{name}" holds {current_size}-dimensional vectors but the '
                f"configured embedding produces {EMBED_DIM}; recreating it."
            )
            qdrant.delete_collection(name)
        qdrant.create_collection(
            name, vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE)
        )


def stable_point_id(*parts: str) -> int:
    """Deterministic point id, stable across process restarts.

    Python's built-in hash() is randomized per process (PYTHONHASHSEED), so the same input
    produced a different id every run. That meant every re-index inserted a fresh duplicate
    of each point instead of updating the existing one, and retrieval quality degraded a
    little more with each restart as duplicates crowded out real variety. Several parts are
    accepted, joined by a separator a real identifier cannot contain, so a table point and a
    (table, column, value) point are never at risk of colliding on the same id.
    """
    joined = "\x1f".join(parts)
    return int(hashlib.sha256(joined.encode()).hexdigest()[:8], 16)


def _table_name_from_def(table_def: str) -> str:
    return table_def.split('"')[1].split('"')[0]


def _index_tables(conn, table_defs: list[str], fingerprints: dict) -> int:
    """Embed and upsert one point per table, enriched with evidence and its join neighbours."""
    foreign_keys = get_foreign_keys(conn)
    enriched: list[tuple[str, dict]] = []
    for table_def in table_defs:
        table_name = _table_name_from_def(table_def)
        columns, sample_rows = get_sample_rows(conn, table_name)
        evidence = generate_evidence(table_def, columns, sample_rows)
        enriched_def = f"{table_def}\n  Represents: {evidence}" if evidence else table_def
        fingerprint = fingerprints.get(table_name, {"table_name": table_name})
        enriched.append((enriched_def, fingerprint))

    if not enriched:
        return 0

    vectors = _embed_all([text for text, _ in enriched])
    points = [
        PointStruct(
            id=stable_point_id(fingerprint["table_name"]),
            vector=vector,
            payload={
                "table_def": text,
                "fingerprint": fingerprint,
                "fk_neighbours": sorted(foreign_keys.get(fingerprint["table_name"], set())),
            },
        )
        for (text, fingerprint), vector in zip(enriched, vectors, strict=True)
    ]
    _upsert_all(QDRANT_COLLECTION_TABLES, points)
    return len(points)


def _table_filter(table_names: list[str]) -> Filter:
    return Filter(must=[FieldCondition(key="table", match=MatchAny(any=table_names))])


def _value_point_ids_for(table_names: list[str]) -> list:
    ids = []
    offset = None
    while True:
        found, offset = qdrant.scroll(
            collection_name=QDRANT_COLLECTION_VALUES,
            scroll_filter=_table_filter(table_names),
            limit=200,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        ids.extend(point.id for point in found)
        if offset is None:
            break
    return ids


def _index_values(conn, table_names: list[str]) -> int:
    """Embed and upsert one point per (table, column, value) worth searching by value.

    Replaces every point this run previously wrote for these tables first: a value dropped
    from the data, or a column that no longer qualifies as low cardinality, has to stop being
    offered as a match, and upserting alone would only ever add or update, never remove.
    """
    if not table_names:
        return 0
    stale_ids = _value_point_ids_for(table_names)
    if stale_ids:
        qdrant.delete(QDRANT_COLLECTION_VALUES, stale_ids)

    entries: list[tuple[str, str, str]] = []
    for table_name in table_names:
        for column, values in low_cardinality_values(conn, table_name).items():
            entries.extend((table_name, column, value) for value in values)

    if not entries:
        return 0

    vectors = _embed_all([value for _t, _c, value in entries])
    points = [
        PointStruct(
            id=stable_point_id(table, column, value),
            vector=vector,
            payload={"table": table, "column": column, "value": value},
        )
        for (table, column, value), vector in zip(entries, vectors, strict=True)
    ]
    _upsert_all(QDRANT_COLLECTION_VALUES, points)
    return len(points)


def _prune_stale_tables(valid_table_names: set[str]) -> int:
    """Remove any indexed table point whose table is not one of the ones just indexed.

    A full re-index only ever upserts the tables it finds. Without this, a table that was
    renamed or dropped keeps its old point in Qdrant forever, and the assistant goes on being
    offered a table that no longer exists.
    """
    valid_ids = {stable_point_id(name) for name in valid_table_names}
    stale_ids = []
    offset = None
    while True:
        points, offset = qdrant.scroll(
            collection_name=QDRANT_COLLECTION_TABLES,
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
        qdrant.delete(QDRANT_COLLECTION_TABLES, stale_ids)
    return len(stale_ids)


def full_reindex():
    """Full re-index of every table, and removal of anything indexed that no longer exists."""
    ensure_collections()
    with get_connection() as conn:
        table_defs = get_table_defs(conn)
        fingerprints = get_db_fingerprint(conn)
        table_names = {_table_name_from_def(td) for td in table_defs}

        indexed = _index_tables(conn, table_defs, fingerprints)
        _index_values(conn, sorted(table_names))

    removed = _prune_stale_tables(table_names)
    print(f"Full re-index complete: {indexed} table(s) indexed, {removed} stale point(s) removed")
    return indexed


def incremental_reindex(changed_tables: list[str]):
    """Re-index only the changed tables, deleting any that no longer exist."""
    ensure_collections()
    with get_connection() as conn:
        table_defs = get_table_defs(conn)
        fingerprints = get_db_fingerprint(conn)
        defs_by_table = {_table_name_from_def(td): td for td in table_defs}

        still_here = [t for t in changed_tables if t in defs_by_table]
        dropped = [t for t in changed_tables if t not in defs_by_table]

        indexed = _index_tables(conn, [defs_by_table[t] for t in still_here], fingerprints)
        _index_values(conn, still_here)

    for table_name in dropped:
        qdrant.delete(QDRANT_COLLECTION_TABLES, [stable_point_id(table_name)])
    if dropped:
        _prune_stale_values(dropped)

    print(f"Incremental re-index complete: {indexed} table(s) updated, {len(dropped)} removed")
    return indexed


def _prune_stale_values(table_names: list[str]) -> None:
    """Remove every value point belonging to tables that no longer exist."""
    stale_ids = _value_point_ids_for(table_names)
    if stale_ids:
        qdrant.delete(QDRANT_COLLECTION_VALUES, stale_ids)


def _embedding_config_changed() -> bool:
    """Whether an already-indexed collection was built for a different embedding model.

    Checked separately from the schema fingerprint, which only ever reflects the database's
    own shape: the database can be completely unchanged while EMBED_MODEL or EMBED_DIM is
    reconfigured to a different deployment, and that case has to force a reindex on its own,
    not wait for a schema fingerprint that was never going to change to notice it.
    """
    if not qdrant.collection_exists(QDRANT_COLLECTION_TABLES):
        return False
    current_size = qdrant.get_collection(QDRANT_COLLECTION_TABLES).config.params.vectors.size
    return current_size != EMBED_DIM


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
        if _embedding_config_changed():
            print("The embedding deployment has changed since this was last indexed; full re-index needed")
            return full_reindex()
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
