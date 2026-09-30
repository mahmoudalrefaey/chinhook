"""Writing the schema into the vector store, and keeping it in step with the database.

Every function here works on the current session's database and writes only to that
database's own pair of collections (see scripts/db/clients.py), so indexing one database can
never touch another's index.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Callable, Optional

from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchAny,
    PointStruct,
    VectorParams,
)

from scripts.db.clients import (
    embed_batch,
    embedding_size,
    get_connection,
    qdrant,
    tables_collection,
    values_collection,
)
from scripts.db.evidence import generate_evidence, get_sample_rows
from scripts.db.fingerprinting import (
    compare_fingerprints,
    get_db_fingerprint,
    get_qdrant_fingerprint,
)
from scripts.db.introspection import get_catalog, get_foreign_keys, get_table_defs
from scripts.db.values import low_cardinality_values

# Both caps exist for the same reason: a schema-wide batch of embeddings or points can be
# larger than is sensible to hold or send in one request. Chunking is what lets a table or a
# column with a genuinely large number of distinct values still index correctly.
_EMBED_BATCH_SIZE = 200
_UPSERT_BATCH_SIZE = 200

# Called as progress(done, total, message) while indexing runs, so the interface can show how
# far along it is. Optional everywhere.
Progress = Optional[Callable[[int, int, str], None]]


def _report(progress: Progress, done: int, total: int, message: str) -> None:
    if progress is not None:
        progress(done, total, message)


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
    """Create this database's two Qdrant collections if they do not exist yet.

    A collection that exists at a different vector size than the embedding model now produces
    is dropped and recreated rather than left as a silent mismatch. The model's name is part
    of the collection's own name, so this only happens if a collection was damaged or built by
    hand, but Qdrant would otherwise reject every vector the next reindex tried to write.
    """
    size = embedding_size()
    for name in (tables_collection(), values_collection()):
        if qdrant.collection_exists(name):
            current_size = qdrant.get_collection(name).config.params.vectors.size
            if current_size == size:
                continue
            print(
                f'Collection "{name}" holds {current_size}-dimensional vectors but the '
                f"embedding model produces {size}; recreating it."
            )
            qdrant.delete_collection(name)
        qdrant.create_collection(
            name, vectors_config=VectorParams(size=size, distance=Distance.COSINE)
        )


def stable_point_id(*parts: str) -> str:
    """Deterministic point id, stable across process restarts.

    Python's built-in hash() is randomized per process, so it cannot be used: the same table
    would get a new id on every run, and every re-index would insert a duplicate instead of
    updating the point already there. A full 128 bits of a SHA-256, as a UUID, rather than a
    short integer: the values collection holds one point per distinct value, and a short id
    collides often enough at that scale for one value to silently overwrite another. Several
    parts are joined by a separator a real identifier cannot contain, so a table point and a
    (table, column, value) point never collide either.
    """
    joined = "\x1f".join(parts)
    return str(uuid.UUID(bytes=hashlib.sha256(joined.encode()).digest()[:16]))


def _table_name_from_def(table_def: str) -> str:
    return table_def.split('"')[1].split('"')[0]


def _index_tables(conn, table_defs: list[str], fingerprints: dict, progress: Progress = None) -> int:
    """Embed and upsert one point per table, enriched with evidence and its join neighbours."""
    foreign_keys = get_foreign_keys(conn)
    enriched: list[tuple[str, dict]] = []
    total = len(table_defs)
    for done, table_def in enumerate(table_defs):
        table_name = _table_name_from_def(table_def)
        _report(progress, done, total, f"Describing {table_name}")
        columns, sample_rows = get_sample_rows(conn, table_name)
        evidence = generate_evidence(table_def, columns, sample_rows)
        enriched_def = f"{table_def}\n  Represents: {evidence}" if evidence else table_def
        fingerprint = fingerprints.get(table_name, {"table_name": table_name})
        enriched.append((enriched_def, fingerprint))

    if not enriched:
        return 0

    _report(progress, total, total, "Embedding table descriptions")
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
    _upsert_all(tables_collection(), points)
    return len(points)


def _table_filter(table_names: list[str]) -> Filter:
    return Filter(must=[FieldCondition(key="table", match=MatchAny(any=table_names))])


def _value_point_ids_for(table_names: list[str]) -> list:
    ids = []
    offset = None
    while True:
        found, offset = qdrant.scroll(
            collection_name=values_collection(),
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


def _index_values(conn, table_names: list[str], fingerprints: dict, progress: Progress = None) -> int:
    """Embed and upsert one point per (table, column, value) worth searching by value.

    Replaces every point this run previously wrote for these tables first: a value dropped
    from the data, or a column that no longer qualifies as low cardinality, has to stop being
    offered as a match, and upserting alone would only ever add or update, never remove.
    """
    if not table_names:
        return 0
    stale_ids = _value_point_ids_for(table_names)
    if stale_ids:
        qdrant.delete(values_collection(), stale_ids)

    catalog = get_catalog(conn)
    entries: list[tuple[str, str, str]] = []
    for done, table_name in enumerate(table_names):
        _report(progress, done, len(table_names), f"Reading values of {table_name}")
        row_estimate = int((fingerprints.get(table_name) or {}).get("row_estimate") or 0)
        found = low_cardinality_values(conn, table_name, catalog.get(table_name, []), row_estimate)
        for column, values in found.items():
            entries.extend((table_name, column, value) for value in values)

    if not entries:
        return 0

    _report(progress, len(table_names), len(table_names), f"Embedding {len(entries)} values")
    vectors = _embed_all([value for _t, _c, value in entries])
    points = [
        PointStruct(
            id=stable_point_id(table, column, value),
            vector=vector,
            payload={"table": table, "column": column, "value": value},
        )
        for (table, column, value), vector in zip(entries, vectors, strict=True)
    ]
    _upsert_all(values_collection(), points)
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
            collection_name=tables_collection(),
            limit=200,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        for point in points:
            if str(point.id) not in valid_ids:
                stale_ids.append(point.id)
        if offset is None:
            break
    if stale_ids:
        qdrant.delete(tables_collection(), stale_ids)
    return len(stale_ids)


def full_reindex(progress: Progress = None) -> int:
    """Full re-index of every table, and removal of anything indexed that no longer exists."""
    ensure_collections()
    with get_connection() as conn:
        _report(progress, 0, 1, "Reading the schema")
        table_defs = get_table_defs(conn)
        table_names = {_table_name_from_def(td) for td in table_defs}
        fingerprints = get_db_fingerprint(conn)
        indexed = _index_tables(conn, table_defs, fingerprints, progress)
        _index_values(conn, sorted(table_names), fingerprints, progress)

    removed = _prune_stale_tables(table_names)
    _report(progress, 1, 1, "Index ready")
    print(f"Full re-index complete: {indexed} table(s) indexed, {removed} stale point(s) removed")
    return indexed


def incremental_reindex(changed_tables: list[str], progress: Progress = None) -> int:
    """Re-index only the changed tables, deleting any that no longer exist."""
    ensure_collections()
    with get_connection() as conn:
        _report(progress, 0, 1, "Reading the schema")
        table_defs = get_table_defs(conn)
        defs_by_table = {_table_name_from_def(td): td for td in table_defs}

        still_here = [t for t in changed_tables if t in defs_by_table]
        dropped = [t for t in changed_tables if t not in defs_by_table]

        fingerprints = get_db_fingerprint(conn)
        indexed = _index_tables(conn, [defs_by_table[t] for t in still_here], fingerprints, progress)
        _index_values(conn, still_here, fingerprints, progress)

    if dropped:
        qdrant.delete(tables_collection(), [stable_point_id(name) for name in dropped])
        _prune_stale_values(dropped)

    _report(progress, 1, 1, "Index ready")
    print(f"Incremental re-index complete: {indexed} table(s) updated, {len(dropped)} removed")
    return indexed


def _prune_stale_values(table_names: list[str]) -> None:
    """Remove every value point belonging to tables that no longer exist."""
    stale_ids = _value_point_ids_for(table_names)
    if stale_ids:
        qdrant.delete(values_collection(), stale_ids)


def check_and_index(progress: Progress = None) -> int:
    """Bring the index in step with the database, doing only as much work as that needs.

    Nothing indexed yet builds everything; a few tables changed re-indexes just those; nothing
    changed does nothing at all beyond one catalog read. A collection that cannot currently be
    read counts as neither "up to date" nor "empty": get_qdrant_fingerprint raises rather than
    returning {} for that case, so a Qdrant hiccup is reported rather than mistaken for an
    empty index and answered with a full rebuild.
    """
    with get_connection() as conn:
        db_fp = get_db_fingerprint(conn)

    qdrant_fp = get_qdrant_fingerprint()
    needs_reindex, changed = compare_fingerprints(db_fp, qdrant_fp)

    if not needs_reindex:
        _report(progress, 1, 1, "Index is up to date")
        return 0

    if not qdrant_fp:
        return full_reindex(progress)

    return incremental_reindex(changed, progress)


def get_index_status() -> dict:
    """What is indexed compared with what the database has, without changing anything.

    A search index that cannot currently be reached is reported as such rather than treated
    as an empty one: the two call for different reactions.
    """
    with get_connection() as conn:
        db_fp = get_db_fingerprint(conn)

    try:
        qdrant_fp = get_qdrant_fingerprint()
        reachable = True
    except Exception:  # noqa: BLE001
        qdrant_fp = {}
        reachable = False

    needs_reindex, changed = compare_fingerprints(db_fp, qdrant_fp) if reachable else (False, [])

    return {
        "db_tables": len(db_fp),
        "qdrant_tables": len(qdrant_fp),
        "needs_reindex": needs_reindex,
        "changed_tables": changed,
        "index_reachable": reachable,
    }


def delete_index() -> None:
    """Remove this database's index entirely."""
    for name in (tables_collection(), values_collection()):
        if qdrant.collection_exists(name):
            qdrant.delete_collection(name)
