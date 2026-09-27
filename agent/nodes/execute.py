"""Running one task's query and keeping the result on that task alone."""

from agent.nodes.common import _current_task, _is_meta, _trace
from agent.state import GraphState

from scripts.db import run_sql_query


# ---------- execution ----------

def node_execute(state: GraphState) -> dict:
    task = _current_task(state)
    if task is None or _is_meta(task):
        return {"phase": "executed"}

    result = run_sql_query(task.sql)
    if isinstance(result, dict) and "columns" in result:
        rows = result.get("rows") or []
        task.columns = list(result.get("columns") or [])
        task.rows = list(rows)
        task.row_count = len(rows)
        task.execution_status = "ok"
        task.status = "executed"
        task.error = None
        detail = f"{task.task_id}: {task.row_count} row(s)"
        trace = _trace(state, "execute", "ran", detail=detail, task=task.task_id, rows=task.row_count)
        return {"phase": "executed", "trace": trace, "needs_restart": False}

    error = str(result.get("error")) if isinstance(result, dict) else str(result)
    task.execution_status = "error"
    task.status = "failed"
    task.error = error
    task.failure_kind = "sql_execution"
    task.failure_reason = f"the database rejected the query: {error}"
    trace = _trace(
        state, "execute", "failed", detail=f"{task.task_id}: {error}", task=task.task_id
    )
    return {
        "phase": "execute_failed",
        "trace": trace,
        "needs_restart": "InFailedSqlTransaction" in error,
    }
