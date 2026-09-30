"""Answer pipeline for the web interface and the command line.

The reasoning that used to live in this file, and separately in scripts/generator.py, now
runs as a LangGraph workflow in the agent package. This module keeps its original job: be the
seam the interface and the command line call, and return everything they display, and adds
the per-task states, the workflow trace and the token counts the workflow produces.

Every function here works on the database and model of the current session (see
runtime.py): the caller wraps its calls in runtime.use(...).
"""

import time

import runtime
from agent.graph import DEFAULT_MAX_ATTEMPTS, STAGE_TO_KEY, WorkflowError, run_turn
from agent.nodes import STAGE_EXECUTE, STAGE_GENERATE, STAGE_RETRIEVE, STAGE_SUMMARISE, STAGE_UNDERSTAND
from agent.session import new_chat

__all__ = [
    "PIPELINE",
    "STAGE_EXECUTE",
    "STAGE_GENERATE",
    "STAGE_RETRIEVE",
    "STAGE_SUMMARISE",
    "STAGE_TO_KEY",
    "STAGE_UNDERSTAND",
    "answer",
    "new_chat",
    "schema_overview",
]

# The interface draws these as a row of nodes and lights each one up as it happens. The keys
# match the keys used in the timings dictionary returned by answer(). Retrieval, writing SQL
# and running it happen inside one task's own graph, for however many tasks a message became,
# possibly several at once (see agent/task_graph.py); each task times its own three stages on
# its own thread, and those are folded into one total per stage, across every task in the
# turn, before this dictionary is built, so the diagram still lights up each one in turn even
# though nothing here is watching a single task run through them in real time.
PIPELINE = [
    {"key": "question", "title": "Question", "detail": "Plain language in"},
    {"key": "retrieve", "title": "Retrieve", "detail": "Schema search in Qdrant"},
    {"key": "generate", "title": "Write SQL", "detail": "The model, through a tool call"},
    {"key": "execute", "title": "Query", "detail": "Your database, read only"},
    {"key": "summarise", "title": "Answer", "detail": "The model writes the reply"},
]


def _blank_result(model_name):
    return {
        "ok": False,
        "answer": None,
        "schema": None,
        "sql": None,
        "columns": None,
        "rows": None,
        "model": model_name,
        "timings": {},
        "error": None,
        "kind": "error",
        "route": "",
        "clarification": None,
        "tasks": [],
        "trace": [],
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "llm_calls": 0},
    }


def answer(
    question, model_name=None, on_stage=None, on_token=None, session=None,
    max_attempts=DEFAULT_MAX_ATTEMPTS,
):
    """Answer one question and report everything that happened along the way.

    on_stage is called with a short label before each step so the caller can show progress.
    on_token, when given, is called with each chunk of the reply as it is written, so a caller
    with somewhere live to show it does not have to wait for the whole thing. Failures are
    returned inside the result rather than raised, so the interface can render a message
    instead of a stack trace.

    session is the conversation this question belongs to. Passing one keeps context across
    turns; leaving it None gives the question an empty conversation of its own, which is what
    a caller that is not holding a chat wants.
    """
    model_name = model_name or runtime.current().llm.model
    result = _blank_result(model_name)
    started = time.perf_counter()

    try:
        result = run_turn(
            question,
            session=session,
            model=model_name,
            on_stage=on_stage,
            on_token=on_token,
            max_attempts=max_attempts,
        )
    except WorkflowError as exc:
        result["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"

    if not result.get("timings"):
        result["timings"] = {"total": time.perf_counter() - started}
    return result


def schema_overview():
    """Every table and view in the connected schema, with its columns and approximate rows.

    The row count comes from the planner's own statistics, not a live COUNT(*) per table: on a
    database sized in the millions of rows, counting every table would make this overview
    itself the slow thing to load. Nothing is hardcoded; every query here reads the database's
    own catalogs, the same ones the indexer reads.
    """
    from scripts.db import get_catalog, get_connection, get_row_estimates, get_tables

    with get_connection() as conn:
        tables = get_tables(conn)
        catalog = get_catalog(conn)
        estimates = get_row_estimates(conn, tables=tables)

    return [
        {
            "table": name,
            "kind": kind,
            "columns": catalog.get(name, []),
            "rows": int(estimates.get(name, 0)),
        }
        for name, kind in tables.items()
    ]


def prepare_index(progress=None):
    """Bring the current database's index up to date, building it the first time.

    Only tables whose shape changed since the last run are re-indexed, so calling this for a
    database that is already indexed and unchanged costs one catalog read and nothing else.
    progress, when given, is called as progress(done, total, message) while it runs.
    """
    from scripts.db.indexing import check_and_index

    return check_and_index(progress)


def delete_index():
    """Remove everything this deployment holds about the current database."""
    from scripts.db.indexing import delete_index as _delete_index

    _delete_index()


def describe_index_error(exc: Exception, db=None) -> str:
    """Why indexing or reading the index failed, in words that say whose problem it is.

    db is the database that was being indexed; the current session's when not given.
    """
    import connection

    text = f"{type(exc).__name__}: {exc}".lower()
    if "qdrant" in text or "6333" in text or "responsehandlingexception" in text or (
        "unexpectedresponse" in text
    ):
        return (
            "The search index service is not reachable right now. This is a problem with "
            "this deployment, not with your database; try again in a minute."
        )
    if isinstance(exc, runtime.NotConnected):
        return "No database is connected."
    return connection.describe_database_error(exc, db or runtime.current().db)
