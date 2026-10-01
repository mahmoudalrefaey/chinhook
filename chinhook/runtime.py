"""The database and the model one browser session is connected to.

Everything here is supplied by the person using the app, on the setup screen, and lives only
in that session's memory: nothing in this module is read from the environment, written to
disk, or shared with another session. The deployment's own settings (where Qdrant is, which
embedding model runs in-process, the limits) are a different thing and live in
chinhook/config.py.

The rest of the code reaches the current connection through current(), rather than having it
passed into every function that eventually needs it. The web page sets it with use() around
each call it makes, and LangGraph copies the context into the threads it runs parallel tasks
on (langgraph/pregel/_executor.py calls copy_context for every task it submits), so a task
running on a worker thread still sees the connection of the session that asked the question
and never another session's.
"""

from __future__ import annotations

import contextlib
import hashlib
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator

from sqlalchemy.engine import URL

from chinhook import config

# The two SQL dialects the app can connect to, by the SQLAlchemy backend name of their URL.
POSTGRES = "postgresql"
MYSQL = "mysql"
SUPPORTED_DIALECTS = (POSTGRES, MYSQL)

# How the connection is encrypted, as offered on the setup screen. Postgres takes these as
# its own sslmode; MySQL has no opportunistic mode, so anything but "disable" means TLS.
SSL_MODES = ("prefer", "require", "disable")


@dataclass(frozen=True)
class DatabaseSettings:
    """Where the user's database is and how to reach it.

    url is a SQLAlchemy URL with the password inside it; its own repr hides the password, so
    printing or logging one of these never leaks it. schema is the Postgres schema to read,
    or, for MySQL, the database name, since a MySQL schema and database are the same thing.
    """

    url: URL
    schema: str
    ssl_mode: str = "prefer"

    @property
    def dialect(self) -> str:
        return self.url.get_backend_name()

    def describe(self) -> str:
        """A one-line, password-free label for the interface."""
        host = self.url.host or "localhost"
        port = f":{self.url.port}" if self.url.port else ""
        return f"{self.url.database or ''} on {host}{port}"


@dataclass(frozen=True)
class LLMSettings:
    """An OpenAI-compatible chat endpoint: its base URL, the key for it, and which model.

    fast_model is optional: a cheaper model for the small, well-defined jobs (a sentence
    describing each table at index time). Left empty, model does everything.
    """

    base_url: str
    api_key: str = field(repr=False)
    model: str
    fast_model: str = ""

    @property
    def small_model(self) -> str:
        return self.fast_model or self.model


@dataclass(frozen=True)
class Runtime:
    db: DatabaseSettings
    llm: LLMSettings

    @property
    def tenant_id(self) -> str:
        """Which index in Qdrant belongs to this connection.

        The same database, read as the same user through the same schema, with the same
        embedding model, always lands on the same id, so reconnecting later reuses an index
        already built rather than paying for it again. The password is deliberately not part
        of it: rotating a password should not throw away a perfectly good index, and nobody
        reaches an index without first passing a real login to the database it describes.
        The user name is part of it, since two users of one database can be granted different
        tables, and the embedding model is, since vectors from two models are not comparable.
        """
        url = self.db.url
        parts = [
            self.db.dialect,
            (url.host or "").lower(),
            str(url.port or ""),
            url.database or "",
            self.db.schema,
            url.username or "",
            config.EMBED_MODEL,
        ]
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


class NotConnected(RuntimeError):
    """Something needed the database or the model before a connection was set up."""


_current: ContextVar[Runtime | None] = ContextVar("chinhook_runtime", default=None)


def current() -> Runtime:
    runtime = _current.get()
    if runtime is None:
        raise NotConnected("No database and model are connected in this session yet.")
    return runtime


def activate(runtime: Runtime | None) -> None:
    """Make runtime the current connection for the rest of this thread's current context.

    For the web page, which calls this once at the top of every script run: Streamlit runs
    each session's script on that session's own thread, so this reaches exactly the calls
    that one run makes, and a run with nothing connected yet passes None so it can never
    inherit anything.
    """
    _current.set(runtime)


@contextlib.contextmanager
def use(runtime: Runtime) -> Iterator[Runtime]:
    """Make runtime the current connection for everything run inside this block."""
    token = _current.set(runtime)
    try:
        yield runtime
    finally:
        _current.reset(token)
