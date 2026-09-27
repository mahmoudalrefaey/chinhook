"""Writing the query for one task."""

import json

from agent import llm, verify as verification
from agent.nodes.common import BASE_SQL_RULES, _current_task, _is_meta, _trace, _usage, _visible_schema
from agent.state import GraphState


# ---------- SQL generation ----------

def node_generate(state: GraphState) -> dict:
    task = _current_task(state)
    if task is None or _is_meta(task):
        return {"phase": "generated"}

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
    if task.semantic:
        lines.append(f"Grounding already established: {task.semantic}")
    if task.clarification_answer:
        # The user was asked something about this task and has answered. The answer is part
        # of what the task is now, so it belongs in the prompt rather than in memory of it.
        lines.append(
            f"The user was asked a question about this task and answered: "
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

    message, spent = llm.chat(state["model"], [{"role": "user", "content": "\n".join(lines)}], tools=tools)
    usage = _usage(state, spent)

    tool_calls = getattr(message, "tool_calls", None) or []
    if not tool_calls:
        task.failure_kind = "sql_generation"
        task.failure_reason = "no query was written for this task"
        task.status = "failed"
        trace = _trace(state, "generate", "no query written", detail=task.failure_reason, task=task.task_id)
        return {"phase": "generate_failed", "usage": usage, "trace": trace}

    try:
        arguments = json.loads(tool_calls[0].function.arguments or "{}")
        query = str(arguments.get("query") or "").strip()
    except (ValueError, AttributeError, IndexError):
        query = ""

    if not query:
        task.failure_kind = "sql_generation"
        task.failure_reason = "no query was written for this task"
        task.status = "failed"
        trace = _trace(state, "generate", "no query written", detail=task.failure_reason, task=task.task_id)
        return {"phase": "generate_failed", "usage": usage, "trace": trace}

    task.sql = query
    task.sql_repaired = task.attempts > 0
    task.sql_limit = verification.statement_limit(query)
    task.status = "sql_ready"
    task.failure_reason = None
    task.repair_hint = ""
    trace = _trace(state, "generate", "sql", detail=f"{task.task_id}: wrote a query", task=task.task_id, sql=query)
    return {"phase": "generated", "usage": usage, "trace": trace}
