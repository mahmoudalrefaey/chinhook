"""Putting a question to the user, and keeping hold of it until they answer."""

from agent.nodes.common import _current_task, _is_meta, _trace, _usage
from agent.state import Clarification, GraphState, TaskState, TokenUsage, Understanding


def _hold_clarification(session, clarification: Clarification) -> Clarification:
    """Put an open question on the chat so the next message is read as the reply to it.

    Whether the question came from reading the message or from grounding a task, the next
    thing the user types is an answer to it. That is only true if the chat knows the
    question is open, and the router reads the reply against the options it was given.
    """
    clarification.asked = True
    session.pending_clarification = clarification
    return clarification


# ---------- clarification ----------

def node_clarify(state: GraphState) -> dict:
    """Put the question to the user and stop. No SQL runs on the way out.

    The question comes from understanding when the ambiguity was found there, or from the
    grounding node when it was found after the schema was in front of the model. Either way
    the question remembers which request it belongs to and which tasks were waiting on it,
    because the reply has to resume that request rather than be read as a new one.
    """
    understanding = state.get("understanding") or Understanding()
    session = state["session"]
    tasks = state.get("tasks") or []
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
    if clarification.scope == "task":
        clarification.task_ids = clarification.task_ids or [
            task.task_id for task in tasks
            if task.status == "failed" and task.semantic_ambiguity
        ]
        # The whole request is kept, not only the part that is waiting. The tasks already
        # answered are what a reply must not throw away, and the tasks waiting on the answer
        # are the only ones reset when it arrives.
        clarification.pending_tasks = clarification.pending_tasks or list(tasks)
    session.pending_clarification = clarification
    clarification.asked = True

    trace = _trace(        state,
        "clarify",
        "asked the user",
        detail=clarification.question,
        options=clarification.options,
        scope=clarification.scope,
        tasks=clarification.task_ids,
    )
    return {
        "clarification": clarification,
        "answer": clarification.question,
        "answer_kind": "clarification",
        "phase": "clarified",
        "trace": trace,
    }


def _hold_task_for_clarification(
    state: GraphState,
    task: TaskState,
    question: str,
    options: list[str],
    usage: TokenUsage,
    trace: list[dict],
) -> dict:
    """Stop one task, put the question to the user, and let the rest of the request finish.

    The task keeps its place in the request and its work, so an answer to this question picks
    it up again from where it stopped rather than sending the whole message back to the start.
    """
    task.semantic_ambiguity = question
    task.status = "failed"
    task.failure_kind = "ambiguous"
    task.failure_reason = f"needs clarification: {question}"
    tasks = state.get("tasks") or [task]
    asked = _hold_clarification(
        state["session"],
        Clarification(
            question=question,
            options=options,
            reason=question,
            scope="task",
            original_question=state.get("original_question")
            or state.get("raw_question")
            or state["question"],
            task_ids=[task.task_id],
            pending_tasks=list(tasks),
        ),
    )
    trace = trace + [
        {
            "node": "ground",
            "event": "ambiguity",
            "task": task.task_id,
            "detail": question,
        }
    ]
    return {
        "phase": "grounded",
        "usage": usage,
        "trace": trace,
        "clarifications": [asked],
    }
