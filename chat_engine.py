"""Answer pipeline for the web interface and the command line.

The reasoning that used to live in this file, and separately in scripts/generator.py, now
runs as a LangGraph workflow in the agent package. This module keeps its original job: be the
seam the interface and the command line call, and return everything they display, and adds
the per-task states, the workflow trace and the token counts the workflow produces.

Retrieval, SQL execution, validation and the model clients are the ones already in the
repository: agent.nodes and agent.task_graph call scripts.db_module and config for all of
them.
"""

import time
from concurrent.futures import ThreadPoolExecutor

import config
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
    "available_models",
    "compare",
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
    {"key": "retrieve", "title": "Retrieve", "detail": "Azure embedding, Qdrant search"},
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
    model_name = model_name or config.DEFAULT_MODEL
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


def compare(question, models=None, session=None):
    """Answer the same question with every model at once, each in its own thread.

    scripts.db.clients hands out a separate pooled connection per query rather than one
    connection shared by everything, so two models running at the same time never touch the
    same connection.
    """
    models = models or available_models()
    with ThreadPoolExecutor(max_workers=len(models)) as pool:
        # Each model gets its own conversation: a comparison is a standalone question, and
        # one model's findings must not become the other's context.
        futures = {
            name: pool.submit(answer, question, name, session=session or new_chat())
            for name in models
        }
        return {name: futures[name].result() for name in models}


def schema_overview():
    """Every table in the database with its columns and an approximate row count.

    The count comes from the planner's own pg_class.reltuples estimate, kept current by
    ANALYZE at index time (see scripts/db/indexing.py), not a live COUNT(*) per table: on a
    database sized in the millions of rows, a COUNT(*) per table would make this overview
    itself the slow thing to load. Nothing is hardcoded; every query here reads the database's
    own catalogs, the same ones the indexer reads.
    """
    from scripts.db_module import get_connection, get_table_names

    with get_connection() as conn:
        names = get_table_names(conn)
        with conn.cursor() as cur:
            cur.execute(
                """SELECT c.relname, GREATEST(c.reltuples, 0)::bigint
                   FROM pg_class c
                   JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname = 'public' AND c.relkind = 'r'"""
            )
            row_estimates = dict(cur.fetchall())

            cur.execute(
                """SELECT table_name, column_name, data_type
                   FROM information_schema.columns
                   WHERE table_schema = 'public'
                   ORDER BY table_name, ordinal_position"""
            )
            columns_by_table: dict[str, list[tuple[str, str]]] = {}
            for table, column, dtype in cur.fetchall():
                columns_by_table.setdefault(table, []).append((column, dtype))

    return [
        {
            "table": name,
            "columns": columns_by_table.get(name, []),
            "rows": int(row_estimates.get(name, 0)),
        }
        for name in names
    ]
