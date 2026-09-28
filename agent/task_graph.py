"""One task's own pipeline: retrieve, write SQL, run it, verify it, repair it.

Built as its own small compiled graph, invoked once per task from agent/graph.py, rather
than as nodes on the main graph. That separation is what makes running several tasks at once
safe: this graph's state holds exactly one task, private to whichever branch invoked it, so
one task's retry loop can never read or overwrite another task's progress. The parent graph
only ever sees this graph's final return value for each task, merged into its own task list
by task_id.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional, TypedDict

import sqlglot
from langgraph.graph import END, START, StateGraph

from agent import llm, schema as schema_store
from agent import verify as verification
from agent.nodes.common import BASE_SQL_RULES, _is_meta, _visible_schema
from agent.state import ChatSession, TaskState, TokenUsage

DEFAULT_MAX_ATTEMPTS = 2
_WIDE_TOP_K = 16

# The pipeline diagram's three data-touching stages. Bucketed the same way the single-task
# graph originally grouped them: schema retrieval and value search under "retrieve", writing
# and repairing SQL under "generate", running and verifying it under "execute".
_PHASE_RETRIEVE = "retrieve"
_PHASE_GENERATE = "generate"
_PHASE_EXECUTE = "execute"


class TaskGraphState(TypedDict, total=False):
    task: TaskState
    model: str
    session: ChatSession
    max_attempts: int
    usage: TokenUsage
    trace: list[dict[str, Any]]
    phase: str
    widen_retrieval: bool
    # Seconds spent in each of the three stages above, for this task alone. Read-modify-write
    # rather than a LangGraph reducer, same as usage and trace above: this graph's own nodes
    # run one at a time, never in parallel with each other, so there is nothing here for a
    # reducer to reconcile. What runs several of these graphs at once is agent/graph.py, which
    # only ever sees the finished totals this graph returns, never these intermediate writes.
    phase_times: dict[str, float]


def _timed(phase: str):
    """Wrap a node so the time it takes is added to this task's own phase_times.

    Purely local bookkeeping inside one task's private state: safe to use even though several
    of these graphs can be running on separate threads at once, because each one only ever
    reads and writes its own state, never another task's.
    """

    def wrap(fn):
        def run(state: TaskGraphState) -> dict:
            started = time.perf_counter()
            result = fn(state) or {}
            elapsed = time.perf_counter() - started
            times = dict(state.get("phase_times") or {})
            times[phase] = times.get(phase, 0.0) + elapsed
            return {**result, "phase_times": times}

        run.__name__ = fn.__name__
        return run

    return wrap


def _trace(state: TaskGraphState, node: str, event: str, detail: str = "", **data) -> list[dict]:
    entry = {"node": node, "event": event}
    if detail:
        entry["detail"] = detail
    if data:
        entry["data"] = data
    trace = list(state.get("trace") or [])
    trace.append(entry)
    return trace


def _usage(state: TaskGraphState, spent: TokenUsage) -> TokenUsage:
    total = state.get("usage") or TokenUsage()
    total.add(spent)
    return total


# ---------- entry: meta tasks need no query at all ----------

def node_entry(state: TaskGraphState) -> dict:
    task = state["task"]
    if _is_meta(task):
        task.status = "verified"
        task.verification = "pass"
        return {
            "task": task,
            "phase": "verified",
            "trace": _trace(state, "task", "no query needed", detail=f"{task.task_id}: meta task"),
        }
    return {"phase": "start"}


def route_after_entry(state: TaskGraphState) -> str:
    return END if state.get("phase") == "verified" else "retrieve"


# ---------- retrieve: schema and real values for this one task ----------

def node_retrieve(state: TaskGraphState) -> dict:
    task = state["task"]
    session = state["session"]
    top_k = 16 if state.get("widen_retrieval") else None
    try:
        schema_text, names, from_cache = schema_store.retrieve_for_task(
            task, session.schema_cache, top_k=top_k
        )
        task.schema = schema_text
        task.schema_tables = names
        task.schema_from_cache = from_cache
        task.value_hints = _value_hints_for(task)
        task.status = "schema_retrieved"
        task.error = None
        detail = (
            f"{task.task_id}: reused {len(names)} table(s) already retrieved in this chat"
            if from_cache
            else f"{task.task_id}: retrieved {len(names)} table(s)"
        )
        return {
            "task": task,
            "phase": "retrieved",
            "trace": _trace(state, "retrieve", "schema", detail, tables=names, from_cache=from_cache),
        }
    except Exception as exc:  # noqa: BLE001
        message = f"{type(exc).__name__}: {exc}"
        task.status = "failed"
        task.failure_kind = "schema_retrieval"
        task.failure_reason = f"the schema could not be read: {message}"
        task.error = message
        return {
            "task": task,
            "phase": "retrieve_failed",
            "trace": _trace(state, "retrieve", "schema failed", detail=message),
        }


def _value_hints_for(task: TaskState) -> list[dict]:
    """Real stored values that might be what this task's filters mean.

    Searched once per filter value the task actually has, rather than once for the whole
    task question: a task with two filters gets hints for each of them, not a single search
    that has to represent both at once.
    """
    hints: list[dict] = []
    seen: set[tuple] = set()
    terms = [str(f.get("value") or "").strip() for f in (task.filters or [])]
    terms = [t for t in terms if t]
    if not terms:
        return hints
    for term in terms:
        for hit in schema_store.search_values_for(term, top_k=5):
            key = (hit["table"], hit["column"], hit["value"])
            if key not in seen:
                seen.add(key)
                hints.append({**hit, "term": term})
    return hints


def route_after_retrieve(state: TaskGraphState) -> str:
    return "repair" if state.get("phase") == "retrieve_failed" else "generate"


# ---------- generate: write the SQL ----------

def node_generate(state: TaskGraphState) -> dict:
    task = state["task"]
    model = state["model"]
    from scripts.db_module import tools

    schema_text = _visible_schema(task)
    lines = [
        BASE_SQL_RULES + schema_text,
        "",
        "Answer exactly one task with one SELECT statement.",
        f"Task: {task.question or task.raw}",
        f"Intent: {task.intent}. Result shape expected: {task.expected_row_kind}.",
    ]
    if task.entities:
        lines.append(f"Entities: {', '.join(task.entities)}.")
    if task.metrics:
        lines.append(f"Measure: {'; '.join(task.metrics)}.")
    if task.filters:
        lines.append(f"Filters to apply: {json.dumps(task.filters, ensure_ascii=False)}.")
    if task.value_hints:
        hint_lines = [
            f'  "{h["term"]}" may be stored as "{h["value"]}" in {h["table"]}.{h["column"]}'
            for h in task.value_hints[:8]
        ]
        lines.append("Real values that may be what a filter means, from an index of the data "
                      "itself (use the stored spelling in the query, not the user's word):\n"
                      + "\n".join(hint_lines))
    if task.clarification_answer:
        lines.append(
            f"The user was asked a question about this request and answered: "
            f"\"{task.clarification_answer}\". Use that answer to settle what was undecided."
        )
    if task.expected_limit is not None:
        lines.append(f"Return at most {task.expected_limit} row(s).")
    if task.expected_row_kind == "count":
        lines.append("Return a single aggregate value, in one column.")
    if task.expected_row_kind == "single":
        lines.append("Return the single best matching row.")
    if task.repair_hint:
        lines.append(f"Note from verification: {task.repair_hint}")
    if task.attempts > 0 and task.sql:
        lines.append(
            "Your previous attempt for this task was rejected. Diagnose it and write a "
            f"different statement.\nPrevious SQL: {task.sql}\n"
            f"Why it was rejected: {task.failure_reason}"
        )
    lines.append("Call the run_sql_query tool with the SQL. Do not answer in prose.")

    message, spent = llm.chat(model, [{"role": "user", "content": "\n".join(lines)}], tools=tools)
    usage = _usage(state, spent)

    tool_calls = getattr(message, "tool_calls", None) or []
    query = ""
    if tool_calls:
        try:
            arguments = json.loads(tool_calls[0].function.arguments or "{}")
            query = str(arguments.get("query") or "").strip()
        except (ValueError, AttributeError, IndexError):
            query = ""

    if not query:
        task.failure_kind = "sql_generation"
        task.failure_reason = "no query was written for this task"
        task.status = "failed"
        return {
            "task": task, "usage": usage, "phase": "generate_failed",
            "trace": _trace(state, "generate", "no query written", detail=task.failure_reason),
        }

    task.sql = query
    task.sql_repaired = task.attempts > 0
    task.sql_limit = verification.statement_limit(query)
    task.status = "sql_ready"
    task.failure_reason = None
    task.repair_hint = ""
    return {
        "task": task, "usage": usage, "phase": "generated",
        "trace": _trace(state, "generate", "sql", detail=f"{task.task_id}: wrote a query", sql=query),
    }


# ---------- check and run: one parse, then a validated, read-only execution ----------

_SYSTEM_SCHEMAS = {"information_schema", "pg_catalog"}


def _is_known_table(name: str) -> bool:
    schema, _, bare = name.rpartition(".")
    if bare in schema_store.catalog():
        return True
    if schema:
        return schema.lower() in _SYSTEM_SCHEMAS or schema.lower().startswith("pg_")
    return name in schema_store.catalog()


def node_check_and_run(state: TaskGraphState) -> dict:
    task = state["task"]
    sql = task.sql or ""

    problem: Optional[str] = None
    tree = None
    if not sql:
        problem = "no query was produced"
    else:
        try:
            tree = sqlglot.parse_one(sql, read="postgres")
        except Exception as exc:  # noqa: BLE001
            problem = f"the query could not be parsed: {exc}"

    if problem is None:
        unknown = [t for t in verification.tables_in(tree) if not _is_known_table(t)]
        if unknown:
            problem = f"the query reads {', '.join(unknown)}, which this database does not have"
            task.failure_kind = "unsupported"

    if problem:
        task.validation_error = problem
        if not task.failure_kind:
            task.failure_kind = "sql_validation"
        task.failure_reason = f"the query was not safe to run: {problem}"
        return {
            "task": task, "phase": "validation_failed",
            "trace": _trace(state, "check_and_run", "rejected", detail=problem),
        }

    # The database connection's own validation runs again here regardless: the check above
    # decides whether this task's own retry loop should fire, but scripts.db.sql.run_sql_query
    # never trusts a caller's own parse for whether a write can reach the database.
    from scripts.db_module import run_sql_query

    result = run_sql_query(sql)
    if isinstance(result, dict) and "columns" in result:
        rows = result.get("rows") or []
        task.columns = list(result.get("columns") or [])
        task.rows = list(rows)
        task.row_count = len(rows)
        task.truncated = bool(result.get("truncated"))
        task.execution_status = "ok"
        task.status = "executed"
        task.error = None
        detail = f"{task.task_id}: {task.row_count} row(s)"
        if task.truncated:
            detail += " (truncated)"
        return {
            "task": task, "phase": "executed",
            "trace": _trace(state, "check_and_run", "ran", detail=detail),
        }

    error = str(result.get("error")) if isinstance(result, dict) else str(result)
    task.execution_status = "error"
    task.status = "failed"
    task.error = error
    task.failure_kind = "sql_execution"
    task.failure_reason = f"the database rejected the query: {error}"
    return {
        "task": task,
        "phase": "execute_failed",
        "trace": _trace(state, "check_and_run", "failed", detail=f"{task.task_id}: {error}"),
    }


def route_after_check_and_run(state: TaskGraphState) -> str:
    if state.get("phase") in {"validation_failed", "execute_failed"}:
        return "repair"
    return "verify"


# ---------- verify: deterministic checks only ----------

def node_verify(state: TaskGraphState) -> dict:
    task = state["task"]
    verdict, checks, notes = verification.verify_task(task, {
        "error": None,
        "row_count": task.row_count,
    })
    trace = _trace(state, "verify", "deterministic", detail=f"{task.task_id}: {verdict}", checks=checks)

    task.verification = verdict
    task.verification_checks = checks
    task.verification_notes = notes
    reasons = [
        f"{check['check']}: {check['detail']}"
        for check in checks
        if check["verdict"] == "fail" and check["detail"]
    ]

    if verdict != "fail":
        task.status = "verified"
        task.failure_reason = None
        return {"task": task, "phase": "verified", "trace": trace}

    task.status = "failed"
    task.failure_kind = "result_verification"
    task.failure_reason = (
        f"the result does not answer the task ({'; '.join(reasons)})"
        if reasons
        else "the result does not answer the task"
    )
    return {"task": task, "phase": "verify_failed", "trace": trace}


def route_after_verify(state: TaskGraphState) -> str:
    return END if state.get("phase") == "verified" else "repair"


# ---------- repair: bounded retry, and where to retry from ----------

def node_repair(state: TaskGraphState) -> dict:
    task = state["task"]
    max_attempts = state.get("max_attempts", DEFAULT_MAX_ATTEMPTS)
    task.attempts += 1
    if task.attempts > max_attempts:
        # Out of attempts. The failed rows are not carried into the answer: a task that gave
        # up is reported as failed, not as though a stale result from an earlier try were
        # the real one.
        task.rows = []
        task.columns = []
        task.row_count = 0
        task.status = "failed"
        return {
            "task": task, "phase": "task_failed",
            "trace": _trace(state, "repair", "gave up", detail=f"{task.task_id}: {task.failure_reason}"),
        }

    task.repair_hint = task.failure_reason or ""
    widen = task.failure_kind in {"unsupported", "schema_retrieval"}
    return {
        "task": task,
        "phase": "repairing",
        "widen_retrieval": widen,
        "trace": _trace(
            state, "repair", "retrying",
            detail=f"{task.task_id}: attempt {task.attempts} of {max_attempts}",
        ),
    }


def route_after_repair(state: TaskGraphState) -> str:
    if state.get("phase") == "task_failed":
        return END
    return "retrieve" if state.get("widen_retrieval") else "generate"


# ---------- graph assembly ----------

def build_task_graph():
    graph = StateGraph(TaskGraphState)
    graph.add_node("entry", node_entry)
    graph.add_node("retrieve", _timed(_PHASE_RETRIEVE)(node_retrieve))
    graph.add_node("generate", _timed(_PHASE_GENERATE)(node_generate))
    graph.add_node("check_and_run", _timed(_PHASE_EXECUTE)(node_check_and_run))
    graph.add_node("verify", _timed(_PHASE_EXECUTE)(node_verify))
    graph.add_node("repair", _timed(_PHASE_GENERATE)(node_repair))

    graph.add_edge(START, "entry")
    graph.add_conditional_edges("entry", route_after_entry, {"retrieve": "retrieve", END: END})
    graph.add_conditional_edges("retrieve", route_after_retrieve, {"generate": "generate", "repair": "repair"})
    graph.add_edge("generate", "check_and_run")
    graph.add_conditional_edges(
        "check_and_run", route_after_check_and_run, {"verify": "verify", "repair": "repair"}
    )
    graph.add_conditional_edges("verify", route_after_verify, {"repair": "repair", END: END})
    graph.add_conditional_edges(
        "repair", route_after_repair, {"retrieve": "retrieve", "generate": "generate", END: END}
    )
    return graph.compile()


_COMPILED = None


def run_task(
    task: TaskState, model: str, session: ChatSession, max_attempts: int
) -> tuple[TaskState, TokenUsage, list[dict], dict[str, float]]:
    """Run one task through its own graph to completion, and return it, its own token usage,
    its own trace, and the seconds it spent in each stage, all for the caller to merge into
    the parent turn."""
    global _COMPILED
    if _COMPILED is None:
        _COMPILED = build_task_graph()

    initial: TaskGraphState = {
        "task": task,
        "model": model,
        "session": session,
        "max_attempts": max_attempts,
        "usage": TokenUsage(),
        "trace": [],
        "phase": "start",
        "phase_times": {},
    }
    # Generous but bounded: entry, retrieve, generate, check_and_run, verify and repair once
    # per attempt, plus headroom. Sized per task since each task now runs in its own graph
    # rather than sharing one recursion budget with every other task in the request.
    steps_per_attempt = 5
    limit = 6 + steps_per_attempt * (max_attempts + 1)
    final = _COMPILED.invoke(initial, config={"recursion_limit": limit})
    return (
        final["task"],
        final.get("usage") or TokenUsage(),
        list(final.get("trace") or []),
        dict(final.get("phase_times") or {}),
    )
