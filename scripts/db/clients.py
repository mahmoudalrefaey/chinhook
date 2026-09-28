"""The clients the database tooling shares, and the pool that owns the Postgres connections.

Qdrant's client is created here but does not connect until first used, which is how the
client library already behaves. Postgres does not behave that way on its own, which is why
it gets a pool instead of a bare connection: a pool is created lazily on first use rather
than at import, connections are checked out for the duration of one operation and always
returned, and a browser session's own thread never blocks another session's on a shared,
half-finished transaction.

The pool connects with DATABASE_URL_RO, which points at a role with SELECT only wherever that
role has been created (see docs/READ_ONLY_ROLE.md). Falling back to DATABASE_URL when no
separate read-only role exists keeps an unconfigured deployment running, just without the
database's own guarantee that a write cannot happen even if every check in the application
layer has a gap.
"""

import atexit
import threading

from psycopg2 import pool as psycopg2_pool
from qdrant_client import QdrantClient

import config
from config import DATABASE_URL, DATABASE_URL_RO, QDRANT_URL

qdrant = QdrantClient(url=QDRANT_URL)

_pool_lock = threading.Lock()
_pool = None


def _create_pool():
    dsn = DATABASE_URL_RO or DATABASE_URL
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is not set. Nothing that talks to Postgres can run without it."
        )
    # SSL mode is whatever the URL itself says (sslmode=require for a hosted database,
    # sslmode=disable for a local one such as the CI Postgres service). Forcing "require"
    # here used to override that and refuse to connect to any database that does not speak
    # TLS, local development and CI included.
    return psycopg2_pool.ThreadedConnectionPool(1, 10, dsn)


def _get_pool():
    """The shared pool, created on first use.

    Double-checked locking rather than a bare check-then-create: two threads racing to
    create the pool at the same moment used to be able to each open their own connection and
    leave one leaked, which is the same shape of bug this replaces at the connection level.
    """
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = _create_pool()
                atexit.register(_pool.closeall)
    return _pool


class _CheckedOutConnection:
    """One connection, borrowed from the pool for the lifetime of a single `with` block.

    Always returned to the pool on the way out, success or failure, so a caller that raises
    partway through a query cannot leak a connection the way the old single shared connection
    could leak a broken transaction. The connection is rolled back before it goes back to the
    pool: with a read-only role there is nothing to commit, and rolling back is what clears
    whatever transaction and session settings, such as a LOCAL statement_timeout, the caller
    used, so the next borrower starts clean.
    """

    def __enter__(self):
        self._pool = _get_pool()
        self._conn = self._pool.getconn()
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        try:
            self._conn.rollback()
        except Exception:  # noqa: BLE001
            # The connection itself is no longer usable. Tell the pool to drop it instead of
            # returning something broken to the next caller, who would otherwise inherit an
            # error that has nothing to do with their own query.
            try:
                self._pool.putconn(self._conn, close=True)
            except Exception:  # noqa: BLE001
                pass
            return
        self._pool.putconn(self._conn)


def get_connection():
    """A connection checked out from the shared pool, for use as `with get_connection() as conn:`.

    Nothing connects to Postgres just by importing this module or calling this function; the
    pool itself, and the one connection this call hands out, are both created only once
    something actually needs them.
    """
    return _CheckedOutConnection()


def embed(text: str) -> list[float]:
    return embed_batch([text])[0]


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embeddings for several texts in one Azure OpenAI call.

    Used at index time, where a table's description and dozens or hundreds of a column's
    distinct values all need embedding: one round trip for the whole batch rather than one
    per text is what keeps indexing a large schema from being dominated by network latency.
    A single query at ask time is just a batch of one.
    """
    if not texts:
        return []
    client = config.create_embedding_client()
    response = client.embeddings.create(input=texts, model=config.EMBED_MODEL)
    by_index = {item.index: item.embedding for item in response.data}
    return [by_index[i] for i in range(len(texts))]
