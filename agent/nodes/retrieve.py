"""Finding the schema for one task, from the cache or from Qdrant."""

from agent.nodes.common import _current_task, _is_meta, _trace
from agent import schema as schema_store
from agent.state import GraphState


# ---------- schema retrieval ----------

def node_retrieve(state: GraphState) -> dict:
    task = _current_task(state)
    if task is None or _is_meta(task):
        return {"phase": "retrieved"}

    session = state["session"]
    try:
        schema_text, names, from_cache = schema_store.retrieve_for_task(task, session.schema_cache)
        task.schema = schema_text
        task.schema_tables = names
        task.schema_from_cache = from_cache
        task.status = "schema_retrieved"
        task.error = None
        detail = (
            f"{task.task_id}: reused {len(names)} table(s) already retrieved in this chat"
            if from_cache
            else f"{task.task_id}: retrieved {len(names)} table(s)"
        )
        trace = _trace(state, "retrieve", "schema", detail, tables=names, from_cache=from_cache)
        return {"phase": "retrieved", "trace": trace, "error": None}
    except Exception as exc:  # noqa: BLE001
        message = f"{type(exc).__name__}: {exc}"
        task.status = "failed"
        task.failure_kind = "schema_retrieval"
        task.failure_reason = f"the schema could not be read: {message}"
        task.error = message
        trace = _trace(state, "retrieve", "schema failed", detail=message, task=task.task_id)
        return {"phase": "retrieve_failed", "trace": trace}


# ---------- semantic grounding ----------
