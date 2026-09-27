"""The clients the database tooling shares, and the pool that owns the Postgres connections.

Ollama and Qdrant clients are created here but do not connect until first used, which is how
the client libraries for both already behave. Postgres does not behave that way on its own,
which is why it gets a pool instead of a bare connection: a pool is created lazily on first
use rather than at import, connections are checked out for the duration of one operation and
always returned, and a browser session's own thread never blocks another session's on a
shared, half-finished transaction.

The pool connects with DATABASE_URL_RO, which points at a role with SELECT only wherever that
role has been created (see docs/READ_ONLY_ROLE.md). Falling back to DATABASE_URL when no
separate read-only role exists keeps an unconfigured deployment running, just without the
database's own guarantee that a write cannot happen even if every check in the application
layer has a gap.
"""

import atexit
import threading

import ollama
from psycopg2 import pool as psycopg2_pool
from qdrant_client import QdrantClient

from config import DATABASE_URL, DATABASE_URL_RO, EMBED_MODEL, OLLAMA_HOST, QDRANT_URL

ollama_client = ollama.Client(host=OLLAMA_HOST)
qdrant = QdrantClient(url=QDRANT_URL)

_pool_lock = threading.Lock()
_pool = None


def _create_pool():
    dsn = DATABASE_URL_RO or DATABASE_URL
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is not set. Nothing that talks to Postgres can run without it."
        )
    return psycopg2_pool.ThreadedConnectionPool(1, 10, dsn, sslmode="require")


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
    resp = ollama_client.embeddings(model=EMBED_MODEL, prompt=text)
    return resp["embedding"]
