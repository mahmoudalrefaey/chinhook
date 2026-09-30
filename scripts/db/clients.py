"""The clients the database tooling shares: connection pools, Qdrant, and the embedder.

Every session connects to its own database, so there is one SQLAlchemy engine (and with it
one small connection pool) per database in use, created the first time a session asks for it
and dropped once enough other databases have been used since. The engine is chosen by the
current session's own settings (see runtime.py), so a query can never be sent down another
session's connection.

Every connection an engine opens is switched to read-only before anything else runs on it
(see scripts/db/dialects.py). That is the guarantee that holds even when a user connects with
a login that could write, and even if every check above it had a gap: the database itself
refuses the write.

Qdrant is the deployment's own, configured once in config.py. What differs per session is
which collections in it are read and written: one pair per database (see tables_collection).
"""

from __future__ import annotations

import contextlib
import hashlib
import threading
from collections import OrderedDict
from typing import Iterator

from qdrant_client import QdrantClient
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

import config
import runtime
from scripts.db import dialects

qdrant = QdrantClient(url=config.QDRANT_URL, api_key=config.QDRANT_API_KEY, timeout=30)

# ---------- relational database ----------

_MAX_ENGINES = 32
_engines: OrderedDict[str, Engine] = OrderedDict()
_engines_lock = threading.Lock()


def _engine_key(db: runtime.DatabaseSettings) -> str:
    rendered = db.url.render_as_string(hide_password=False)
    return hashlib.sha256(f"{rendered}\x1f{db.ssl_mode}".encode("utf-8")).hexdigest()


def create_engine_for(db: runtime.DatabaseSettings) -> Engine:
    """A new engine for these settings, every connection it opens set read-only."""
    dialect = dialects.for_settings(db)
    engine = create_engine(
        db.url,
        pool_size=2,
        # Several tasks of one question run at once, each with its own connection.
        max_overflow=6,
        pool_timeout=30,
        pool_pre_ping=True,
        pool_recycle=300,
        connect_args=dialect.connect_args(db),
    )
    event.listen(engine, "connect", lambda dbapi_conn, _record: dialect.configure_session(dbapi_conn))
    return engine


def engine() -> Engine:
    """The current session's engine, created on first use and reused after that.

    Least recently used engines are disposed once more than a handful of databases are in use
    at once, so a busy public deployment does not hold a pool open for every database anyone
    has ever connected to.
    """
    db = runtime.current().db
    key = _engine_key(db)
    with _engines_lock:
        found = _engines.get(key)
        if found is not None:
            _engines.move_to_end(key)
            return found
        created = create_engine_for(db)
        _engines[key] = created
        while len(_engines) > _MAX_ENGINES:
            _old_key, old = _engines.popitem(last=False)
            old.dispose()
        return created


def forget_engine(db: runtime.DatabaseSettings) -> None:
    """Drop the engine for these settings, when they turned out not to work."""
    with _engines_lock:
        found = _engines.pop(_engine_key(db), None)
    if found is not None:
        found.dispose()


@contextlib.contextmanager
def get_connection() -> Iterator:
    """A DB-API connection borrowed from the current session's pool, for one `with` block.

    Everything run on it happens inside one read-only transaction with a time limit (see
    Dialect.start). Always handed back on the way out, rolled back first so whatever transaction the caller
    left open, and any LOCAL setting it made, does not reach the next borrower. A connection
    that cannot even roll back is thrown away rather than returned broken.
    """
    conn = engine().raw_connection()
    try:
        with conn.cursor() as cur:
            dialect().start(cur, dialects.DEFAULT_TIMEOUT_MS, schema())
        yield conn
    finally:
        if conn.is_valid:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                conn.invalidate()
        conn.close()


def dialect() -> dialects.Dialect:
    """The SQL dialect of the current session's database."""
    return dialects.for_settings(runtime.current().db)


def schema() -> str:
    """The schema of the current session's database that questions are answered from."""
    return runtime.current().db.schema


# ---------- Qdrant collections ----------

def _collection_base() -> str:
    return f"{config.QDRANT_COLLECTION_PREFIX}_{runtime.current().tenant_id}"


def tables_collection() -> str:
    """This database's table definitions: one point per table."""
    return f"{_collection_base()}_tables"


def values_collection() -> str:
    """This database's low-cardinality values: one point per (table, column, value)."""
    return f"{_collection_base()}_values"


# ---------- embeddings ----------
# One model per process, loaded on first use and shared by every session: it holds no state
# about any one of them, and ONNX Runtime inference is safe to call from several threads.

_embedder = None
_embedder_lock = threading.Lock()


def _embedding_model():
    global _embedder
    if _embedder is None:
        with _embedder_lock:
            if _embedder is None:
                from fastembed import TextEmbedding

                _embedder = TextEmbedding(model_name=config.EMBED_MODEL, cache_dir=config.EMBED_CACHE_DIR)
    return _embedder


def embedding_size() -> int:
    """The vector size the configured embedding model produces."""
    from fastembed import TextEmbedding

    return TextEmbedding.get_embedding_size(config.EMBED_MODEL)


def embed(text: str) -> list[float]:
    """The embedding of a search query: a question, or one word of one."""
    return next(iter(_embedding_model().query_embed(text))).tolist()


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embeddings for several documents being indexed, computed as one batch."""
    if not texts:
        return []
    return [vector.tolist() for vector in _embedding_model().passage_embed(texts, batch_size=64)]
