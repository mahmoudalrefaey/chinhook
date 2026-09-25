"""Answer pipeline for the web interface.

This repeats the flow that scripts/generator.py performs, because the interface needs to
display two values that the original flow keeps to itself: the schema that was retrieved
from Qdrant, and the SQL that the model wrote. Both are local variables inside
chat_with_db(), which returns only the final text, so they cannot be read from outside.

The retrieval, SQL execution and tool definitions all come from scripts.db_module. Only the
orchestration is repeated here. Nothing in scripts/ or config.py is read differently or
modified.

Behaviour is kept identical to the command line version on purpose, including leaving the
temperature unset on the model call.
"""

import json
import time

import config
from scripts.db_module import get_relevant_schema, run_sql_query, tools

# Copied exactly as it appears in scripts/generator.py so the model receives the same
# instructions it receives on the command line. Altering the wording here, including the
# punctuation, would change how the model writes SQL and the two paths would drift apart.
SYSTEM_PROMPT = (
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

STAGE_RETRIEVE = "Finding relevant tables"
STAGE_GENERATE = "Writing the query"
STAGE_EXECUTE = "Running it against the database"
STAGE_SUMMARISE = "Putting the answer together"


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
    }


def answer(question, model_name=None, on_stage=None):
    """Answer one question and report everything that happened along the way.

    on_stage is called with a short label before each step so the caller can show progress.
    Failures are returned inside the result rather than raised, so the interface can render
    a message instead of a stack trace.

    needs_restart is set when the shared database connection has been left in an aborted
    transaction by an earlier failure. Once that happens every later question fails too,
    and only restarting the process clears it.
    """
    model_name = model_name or config.DEFAULT_MODEL
    result = _blank_result(model_name)
    started = time.perf_counter()

    def stage(label):
        if on_stage:
            on_stage(label)

    try:
        stage(STAGE_RETRIEVE)
        mark = time.perf_counter()
        schema_context = get_relevant_schema(question)
        result["schema"] = schema_context
        result["timings"]["retrieve"] = time.perf_counter() - mark

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT + schema_context},
            {"role": "user", "content": question},
        ]

        client = config.create_azure_client(model_name)
        deployment = config.get_model_config(model_name)["deployment"]

        stage(STAGE_GENERATE)
        mark = time.perf_counter()
        response = client.chat.completions.create(
            model=deployment,
            messages=messages,
            tools=tools,
        )
        result["timings"]["generate"] = time.perf_counter() - mark

        message = response.choices[0].message

        # No tool call means the model answered from the schema alone without querying.
        if not message.tool_calls:
            result["answer"] = message.content
            result["ok"] = True
            result["timings"]["total"] = time.perf_counter() - started
            return result

        messages.append(message)

        stage(STAGE_EXECUTE)
        mark = time.perf_counter()
        for call in message.tool_calls:
            arguments = json.loads(call.function.arguments)
            query = arguments["query"]
            result["sql"] = query
            query_result = run_sql_query(query)

            if isinstance(query_result, dict):
                if "columns" in query_result:
                    result["columns"] = query_result["columns"]
                    result["rows"] = query_result["rows"]
                elif "error" in query_result:
                    result["error"] = query_result["error"]

            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": str(query_result),
            })
        result["timings"]["execute"] = time.perf_counter() - mark

        stage(STAGE_SUMMARISE)
        mark = time.perf_counter()
        final = client.chat.completions.create(model=deployment, messages=messages)
        result["timings"]["summarise"] = time.perf_counter() - mark

        result["answer"] = final.choices[0].message.content
        result["ok"] = True

    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["needs_restart"] = "InFailedSqlTransaction" in type(exc).__name__

    result["timings"]["total"] = time.perf_counter() - started
    return result
