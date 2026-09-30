"""How many questions one browser session, and the app as a whole, may answer per minute.

Every visitor pays for their own model calls with their own key, but every question still
costs this server real work: embeddings, database connections, a slot in the agent's thread
pool. On a public link with no other access control, one session asking in a tight loop would
slow the app down for everyone else, which is what these limits prevent. Framework agnostic
on purpose, so it is testable without Streamlit running at all: app.py holds one bucket per
session in st.session_state and calls check() with it.
"""

from __future__ import annotations

import threading
import time

import config


class TokenBucket:
    """A capacity of tokens, refilled continuously at a fixed rate, spent one at a time.

    The classic shape for this kind of limit: it allows a short burst up to the full
    capacity, then settles into the steady rate, rather than a fixed window that allows a
    burst right at the edge of every window boundary.
    """

    def __init__(self, capacity: int, per_minute: int) -> None:
        self.capacity = max(1, capacity)
        self.rate = max(per_minute, 0) / 60.0  # tokens per second
        self.tokens = float(self.capacity)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        self._updated = now
        if elapsed > 0 and self.rate > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)

    def take(self) -> tuple[bool, float]:
        """Try to spend one token. Returns (allowed, seconds until one would be available)."""
        with self._lock:
            self._refill()
            if self.tokens >= 1:
                self.tokens -= 1
                return True, 0.0
            missing = 1 - self.tokens
            wait = missing / self.rate if self.rate > 0 else float("inf")
            return False, wait

    def give_back(self) -> None:
        """Return a token that was taken but should not have been charged after all."""
        with self._lock:
            self.tokens = min(self.capacity, self.tokens + 1)


# One bucket for the whole process, shared by every session, protecting the aggregate spend
# rather than any one visitor's share of it.
_global_bucket = TokenBucket(
    config.RATE_LIMIT_GLOBAL_PER_MINUTE, config.RATE_LIMIT_GLOBAL_PER_MINUTE
)


def new_session_bucket() -> TokenBucket:
    """A fresh bucket for one session, meant to be held in that session's own state."""
    limit = config.RATE_LIMIT_PER_SESSION_PER_MINUTE
    return TokenBucket(limit, limit)


def check(session_bucket: TokenBucket) -> tuple[bool, str]:
    """Whether a question from this session may proceed right now.

    The session's own budget is checked first, then the shared one, so a session that was
    never going to be allowed to send this message anyway is not the one that spends the
    shared budget on being refused. If the session passes but the shared budget has nothing
    left, the session's token is handed back rather than lost, since it was the shared limit
    that said no, not anything this particular session did.
    """
    allowed, wait = session_bucket.take()
    if not allowed:
        return False, f"Please slow down a little, and try again in about {wait:.0f} seconds."

    allowed, wait = _global_bucket.take()
    if not allowed:
        session_bucket.give_back()
        return False, f"This app is busy right now. Please try again in about {wait:.0f} seconds."

    return True, ""
