"""What every node needs: the stage labels, the small state helpers, and the
shapes a task is built from."""

from typing import Any, Optional

from agent import schema as schema_store, verify as verification
from agent.state import GraphState, TaskState, TokenUsage, Understanding


# ---------- stage labels ----------
# Reused by chat_engine so the existing pipeline diagram lights up in the same order and
# with the same four labels it has always used.

STAGE_UNDERSTAND = "Reading the question"
STAGE_REWRITE = "Making the question clear"
STAGE_RETRIEVE = "Finding relevant tables"
STAGE_GENERATE = "Writing the query"
STAGE_EXECUTE = "Running it against the database"
STAGE_SUMMARISE = "Putting the answer together"


BASE_SQL_RULES = (
    "You answer questions using this schema, querying a PostgreSQL database. "
    "Table and column names are case-sensitive — always wrap them in double "
    "quotes exactly as given below. Use PostgreSQL syntax only "
    "(e.g. CURRENT_DATE, NOW(), INTERVAL '7 days') — never SQLite or MySQL "
    "date functions like date('now', ...). "
    "When matching user-provided text values (names, titles, etc.) in WHERE "
    "clauses, use ILIKE instead of = or LIKE so matching is case-insensitive "
    "— the user may type a value in any case. This case-insensitive rule "
    "applies only to data values, never to table or column identifiers.\n"
)

_INTENT_ROW_KIND = {
    "count": "count",
    "sum": "count",
    "average": "count",
    "aggregate": "count",
    "max": "single",
    "min": "single",
    "rank": "limited",
    "list": "rows",
    "lookup": "rows",
    "compare": "rows",
    "other": "rows",
    "meta": "rows",
}


# ---------- small helpers ----------

def _trace(state: GraphState, node: str, event: str, detail: str = "", **data) -> list[dict]:
    entry = {"node": node, "event": event}
    if detail:
        entry["detail"] = detail
    if data:
        entry["data"] = data
    trace = list(state.get("trace") or [])
    trace.append(entry)
    return trace


def _usage(state: GraphState, spent: TokenUsage) -> TokenUsage:
    total = state.get("usage") or TokenUsage()
    total.add(spent)
    return total


def _current_task(state: GraphState) -> Optional[TaskState]:
    tasks = state.get("tasks") or []
    index = state.get("current_index", 0)
    if 0 <= index < len(tasks):
        return tasks[index]
    return None


def _is_meta(task: TaskState) -> bool:
    return task.intent == "meta" or (not task.needs_sql)


def _visible_schema(task: TaskState) -> str:
    """The schema a task's prompt should carry.

    A task that got an answer on the wrong entity during repair is re-grounded from scratch,
    so it is given the whole schema rather than only what the failed attempt retrieved.
    """
    if task.attempts > 0 and task.failure_reason and "wrong entity" in (task.failure_reason or ""):
        return schema_store.all_schema_text()
    return task.schema


def _looks_like_a_data_task(
    question: str, entities: list[str], filters: list[dict], metrics: list[str]
) -> bool:
    """Whether a task the understanding step labelled "meta" still looks like real data work.

    Any one of these on its own is enough: a genuine question about the assistant or the
    conversation has no entity, filter or metric of its own, because there is nothing in the
    data for those to refer to, and it does not happen to name something the connected
    database actually holds.
    """
    if entities or filters or metrics:
        return True
    return bool(schema_store.mentions_table_word(question))


def _task_from_spec(spec: dict[str, Any], index: int, raw: str) -> TaskState:
    intent = str(spec.get("intent") or "other").lower().strip()
    if intent not in _INTENT_ROW_KIND:
        intent = "other"
    question = str(spec.get("question") or raw).strip()
    filters = spec.get("filters")
    filters = [f for f in filters if isinstance(f, dict)] if isinstance(filters, list) else []
    entities = spec.get("entities")
    entities = [str(e) for e in entities if isinstance(e, (str, int))] if isinstance(entities, list) else []
    metrics = spec.get("metrics")
    metrics = [str(m) for m in metrics if isinstance(m, (str, int))] if isinstance(metrics, list) else []
    limit = spec.get("expected_limit")
    try:
        limit = int(limit) if limit is not None else None
    except (TypeError, ValueError):
        limit = None
    if limit is not None and limit <= 0:
        limit = None
    stated = verification.parse_limit_from_question(question)
    if stated is not None:
        limit = stated

    if intent == "meta" and _looks_like_a_data_task(question, entities, filters, metrics):
        # The understanding step read this as a question about the assistant or the
        # conversation, which skips SQL entirely and is trusted to verify itself. A genuine
        # meta question has none of its own entities, filters or metrics, and does not name
        # anything the connected database actually holds; this one does, so the reading is
        # treated as wrong rather than trusted. Without this, a data question mislabelled
        # this one way came back marked verified with no query ever run, and the answer node
        # wrote a confident sentence from nothing.
        intent = "other"

    return TaskState(
        task_id=f"T{index + 1}",
        raw=raw,
        question=question,
        intent=intent,
        entities=entities,
        filters=filters,
        metrics=metrics,
        expected_limit=limit,
        expected_row_kind=_INTENT_ROW_KIND[intent],
        needs_sql=intent != "meta",
        needs_grounding=bool(spec.get("needs_grounding")),
        pending_ambiguity=str(spec.get("ambiguity") or "").strip(),
        pending_ambiguity_options=[
            str(o).strip() for o in (spec.get("ambiguity_options") or []) if str(o).strip()
        ],
        ambiguity_kind=str(spec.get("ambiguity_kind") or "").strip().lower(),
    )


def _fallback_understanding(question: str) -> Understanding:
    """Used when the model returns nothing usable even after a repair.

    The message is still answered, as one task in its own words, rather than dropped. It
    gets no invented structure: the only field filled in is the question itself.
    """
    return Understanding(
        clarity="clear",
        resolved_question=question,
        tasks=[TaskState(task_id="T1", raw=question, question=question, intent="other")],
    )


def _next_open_task(tasks: list[TaskState], start: int) -> int:
    """The first task at or after start that still has work to do.

    Resuming after a clarification keeps the tasks that were already answered, so the walk
    has to step over them rather than run them again or stop short of them.
    """
    for index in range(max(0, start), len(tasks)):
        task = tasks[index]
        if task.status != "verified":
            return index
    return len(tasks)
