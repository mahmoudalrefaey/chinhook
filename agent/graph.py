"""The graph itself: nodes, edges and the one turn entry point.

Routing is written in plain functions over the state, not in prompts, so the shape of the
workflow is readable in one place and cannot drift. The loop that retries a failed task runs
through node_repair_or_finish, which compares the task's attempt count against a fixed limit
in the state, so a task cannot retry forever and a failing task never stops the others.

No checkpointer is configured. That is deliberate: conversation state lives in the caller's
ChatSession, which exists for one chat, and nothing here writes a checkpoint, stores a
thread or keeps conversation state in a long-lived graph instance.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Optional

from langgraph.graph import END, START, StateGraph

from agent import nodes
from agent.nodes import (
    STAGE_EXECUTE,
    STAGE_GENERATE,
    STAGE_RETRIEVE,
    STAGE_REWRITE,
    STAGE_SUMMARISE,
    STAGE_UNDERSTAND,
)
from agent.session import new_chat
from agent.state import (
    ChatSession,
    GraphState,
    MAX_TASKS_PER_REQUEST,
    TaskState,
    TokenUsage,
    Turn,
    Understanding,
    split_schema,
)

# How a stage label maps onto the pipeline the interface already draws. One definition,
# imported by chat_engine, so the diagram keeps its existing keys and is not extended.
# Reading the question is accounted against the "question" node, which is the honest place
# for it: the node already means "plain language in", and the diagram still lights the same
# four stages in the same order as before.
STAGE_TO_KEY = {
    STAGE_REWRITE: "question",
    STAGE_UNDERSTAND: "question",
    STAGE_RETRIEVE: "retrieve",
    STAGE_GENERATE: "generate",
    STAGE_EXECUTE: "execute",
    STAGE_SUMMARISE: "summarise",
}

DEFAULT_MAX_ATTEMPTS = 2

# One task, retried until it gives up, visits retrieve, ground, generate, validate, execute,
# verify and repair once per attempt.
_STEPS_PER_TASK_ATTEMPT = 7
# Plus plan and next_task once per task, regardless of how many attempts that task takes.
_FIXED_STEPS_PER_TASK = 2
# rewrite, route and understand run once per request, and answer runs once at the end. The
# rest is headroom, since being generous here costs nothing: a run that behaves normally never
# comes close to this ceiling, and the ceiling only exists to stop one that does not.
_FIXED_OVERHEAD = 8


def _recursion_limit(max_attempts: int) -> int:
    """A recursion limit generous enough for the worst case LangGraph could actually reach.

    The old limit was a function of max_attempts alone: 24 + 12 * max_attempts, which sized
    the budget for one task and gave no more room for a second one. A question that splits
    into two or more tasks, each retrying even once, could exceed it and lose the entire
    turn, including whatever tasks inside it had already succeeded.

    LangGraph needs this number before the graph runs, at a point where the real number of
    tasks a question will become is not known yet: that only comes out of node_understand,
    partway through the same invocation this limit is set for. Sizing against
    MAX_TASKS_PER_REQUEST instead of the true count is what makes this correct rather than
    just a bigger guess: node_understand enforces that same number as a hard cap on how many
    tasks a single message can ever produce, so this is sized for the worst case that could
    actually happen, not merely a case unlikely to be exceeded.
    """
    per_task = _FIXED_STEPS_PER_TASK + _STEPS_PER_TASK_ATTEMPT * (max_attempts + 1)
    return _FIXED_OVERHEAD + MAX_TASKS_PER_REQUEST * per_task


class Timing:
    """Per-stage seconds for one request, plus the stage callback.

    Retrieval runs once per task, so a three-task request adds to the same key rather than
    overwriting it. The keys are the interface's existing ones, so the diagram shows the
    same four stages it always did and no new measurement appears.
    """

    def __init__(self, announce: Optional[Callable[[str], None]] = None) -> None:
        self.announce = announce or (lambda _stage: None)
        self.stages: dict[str, float] = {}
        self.started = time.perf_counter()
        self._stage: Optional[str] = None
        self._announced: Optional[str] = None
        self._mark = time.perf_counter()

    def enter(self, stage: str, announce: bool = True) -> None:
        self._close()
        key = STAGE_TO_KEY.get(stage, stage)
        if announce and key != self._announced:
            self._announced = key
            self.announce(stage)
        self._stage = key
        self._mark = time.perf_counter()

    def _close(self) -> None:
        if self._stage is None:
            return
        self.stages[self._stage] = self.stages.get(self._stage, 0.0) + (
            time.perf_counter() - self._mark
        )

    def as_dict(self) -> dict[str, float]:
        self._close()
        self._stage = None
        return {**self.stages, "total": time.perf_counter() - self.started}


def _timed(stage: str, announce: bool = True) -> Callable:
    """Wrap a node so entering it announces the stage and accounts for the time it takes.

    Nodes that only move the workflow along, rather than do work the user is waiting to see
    finished, pass announce=False: their time is still counted, but the progress diagram
    keeps showing the same stages it always did.
    """

    def wrap(fn: Callable) -> Callable:
        def run(state: GraphState) -> dict:
            state["timing"].enter(stage, announce=announce)
            return fn(state)

        run.__name__ = fn.__name__
        return run

    return wrap


def build_graph():
    """Assemble the workflow. Deterministic edges, no cycle without a counter."""
    graph = StateGraph(GraphState)

    graph.add_node("rewrite", _timed(STAGE_REWRITE)(nodes.node_rewrite))
    graph.add_node("route", _timed(STAGE_UNDERSTAND, announce=False)(nodes.node_route))
    graph.add_node("greeting", _timed(STAGE_SUMMARISE)(nodes.node_greeting))
    graph.add_node("understand", _timed(STAGE_UNDERSTAND, announce=False)(nodes.node_understand))
    graph.add_node("clarify", _timed(STAGE_SUMMARISE)(nodes.node_clarify))
    graph.add_node("plan", _timed(STAGE_UNDERSTAND, announce=False)(nodes.node_plan))
    graph.add_node("retrieve", _timed(STAGE_RETRIEVE)(nodes.node_retrieve))
    graph.add_node("ground", _timed(STAGE_RETRIEVE)(nodes.node_ground))
    graph.add_node("generate", _timed(STAGE_GENERATE)(nodes.node_generate))
    graph.add_node("validate", _timed(STAGE_GENERATE, announce=False)(nodes.node_validate))
    graph.add_node("execute", _timed(STAGE_EXECUTE)(nodes.node_execute))
    graph.add_node("verify", _timed(STAGE_EXECUTE, announce=False)(nodes.node_verify))
    graph.add_node("repair", _timed(STAGE_GENERATE, announce=False)(nodes.node_repair_or_finish))
    graph.add_node("next_task", _timed(STAGE_UNDERSTAND, announce=False)(nodes.node_next_task))
    graph.add_node("answer", _timed(STAGE_SUMMARISE)(nodes.node_answer))

    graph.add_edge(START, "rewrite")
    graph.add_conditional_edges(
        "rewrite",
        nodes.route_after_rewrite,
        {"clarify": "clarify", "route": "route"},
    )
    graph.add_conditional_edges(
        "route",
        nodes.route_after_route,
        {
            "greeting": "greeting",
            "clarify": "clarify",
            "understand": "understand",
            "plan": "plan",
        },
    )
    graph.add_edge("greeting", END)

    graph.add_conditional_edges(
        "understand",
        nodes.route_after_understand,
        {"clarify": "clarify", "answer": "answer", "plan": "plan"},
    )
    graph.add_edge("clarify", END)
    graph.add_edge("answer", END)

    graph.add_edge("plan", "retrieve")
    graph.add_conditional_edges(
        "retrieve",
        nodes.route_after_retrieve,
        {
            "repair": "repair",
            "ground": "ground",
            "generate": "generate",
            "verify": "verify",
            "next_task": "next_task",
        },
    )
    graph.add_conditional_edges(
        "ground",
        nodes.route_after_ground,
        {"generate": "generate", "next_task": "next_task"},
    )

    graph.add_edge("generate", "validate")
    graph.add_conditional_edges(
        "validate", nodes.route_after_validate, {"execute": "execute", "repair": "repair"}
    )
    graph.add_conditional_edges(
        "execute",
        nodes.route_after_execute,
        {"verify": "verify", "repair": "repair", "answer": "answer"},
    )
    graph.add_conditional_edges(
        "verify",
        nodes.route_after_verify,
        {"next_task": "next_task", "repair": "repair", "answer": "answer"},
    )
    graph.add_conditional_edges(
        "repair", nodes.route_after_repair, {"retrieve": "retrieve", "next_task": "next_task"}
    )
    graph.add_conditional_edges(
        "next_task", nodes.route_after_next_task, {"plan": "plan", "answer": "answer"}
    )

    return graph.compile()


_COMPILED = None


def _compiled():
    """One compiled graph per process. It holds topology only, never conversation state."""
    global _COMPILED
    if _COMPILED is None:
        _COMPILED = build_graph()
    return _COMPILED


class WorkflowError(Exception):
    """A failure that escaped every node, carrying the flag the interface already shows."""

    def __init__(self, message: str, needs_restart: bool = False) -> None:
        super().__init__(message)
        self.needs_restart = needs_restart


def run_turn(
    question: str,
    session: Optional[ChatSession] = None,
    model: Optional[str] = None,
    on_stage: Optional[Callable[[str], None]] = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> dict[str, Any]:
    """Answer one question inside one chat, and report everything that happened.

    The returned dict carries the shape chat_engine already hands the interface, plus the
    per-task states, the workflow trace and the token counts. Failures come back inside the
    result rather than raised, so the interface renders a message rather than a traceback.
    """
    import config

    model = model or config.DEFAULT_MODEL
    session = session if session is not None else new_chat()
    timing = Timing(on_stage)
    started = time.perf_counter()

    # Nothing is marked answered here. The router needs the open question and its options to
    # read the reply against, and the question itself stays on the session until that
    # decision has been made, so it is recorded afterwards by the node that made it.

    initial: GraphState = {
        "session": session,
        "model": model,
        "question": question,
        # What the user typed, kept beside the working question for the rest of the run. The
        # rewrite node replaces "question" with the clearer version; this is never replaced.
        "raw_question": question,
        "rewrite": "",
        "rewrite_conflict": "",
        "trace": [],
        "usage": TokenUsage(),
        "max_attempts": max_attempts,
        "current_index": 0,
        "phase": "start",
        "needs_restart": False,
        "clarifications": [],
        "understanding": Understanding(),
        "original_question": "",
        "clarification_answer": "",
        "timing": timing,
    }

    try:
        final = _compiled().invoke(
            initial, config={"recursion_limit": _recursion_limit(max_attempts)}
        )
    except Exception as exc:  # noqa: BLE001
        message = re.sub(r"\x1b\[[0-9;]*m", "", str(exc))
        raise WorkflowError(f"{type(exc).__name__}: {message}") from exc

    result = _result_from_state(final, question, model, session)
    result["timings"] = timing.as_dict()
    result["timings"]["total"] = time.perf_counter() - started
    return result


def _primary(tasks: list[TaskState]) -> Optional[TaskState]:
    """The task the interface's single SQL and table panel should show."""
    for task in tasks:
        if task.status == "verified" and task.sql:
            return task
    for task in tasks:
        if task.sql:
            return task
    return tasks[0] if tasks else None


