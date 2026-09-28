"""The graph itself: nodes, edges and the one turn entry point.

Routing is written in plain functions over the state, not in prompts, so the shape of the
workflow is readable in one place and cannot drift. Independent tasks run at the same time,
each in its own copy of agent/task_graph.py's small graph (see node_run_tasks below), which
is also where each task's own bounded retry loop lives, isolated from every other task's.

No checkpointer is configured. That is deliberate: conversation state lives in the caller's
ChatSession, which exists for one chat, and nothing here writes a checkpoint, stores a
thread or keeps conversation state in a long-lived graph instance.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Optional

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from agent import nodes, task_graph
from agent.nodes import STAGE_EXECUTE, STAGE_GENERATE, STAGE_RETRIEVE, STAGE_SUMMARISE, STAGE_UNDERSTAND
from agent.session import new_chat
from agent.state import (
    ChatSession,
    GraphState,
    MAX_TASKS_PER_REQUEST,
    TaskState,
    TokenUsage,
    Turn,
    Understanding,
)

# How a stage label maps onto the pipeline the interface draws. One definition, imported by
# chat_engine, so the diagram's keys and this module's stages cannot drift apart.
STAGE_TO_KEY = {
    STAGE_UNDERSTAND: "question",
    STAGE_RETRIEVE: "retrieve",
    STAGE_GENERATE: "generate",
    STAGE_EXECUTE: "execute",
    STAGE_SUMMARISE: "summarise",
}

DEFAULT_MAX_ATTEMPTS = 2

# Per task: entry, retrieve, generate, check_and_run, verify and repair once per attempt.
_STEPS_PER_TASK = 3 + 3 * (DEFAULT_MAX_ATTEMPTS + 1)
# route, understand and answer run once per request.
_FIXED_OVERHEAD = 6


def _recursion_limit(max_attempts: int) -> int:
    """A recursion limit generous enough for the worst case this graph could actually reach.

    Sized against MAX_TASKS_PER_REQUEST, the hard cap node_understand itself enforces on how
    many tasks one message can ever produce, rather than the number of tasks this particular
    message turned into: that count is not known until node_understand has already run, which
    is after LangGraph needs this limit. Each task also runs its own graph with its own
    recursion budget (see task_graph.run_task), so this only has to cover the parent graph's
    own steps.
    """
    per_task = 3 + 3 * (max_attempts + 1)
    return _FIXED_OVERHEAD + MAX_TASKS_PER_REQUEST * per_task


class Timing:
    """Per-stage seconds for one request, plus the stage callback."""

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

    def pause(self, announce_stage: Optional[str] = None) -> None:
        """Close the current stage without opening a new one.

        For the span where independent tasks are running, possibly several at once: there is
        no single stage to attribute that whole span to the way there was when one task ran
        at a time, so nothing here should keep accumulating into whichever stage happened to
        be open before it. Each task instead times its own retrieve, generate and execute
        locally and safely, on its own thread, and those totals are folded into self.stages
        directly once every task has finished; see agent.graph.run_turn. announce_stage, when
        given, still fires the interface's progress pulse for it.
        """
        self._close()
        self._stage = None
        if announce_stage is not None:
            key = STAGE_TO_KEY.get(announce_stage, announce_stage)
            if key != self._announced:
                self._announced = key
                self.announce(announce_stage)

    def add_finished_stage(self, key: str, seconds: float) -> None:
        """Record a stage's duration directly, computed elsewhere rather than measured by
        entering and leaving it on this object. Used once every task in a turn has finished
        and their own, separately-timed retrieve/generate/execute totals are ready to fold
        into the same dictionary as the stages this object timed itself."""
        self.stages[key] = self.stages.get(key, 0.0) + seconds

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
    def wrap(fn: Callable) -> Callable:
        def run(state: GraphState) -> dict:
            state["timing"].enter(stage, announce=announce)
            return fn(state)

        run.__name__ = fn.__name__
        return run

    return wrap


# ---------- routing between the parent graph's own nodes ----------

def route_after_route(state: GraphState) -> str:
    return "greeting" if state.get("route") == "greeting" else "understand"


def route_after_understand(state: GraphState):
    understanding = state["understanding"]
    if understanding.clarity == "unsupported":
        return "answer"
    if understanding.clarification is not None:
        return "clarify"
    tasks = state.get("tasks") or []
    if not tasks:
        return "answer"
    # Paused here, once, rather than timed from inside node_run_task: that node runs once per
    # task, possibly several at once on separate threads, and Timing.enter mutates shared
    # counters that are not safe to touch from more than one thread at a time. Called from
    # this routing function instead, which LangGraph only ever calls once and synchronously,
    # right before the tasks it is about to dispatch actually start. Each task's own retrieve,
    # generate and execute times are measured on its own thread instead (see
    # agent/task_graph.py) and folded into this same Timing object afterwards, once every
    # task has finished and there is no more concurrent access to guard against.
    state["timing"].pause(announce_stage=STAGE_RETRIEVE)
    return [
        Send("run_task", {"tasks": [t], "model": state["model"], "session": state["session"],
                           "max_attempts": state.get("max_attempts", DEFAULT_MAX_ATTEMPTS)})
        for t in tasks
    ]


# ---------- the node that runs one task's whole pipeline ----------

def node_run_task(state: GraphState) -> dict:
    """Run one task to completion in its own graph, and hand the result back to be merged.

    state here is this branch's own local view: Send gave it exactly one task, and nothing
    it does can be seen by any other branch until this returns. Not wrapped in _timed: see
    the note in route_after_understand on why the shared Timing object is paused from there
    instead, once, rather than entered from here on every task's own thread.
    """
    task = state["tasks"][0]
    finished, spent, task_trace, phase_times = task_graph.run_task(
        task, state["model"], state["session"], state.get("max_attempts", DEFAULT_MAX_ATTEMPTS)
    )
    return {
        "tasks": [finished],
        "task_usages": [spent],
        "trace": task_trace,
        "task_phase_times": [phase_times],
    }


def build_graph():
    """Assemble the workflow. Deterministic edges, no cycle without a counter."""
    graph = StateGraph(GraphState)

    graph.add_node("route", _timed(STAGE_UNDERSTAND, announce=False)(nodes.node_route))
    graph.add_node("greeting", _timed(STAGE_SUMMARISE)(nodes.node_greeting))
    graph.add_node("understand", _timed(STAGE_UNDERSTAND)(nodes.node_understand))
    graph.add_node("clarify", _timed(STAGE_SUMMARISE)(nodes.node_clarify))
    graph.add_node("run_task", node_run_task)
    graph.add_node("answer", _timed(STAGE_SUMMARISE)(nodes.node_answer))

    graph.add_edge(START, "route")
    graph.add_conditional_edges(
        "route", route_after_route, {"greeting": "greeting", "understand": "understand"}
    )
    graph.add_edge("greeting", END)

    graph.add_conditional_edges(
        "understand", route_after_understand, ["clarify", "answer", "run_task"]
    )
    graph.add_edge("clarify", END)
    graph.add_edge("run_task", "answer")
    graph.add_edge("answer", END)

    return graph.compile()


_COMPILED = None


def _compiled():
    """One compiled graph per process. It holds topology only, never conversation state."""
    global _COMPILED
    if _COMPILED is None:
        _COMPILED = build_graph()
    return _COMPILED


class WorkflowError(Exception):
    """A failure that escaped every node."""


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

    initial: GraphState = {
        "session": session,
        "model": model,
        "question": question,
        "raw_question": question,
        "trace": [],
        "usage": TokenUsage(),
        "task_usages": [],
        "task_phase_times": [],
        "max_attempts": max_attempts,
        "clarification": None,
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

    # Every task has finished by this point, so there is no more concurrent access to guard
    # against: each task's own retrieve/generate/execute seconds, timed locally on its own
    # thread while it ran, are folded into the same totals the sequential stages timed
    # themselves. A stage the same task passed through more than once, on a retry, has
    # already summed to one number inside agent.task_graph.run_task; the max across tasks is
    # what is taken here, since independent tasks ran that stage at the same time as each
    # other, not one after another.
    for key in ("retrieve", "generate", "execute"):
        per_task = [times.get(key, 0.0) for times in (final.get("task_phase_times") or [])]
        if per_task:
            timing.add_finished_stage(key, max(per_task))

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
    for spent in state.get("task_usages") or []:
        usage.add(spent)
    answer = state.get("answer")
    kind = state.get("answer_kind") or "answer"
    primary = _primary(tasks)

    definitions: list[str] = []
    seen: set[str] = set()
    for task in tasks:
        for name in task.schema_tables:
            if name not in seen and task.schema:
                seen.add(name)
        if task.schema and task.schema not in definitions:
            definitions.append(task.schema)

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
        "kind": kind,
        "route": state.get("route") or "",
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
