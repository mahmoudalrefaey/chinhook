"""Checking a query is safe to run, before it is run."""

import re

import sqlglot

from agent import schema as schema_store, verify as verification
from agent.nodes.common import _current_task, _is_meta, _trace, _usage
from agent.state import GraphState, TaskState


_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|vacuum|analyze|"
    r"comment|call|do|listen|notify|prepare|execute|set|reset|discard)\b",
    re.IGNORECASE,
)


def node_validate(state: GraphState) -> dict:
    task = _current_task(state)
    if task is None or _is_meta(task):
        return {"phase": "validated"}

    from scripts.db_module import validate_sql

    sql = task.sql or ""
    problem = None

    if not sql:
        problem = "no query was produced"
    else:
        import sqlglot

        try:
            statements = sqlglot.parse(sql, read="postgres")
        except Exception as exc:  # noqa: BLE001
            statements = []
            problem = f"the query could not be parsed: {exc}"
        if problem is None and len([s for s in statements if s is not None]) != 1:
            problem = "more than one statement was produced; exactly one is allowed"
        if problem is None:
            try:
                allowed = validate_sql(sql)
            except Exception as exc:  # noqa: BLE001
                allowed = False
                problem = f"the query could not be validated: {exc}"
            if allowed is False and problem is None:
                problem = "only read-only SELECT statements are allowed"
        if problem is None and _FORBIDDEN.search(_strip_literals(sql)):
            problem = "the query contains a statement that is not allowed here"
        if problem is None:
            unknown = [t for t in verification.referenced_tables(sql) if t not in schema_store.catalog()]
            if unknown:
                problem = (
                    f"the query reads {', '.join(unknown)}, which this database does not have"
                )
                task.failure_kind = "unsupported"

    if problem:
        task.validation_error = problem
        if not task.failure_kind:
            task.failure_kind = "sql_validation"
        task.failure_reason = f"the query was not safe to run: {problem}"
        trace = _trace(
            state, "validate", "rejected", detail=f"{task.task_id}: {problem}", task=task.task_id
        )
        return {"phase": "validation_failed", "trace": trace}

    task.validation_error = None
    trace = _trace(state, "validate", "accepted", detail=f"{task.task_id}: safe to run", task=task.task_id)
    return {"phase": "validated", "trace": trace}


def _strip_literals(sql: str) -> str:
    """Remove quoted text so a word inside a value is not mistaken for a keyword."""
    return re.sub(r"'(?:[^']|'')*'", "''", sql)
