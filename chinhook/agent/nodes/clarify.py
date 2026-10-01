"""Putting a question to the user, and keeping hold of it until they answer."""

from chinhook.agent.nodes.common import _trace
from chinhook.agent.state import Clarification, GraphState, Understanding


def node_clarify(state: GraphState) -> dict:
    """Put the question to the user and stop. No SQL runs on the way out.

    The whole request waits on the answer, not only the part of it that was unclear: this is
    the only node that can end a turn with a question instead of an answer, and it always
    means the entire message is on hold until the reply comes back.
    """
    understanding = state.get("understanding") or Understanding()
    session = state["session"]
    clarification = (
        state.get("clarification")
        or understanding.clarification
        or Clarification(question="Could you say a little more about what you are after?")
    )
    if state.get("clarification_answer") and not clarification.options:
        # The reply could not be read and there were no options to read it against. Say what
        # was not understood rather than repeating the question as though nothing happened.
        clarification.reason = (
            f"I could not tell what \"{state['clarification_answer']}\" meant for this."
        )
    clarification.original_question = (
        state.get("original_question") or state.get("raw_question") or state["question"]
    )
    session.pending_clarification = clarification
    clarification.asked = True

    trace = _trace(
        state,
        "clarify",
        "asked the user",
        detail=clarification.question,
        options=clarification.options,
    )
    return {
        "clarification": clarification,
        "answer": clarification.question,
        "answer_kind": "clarification",
        "trace": trace,
    }
