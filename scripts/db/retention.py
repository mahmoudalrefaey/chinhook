"""How long an index is kept after the last time anyone used it.

An index holds things taken from a user's own database: table definitions, a sentence
describing each table, and real values from low-cardinality columns. Keeping it means a
reconnect is instant instead of paying for indexing again; keeping it forever means this
server holds that data long after anyone needed it. So each index records when it was last
used, and one unused for INDEX_RETENTION_DAYS is deleted.

Qdrant keeps no timestamp of its own for a collection, so a small registry collection holds
one point per index: {"tenant": ..., "last_used": epoch seconds}. Its vectors are a single
constant number, since nothing ever searches it by similarity.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
import uuid

from qdrant_client.models import Distance, PointStruct, VectorParams

import config
from scripts.db.clients import qdrant

_TOUCH_INTERVAL_SECONDS = 3600
_PURGE_INTERVAL_SECONDS = 3600

_touched: dict[str, float] = {}
_purge_lock = threading.Lock()
_last_purge = 0.0


def _registry() -> str:
    return f"{config.QDRANT_COLLECTION_PREFIX}__registry"


def _collections_of(tenant: str) -> tuple[str, str]:
    base = f"{config.QDRANT_COLLECTION_PREFIX}_{tenant}"
    return f"{base}_tables", f"{base}_values"


def _point_id(tenant: str) -> str:
    return str(uuid.UUID(bytes=hashlib.sha256(tenant.encode("utf-8")).digest()[:16]))


def _ensure_registry() -> None:
    if not qdrant.collection_exists(_registry()):
        qdrant.create_collection(_registry(), vectors_config=VectorParams(size=1, distance=Distance.DOT))


def touch(tenant: str) -> None:
    """Record that this tenant's index is in use right now."""
    _ensure_registry()
    now = time.time()
    qdrant.upsert(
        _registry(),
        [PointStruct(id=_point_id(tenant), vector=[1.0], payload={"tenant": tenant, "last_used": now})],
    )
    _touched[tenant] = now


def touch_if_stale(tenant: str) -> None:
    """touch(), but at most once an hour per tenant: cheap enough to call on every question."""
    if time.time() - _touched.get(tenant, 0.0) >= _TOUCH_INTERVAL_SECONDS:
        touch(tenant)


def forget(tenant: str) -> None:
    """Remove this tenant from the registry, once its collections are gone."""
    if qdrant.collection_exists(_registry()):
        qdrant.delete(_registry(), [_point_id(tenant)])
    _touched.pop(tenant, None)


def purge_expired(now: float | None = None) -> list[str]:
    """Delete every index unused for longer than INDEX_RETENTION_DAYS. Returns their tenants.

    Also deletes collections with this app's naming but no registry entry at all, which is
    what a crash between creating collections and recording them would leave behind. Only
    names exactly of the form this app creates are ever touched, so other collections in a
    shared Qdrant are safe. INDEX_RETENTION_DAYS of 0 or less keeps everything.
    """
    if config.INDEX_RETENTION_DAYS <= 0:
        return []
    now = time.time() if now is None else now
    cutoff = now - config.INDEX_RETENTION_DAYS * 86400

    # Collections are listed before the registry is read. An index being created right now
    # is registered before its collections exist (see chat_engine.prepare_index), so any
    # collection seen here already has its registry entry by the time that is read below;
    # read the other way round, a brand-new index could be mistaken for an orphan.
    pattern = re.compile(rf"^{re.escape(config.QDRANT_COLLECTION_PREFIX)}_([0-9a-f]{{16}})_(tables|values)$")
    present: set[str] = set()
    for collection in qdrant.get_collections().collections:
        match = pattern.match(collection.name)
        if match:
            present.add(match.group(1))

    registered: dict[str, float] = {}
    if qdrant.collection_exists(_registry()):
        offset = None
        while True:
            points, offset = qdrant.scroll(
                _registry(), limit=200, offset=offset, with_payload=True, with_vectors=False
            )
            for point in points:
                tenant = (point.payload or {}).get("tenant")
                if tenant:
                    registered[tenant] = float((point.payload or {}).get("last_used") or 0.0)
            if offset is None:
                break

    expired = [tenant for tenant, last_used in registered.items() if last_used < cutoff]
    orphaned = [tenant for tenant in present if tenant not in registered]
    for tenant in expired + orphaned:
        for name in _collections_of(tenant):
            if qdrant.collection_exists(name):
                qdrant.delete_collection(name)
        forget(tenant)
    if expired or orphaned:
        print(f"Index retention: removed {len(expired)} expired and {len(orphaned)} unregistered index(es)")
    return expired + orphaned


def maybe_purge() -> None:
    """purge_expired(), at most once an hour per process, and never twice at once."""
    global _last_purge
    if time.time() - _last_purge < _PURGE_INTERVAL_SECONDS:
        return
    if not _purge_lock.acquire(blocking=False):
        return
    try:
        _last_purge = time.time()
        purge_expired()
    except Exception as exc:  # noqa: BLE001
        # Retention is housekeeping: failing it must never stop anyone connecting.
        print(f"Index retention check failed: {type(exc).__name__}: {exc}")
    finally:
        _purge_lock.release()
