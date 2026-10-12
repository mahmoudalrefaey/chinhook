"""One visitor's session: what they connected, their conversation, and their limits.

Held in this server's memory and nowhere else: not in a file, not in a database, not in a
cookie. The browser keeps only a random token for it, in the tab's own sessionStorage, so
each tab is a session of its own and closing the tab forgets the token. A session nobody has
used for config.SESSION_IDLE_MINUTES is dropped here, connection details included.

Because sessions live in one process's memory, the server runs as a single process (see the
Dockerfile); requests within it are served on threads.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from chinhook import config
from chinhook import rate_limit
from chinhook import runtime
from chinhook.agent.state import ChatSession


@dataclass(eq=False)
class Session:
    token: str
    authenticated: bool
    runtime: runtime.Runtime | None = None
    index_ready: bool = False
    chat: ChatSession | None = None
    # Each entry is {"role": "user" | "assistant", "content": str, "result": dict, "failed": bool}.
    messages: list[dict] = field(default_factory=list)
    # Indexes of clarifying questions already answered, so their options are not offered again.
    answered: set[int] = field(default_factory=set)
    rate_bucket: rate_limit.TokenBucket = field(default_factory=rate_limit.new_session_bucket)
    # Each attempt to connect costs a real database login and a model call, so it is limited
    # on its own, separately from questions. Refreshing the index shares it.
    setup_bucket: rate_limit.TokenBucket = field(default_factory=lambda: rate_limit.TokenBucket(5, 5))
    # Opening a configuration file runs scrypt, slow and memory-hungry on purpose, so trying
    # passphrases is limited on its own too, as is trying the app's own passphrase.
    import_bucket: rate_limit.TokenBucket = field(default_factory=lambda: rate_limit.TokenBucket(5, 5))
    auth_bucket: rate_limit.TokenBucket = field(default_factory=lambda: rate_limit.TokenBucket(5, 5))
    # Held while a long job (connecting, indexing, answering) runs, so one session never runs
    # two at once over the same conversation.
    busy: threading.Lock = field(default_factory=threading.Lock)
    last_seen: float = field(default_factory=time.monotonic)

    def forget_connection(self) -> None:
        """Back to the setup screen, with nothing of the previous connection or chat kept."""
        self.runtime = None
        self.index_ready = False
        self.new_chat()

    def new_chat(self) -> None:
        """An empty conversation. The agent's memory of the old one goes with its messages."""
        self.chat = None
        self.messages = []
        self.answered = set()

    @property
    def phase(self) -> str:
        if not self.authenticated:
            return "auth"
        if self.runtime is None:
            return "setup"
        if not self.index_ready:
            return "prepare"
        return "chat"


class SessionStore:
    def __init__(self, idle_seconds: float, max_sessions: int) -> None:
        self._idle = idle_seconds
        self._max = max(1, max_sessions)
        # Ordered by last use, oldest first, so expiry and eviction only ever look at the front.
        self._sessions: OrderedDict[str, Session] = OrderedDict()
        self._lock = threading.Lock()

    def create(self) -> Session:
        session = Session(
            token=secrets.token_urlsafe(32),
            authenticated=not config.APP_PASSPHRASE,
        )
        with self._lock:
            self._expire()
            while len(self._sessions) >= self._max:
                self._evict_one()
            self._sessions[session.token] = session
        return session

    def _evict_one(self) -> None:
        """Make room by dropping the longest-idle session that never connected to anything.

        Opening a session costs nothing, so a flood of them only ever pushes out other empty
        ones, never someone in the middle of a conversation. Only when every session holds a
        connection does the longest-idle one go, whatever it holds.
        """
        for token, candidate in self._sessions.items():
            if candidate.runtime is None and not candidate.busy.locked():
                del self._sessions[token]
                return
        self._sessions.popitem(last=False)

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        with self._lock:
            self._expire()
            session = self._sessions.get(token)
            if session is not None:
                session.last_seen = time.monotonic()
                self._sessions.move_to_end(token)
            return session

    def _expire(self) -> None:
        cutoff = time.monotonic() - self._idle
        while self._sessions:
            token, oldest = next(iter(self._sessions.items()))
            # A session in the middle of a long answer is not idle, whatever its timestamp.
            if oldest.last_seen >= cutoff or oldest.busy.locked():
                break
            del self._sessions[token]


store = SessionStore(config.SESSION_IDLE_MINUTES * 60, config.MAX_SESSIONS)
