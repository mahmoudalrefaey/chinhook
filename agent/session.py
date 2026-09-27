"""Chat lifecycle.

A chat is one ChatSession. It is created when a chat starts and dropped when it ends, which
is the whole memory story: there is no module level session, no store, no checkpoint
directory and no cross-chat persistence, so a second chat cannot read the first one's
context even in principle.
"""

from __future__ import annotations

import itertools
import time

from agent.state import ChatSession

_counter = itertools.count(1)


def new_chat() -> ChatSession:
    """An empty chat."""
    return ChatSession(session_id=f"chat-{int(time.time() * 1000)}-{next(_counter)}")
