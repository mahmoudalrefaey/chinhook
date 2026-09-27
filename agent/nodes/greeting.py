"""Replying to small talk.

A greeting, a thank you, an apology, a goodbye: nothing here has an answer to look up, so
the reply is written rather than retrieved. The database is not touched on this path at all,
which is the point of the router sending it here.

The reply is written by the model rather than picked from a list, because a list of fixed
answers is the thing that goes wrong here: it says the same thing to every greeting, it
answers in one language to a message in another, and it tends to steer the conversation back
to what the assistant can do rather than replying to what was said.
"""

from agent import llm
from agent.nodes.common import _trace, _usage
from agent.nodes.prompts import _GREETING_SYSTEM
from agent.state import GraphState, TokenUsage


def node_greeting(state: GraphState) -> dict:
    """Answer a conversational message in the register it was written in.

    The model is given the message and a compact note of what has already been said, so it
    can greet a first message and acknowledge a returning one differently, and so it does
    not walk into a conversation it has no memory of. The cost is counted like any other
    call, and nothing in this path reads or writes a table.
    """
    session = state["session"]
    model = state["model"]
    message = state.get("question") or ""

    material = [f"Message: {message}"]
    if session.turns:
        history = session.turn_summary(limit=3)
        if history:
            material.append(f"Earlier in this conversation:\n{history}")
    else:
        material.append("This is the first message of the conversation.")

    text, spent = llm.chat_text(
        model, _GREETING_SYSTEM, "\n\n".join(material), max_completion_tokens=150
    )
    text = (text or "").strip()
    if not text:
        # Nothing to show, and the app's existing error path is a better answer than an
        # empty bubble. The trace says where it happened.
        raise ValueError("the reply to a conversational message came back empty")

    return {
        "answer": text,
        "answer_kind": "greeting",
        "usage": _usage(state, spent) or TokenUsage(),
        "trace": _trace(
            state,
            "greeting",
            "answered",
            detail=f"replied to {len(message.split())} word(s) of small talk; no data was looked up",
        ),
    }
