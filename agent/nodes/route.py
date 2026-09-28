"""Deciding where a message goes, before anything is retrieved, written or run."""

from agent import router
from agent.nodes.common import _trace
from agent.state import GraphState


def node_route(state: GraphState) -> dict:
    """Greeting, a reply to an open clarification, or an ordinary question.

    The only routes that skip node_understand entirely. Everything else, including a
    question about the conversation or about the assistant, goes there: it has the schema,
    the history and the message together, and can also split a message that holds more than
    one thing, none of which this function needs to know to make its own two decisions.
    """
    session = state["session"]
    question = state["question"]
    raw = state.get("raw_question") or question

    if router.is_small_talk(raw):
        if session.pending_clarification is not None:
            # The user said something like "thanks" or "never mind" instead of answering.
            # Without this, the question that was open stays open and silently intercepts
            # whatever the user asks next, tried against options that have nothing to do
            # with it.
            session.pending_clarification = None
        return {
            "route": "greeting",
            "route_reason": "small talk with no question in it",
            "trace": _trace(state, "route", "greeting", detail=raw[:80]),
        }

    pending = session.pending_clarification
    if pending is not None and (pending.resolved or not pending.asked):
        pending = None

    if pending is None:
        return {
            "route": "question",
            "trace": _trace(state, "route", "question", detail=raw[:80]),
        }

    reply = router.resolve_clarification_reply(raw, pending)
    if reply.resolved:
        return _resume_after_clarification(state, pending, reply)

    if router.looks_like_a_new_request(raw, pending):
        # The user moved on without answering. The open question is dropped rather than
        # left to swallow this new one.
        session.pending_clarification = None
        return {
            "route": "question",
            "trace": _trace(state, "route", "question", detail="a different request while a question was open"),
        }

    # A short reply that named none of the options. There is no cheap way left to tell
    # whether it answers the open question or starts a new one without asking a model, so it
    # goes to node_understand with the open question still attached: understand is already
    # given the conversation and can read a short reply against what was asked, the same way
    # it reads any other follow-up.
    return {
        "route": "question",
        "trace": _trace(state, "route", "question", detail="a short reply, read against the open question"),
    }


def _resume_after_clarification(state: GraphState, pending, reply: router.ClarificationReply) -> dict:
    """Put the answered question back and carry on from where it stopped.

    A reply to a clarification is not a new request. The request that caused the question is
    read again, in full, with the answer applied, rather than continuing from whatever partial
    work an earlier attempt left behind: node_understand is cheap enough now, on a retrieved
    slice of the schema rather than the whole catalog, that re-reading the original request is
    not worth optimising away, and it is what keeps this correct when the answer changes which
    tables the request even needs.
    """
    session = state["session"]
    said = state.get("raw_question") or state["question"]
    trace = _trace(
        state, "route", "clarification resolved",
        detail=f"answered with: {said}", task=None,
    )

    session.remember_clarification(pending.question, pending.options, said)
    session.pending_clarification = None

    if reply.negative:
        # The user rejected the reading that was offered, not merely left it unanswered.
        # The original request is read again, but with that reading now marked as wrong
        # rather than with no context at all: without the original request attached here,
        # understand had nothing to work from but the bare word "no".
        return {
            "route": "question",
            "original_question": pending.original_question,
            "clarification_answer": (
                f"No, that is not what was meant. The question you asked was: "
                f"\"{pending.question}\" and none of the offered readings "
                f"({', '.join(pending.options)}) apply. Read the original request again and "
                "either find a different, more specific reading of it or ask a different, "
                "more specific question."
            ),
            "clarification_resumed": True,
            "trace": trace + [{"node": "route", "event": "declined", "detail": "the offered reading was rejected"}],
        }

    return {
        "route": "question",
        "original_question": pending.original_question,
        "clarification_answer": said,
        "clarification_resumed": True,
        "trace": trace,
    }
