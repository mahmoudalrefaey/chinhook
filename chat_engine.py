"""Answer pipeline for the web interface and the command line.

The reasoning that used to live in this file, and separately in scripts/generator.py, now
runs as a LangGraph workflow in the agent package. This module keeps its original job: be the
seam the interface and the command line call, and return everything they display. The
result keeps every key it had, so app.py's panels, the pipeline diagram, the stage labels
and the timing line are unchanged, and adds the per-task states, the workflow trace and the
token counts the new architecture produces.

Retrieval, SQL execution, validation and the model clients are the ones already in the
repository: agent.nodes calls scripts.db_module and config for all of them.
"""

import time

import config
from agent.graph import DEFAULT_MAX_ATTEMPTS, STAGE_TO_KEY, WorkflowError, run_turn
from agent.nodes import (
    STAGE_EXECUTE,
    STAGE_GENERATE,
    STAGE_RETRIEVE,
    STAGE_SUMMARISE,
    STAGE_UNDERSTAND,
)
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
    "available_models",
    "compare",
    "new_chat",
    "schema_overview",
]

# The interface draws these as a row of nodes and lights each one up as it happens. The keys
# match the keys used in the timings dictionary returned by answer().
PIPELINE = [
    {"key": "question", "title": "Question", "detail": "Plain language in"},
    {"key": "retrieve", "title": "Retrieve", "detail": "Ollama embedding, Qdrant search"},
    {"key": "generate", "title": "Write SQL", "detail": "Azure OpenAI with a tool call"},
    {"key": "execute", "title": "Query", "detail": "Postgres, read only"},
    {"key": "summarise", "title": "Answer", "detail": "Azure OpenAI writes the reply"},
]


def available_models():
    """Model names the configuration knows about, in declaration order."""
    return list(config.MODEL_CONFIGS.keys())


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
        "needs_restart": False,
        "kind": "error",
        "route": "",
        "clarification": None,
        "tasks": [],
        "trace": [],
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "llm_calls": 0},
    }


def answer(question, model_name=None, on_stage=None, session=None, max_attempts=DEFAULT_MAX_ATTEMPTS):
    """Answer one question and report everything that happened along the way.

    on_stage is called with a short label before each step so the caller can show progress.
    Failures are returned inside the result rather than raised, so the interface can render
    a message instead of a stack trace.

    session is the conversation this question belongs to. Passing one keeps context across
    turns; leaving it None gives the question an empty conversation of its own, which is what
    a caller that is not holding a chat wants.

    needs_restart is set when the shared database connection has been left unusable by an
    earlier failure. Once that happens every later question fails too, and only restarting
    the process clears it.
    """
    model_name = model_name or config.DEFAULT_MODEL
    result = _blank_result(model_name)
    started = time.perf_counter()

    try:
        result = run_turn(
            question,
            session=session,
            model=model_name,
            on_stage=on_stage,
            max_attempts=max_attempts,
        )
    except WorkflowError as exc:
        result["error"] = str(exc)
        result["needs_restart"] = exc.needs_restart
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["needs_restart"] = "InFailedSqlTransaction" in type(exc).__name__

    if not result.get("timings"):
        result["timings"] = {"total": time.perf_counter() - started}
    return result


def compare(question, models=None, on_progress=None, session=None):
    """Answer the same question with each model in turn.

    The models run one after another rather than at the same time. The database connection
    in scripts/db_module.py is a single module level object shared by everything, and
    psycopg2 connections are not safe to use from two places at once, so running the models
    concurrently would risk interleaving two queries on one connection.
    """
    models = models or available_models()
    results = {}
    for name in models:
        if on_progress:
            on_progress(name)
        # Each model gets its own conversation: a comparison is a standalone question, and
        # one model's findings must not become the other's context.
        results[name] = answer(question, name, session=session or new_chat())
    return results


def schema_overview():
    """Every table in the database with its columns and row count, read live.

    Nothing is hardcoded. This is the same information the indexer embeds, so it doubles as
    a way to see what the model is given to work with.
    """
    from psycopg2 import sql

    from scripts.db_module import get_connection, get_table_names

    tables = []
    with get_connection() as conn:
        for name in get_table_names(conn):
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT column_name, data_type
                       FROM information_schema.columns
                       WHERE table_schema = 'public' AND table_name = %s
                       ORDER BY ordinal_position""",
                    (name,),
                )
                columns = cur.fetchall()
                cur.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(name)))
                count = cur.fetchone()[0]
            tables.append({"table": name, "columns": columns, "rows": count})
    return tables
