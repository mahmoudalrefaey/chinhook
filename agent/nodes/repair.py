"""Bounded repair, and moving on to the next task."""

from agent.nodes.common import _current_task, _next_open_task, _trace
from agent.state import GraphState


def node_repair_or_finish(state: GraphState) -> dict:
    """Bounded repair, or accept the failure and move on with the other tasks.

    This is the loop's only exit besides success, and it is what stops a retry from running
    forever: the attempt counter is compared against a fixed limit taken from the state.
    """
    task = _current_task(state)
    if task is None:
        return {"phase": "next_task"}
    max_attempts = state.get("max_attempts", 2)
    if task.attempts < max_attempts:
        task.attempts += 1
        task.status = "pending"
        task.verification = "unknown"
        task.semantic_checks_used = 0
        task.row_count = 0
        task.rows = []
        task.columns = []
        task.execution_status = "pending"
        trace = _trace(
            state,
            "repair",
            "retry",
            detail=f"{task.task_id}: attempt {task.attempts + 1} of {max_attempts + 1}, {task.failure_reason}",
            task=task.task_id,
            failure=task.failure_reason,
        )
        return {"phase": "repair", "trace": trace}

    task.status = "failed"
    task.failure_reason = task.failure_reason or "did not verify after the allowed attempts"
    # A task that has given up holds no result at all, so nothing downstream can read one
    # and mistake it for an answer.
    task.rows = []
    task.columns = []
    task.row_count = 0
    task.execution_status = "pending" if not task.error else "error"
    trace = _trace(
        state,
        "repair",
        "gave up",
        detail=f"{task.task_id}: {task.failure_reason}",
        task=task.task_id,
        attempts=task.attempts,
    )
    return {"phase": "task_failed", "trace": trace}


def node_next_task(state: GraphState) -> dict:
    tasks = state.get("tasks") or []
    index = _next_open_task(tasks, state.get("current_index", 0) + 1)
    if index < len(tasks):
        trace = _trace(
            state,
            "next task",
            "moved on",
            detail=f"{index + 1} of {len(tasks)}: {tasks[index].question or tasks[index].raw}",
            task=tasks[index].task_id,
        )
    else:
        trace = _trace(state, "next task", "all tasks processed", detail=f"{len(tasks)} task(s)")
    return {"current_index": index, "phase": "next_task", "trace": trace}
