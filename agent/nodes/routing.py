"""Where a node sends the run next.

Plain functions over the state rather than prompts, so the shape of the workflow is readable in
one place and cannot drift."""

from agent.nodes.common import _current_task, _is_meta, _next_open_task
from agent.state import GraphState


def route_after_understand(state: GraphState) -> str:
    understanding = state["understanding"]
    if understanding.clarity == "unsupported":
        return "answer"
    if understanding.clarification is not None:
        return "clarify"
    return "plan"


def route_after_route(state: GraphState) -> str:
    """Where a classified message goes next.

    Three routes never touch the schema, a query or the database: a greeting, a question
    about the conversation, and a question about the assistant itself. They are answered
    from what the chat already knows.
    """
    route = state.get("route")
    if route == "greeting":
        return "greeting"
    if route in {"conversation", "meta"}:
        return "conversation_answer"
    if state.get("awaiting_reask") or (
        state.get("clarification") is not None
        and not state.get("clarification_resumed")
        and (state.get("clarification_rejected") or state.get("clarification_answer"))
    ):
        return "clarify"
    if state.get("clarification_resumed") and state.get("tasks"):
        # The work already done is kept and the part that was on hold is picked up.
        return "plan"
    return "understand"


def route_after_retrieve(state: GraphState) -> str:
    if state.get("phase") == "retrieve_failed":
        return "repair"
    task = _current_task(state)
    if task is None:
        return "next_task"
    if _is_meta(task):
        # Nothing to retrieve, ground, generate or execute. It goes straight to verification,
        # which is what marks a task that needs no query as done.
        return "verify"
    if task.needs_grounding or task.filters:
        return "ground"
    return "generate"


def route_after_ground(state: GraphState) -> str:
    task = _current_task(state)
    if task is None or _is_meta(task) or task.status == "failed":
        return "next_task"
    return "generate"


def route_after_validate(state: GraphState) -> str:
    return "repair" if state.get("phase") == "validation_failed" else "execute"


def route_after_execute(state: GraphState) -> str:
    if state.get("phase") == "execute_failed":
        return "repair"
    if state.get("needs_restart"):
        return "answer"
    return "verify"


def route_after_verify(state: GraphState) -> str:
    if state.get("needs_restart"):
        return "answer"
    return "next_task" if state.get("phase") == "verified" else "repair"


def route_after_repair(state: GraphState) -> str:
    if state.get("phase") == "task_failed":
        return "next_task"
    task = _current_task(state)
    if task is None or _is_meta(task):
        return "next_task"
    return "retrieve"


def route_after_next_task(state: GraphState) -> str:
    tasks = state.get("tasks") or []
    index = _next_open_task(tasks, state.get("current_index", 0))
    if index < len(tasks):
        return "plan"
    return "answer"
