"""Announcing which task is about to be worked on."""

from agent.nodes.common import _current_task, _trace
from agent.state import GraphState


def node_plan(state: GraphState) -> dict:
    task = _current_task(state)
    detail = f"{task.task_id}: {task.question or task.raw}" if task else "no task"
    trace = _trace(state, "plan", "task selected", detail=detail,
                   intent=task.intent if task else None,
                   entities=task.entities if task else [])
    return {"phase": "planned", "trace": trace}