def _result_from_state(
    state: dict, question: str, model: str, session: ChatSession
) -> dict[str, Any]:
    tasks: list[TaskState] = list(state.get("tasks") or [])
    usage: TokenUsage = state.get("usage") or TokenUsage()
    answer = state.get("answer")
    kind = state.get("answer_kind") or "answer"
    primary = _primary(tasks)

    definitions: list[str] = []
    seen: set[str] = set()
    for task in tasks:
        for name, definition in split_schema(task.schema).items():
            if name not in seen:
                seen.add(name)
                definitions.append(definition)

    result: dict[str, Any] = {
        "ok": bool(answer),
        "answer": answer,
        "schema": "\n".join(definitions) or None,
        "sql": primary.sql if primary else None,
        "columns": list(primary.columns) if primary else [],
        "rows": list(primary.rows) if primary else [],
        "model": model,
        "timings": {},
        "error": state.get("error"),
        "needs_restart": bool(state.get("needs_restart")),
        # Additive: the interface's existing panels are untouched, these are new.
        "kind": kind,
        "route": state.get("route") or "",
        "rewrite": state.get("rewrite") or "",
        "rewrite_conflict": state.get("rewrite_conflict") or "",
        "clarification": _clarification_payload(state),
        "tasks": [task.as_trace() for task in tasks],
        "trace": list(state.get("trace") or []),
        "usage": usage.as_dict(),
    }
    if kind == "clarification":
        result["error"] = None

    session.record_turn(_turn_from(question, state, tasks, answer or ""))
    return result


def _clarification_payload(state: dict) -> Optional[dict[str, Any]]:
    clarification = state.get("clarification")
    if clarification is None or not getattr(clarification, "asked", False):
        return None
    return clarification.as_dict()


def _turn_from(question: str, state: dict, tasks: list[TaskState], answer: str) -> Turn:
    understanding = state.get("understanding")
    summaries = []
    for task in tasks:
        if task.status == "verified" and task.row_count == 1 and task.rows:
            summary = f"{task.columns[0] if task.columns else 'value'} = {task.rows[0][0]}"
        elif task.status == "verified":
            summary = f"{task.row_count} row(s)"
        else:
            summary = ""
        summaries.append(
            {
                "question": task.question or task.raw,
                "intent": task.intent,
                "entities": list(task.entities),
                "filters": list(task.filters),
                "answer_summary": summary,
            }
        )
    return Turn(
        user=question,
        question=(understanding.resolved_question if understanding else question) or question,
        intent=(tasks[0].intent if tasks else ""),
        tasks=summaries,
        tables=sorted({name for task in tasks for name in task.schema_tables}),
        answer=answer[:400],
    )
