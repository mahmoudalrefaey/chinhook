"""Command line entry point into the query workflow.

The flow that used to be written out here, retrieving the schema and then asking the model
to write and run a single query, now runs in the agent package as a LangGraph workflow, and
this module calls it. The two entry points therefore take exactly the same path: the command
line gets the same task decomposition, verification and repair the web interface does, and
there is no second pipeline left to drift out of step.

Retrieval, execution and validation still come from scripts.db_module, and the model clients
from config, exactly as before.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.graph import run_turn
from agent.session import new_chat

# Re-exported so anything that used to import the database tools from this module still
# resolves them. The implementations are unchanged and still live in scripts.db_module.
from scripts.db_module import get_relevant_schema, run_sql_query, tools  # noqa: F401

import config


def chat_with_db(question: str, model_name: str = None, session=None) -> str:
    """Answer one question and return the text.

    Without a session the question is answered on its own, which is what a single call wants.
    The command line in main.py passes one session for the life of the chat instead.
    """
    if model_name is None:
        model_name = config.DEFAULT_MODEL
    result = run_turn(question, session=session, model=model_name)
    if result.get("answer"):
        return result["answer"]
    return f"Error: {result.get('error')}"


def ask(question: str, model_name: str = None, session=None) -> str:
    """Main entry point for querying the database with natural language."""
    return chat_with_db(question, model_name, session=session)


def ask_detailed(question: str, model_name: str = None, session=None) -> dict:
    """Same as ask, but with the full result: tasks, trace, timings and token usage."""
    if model_name is None:
        model_name = config.DEFAULT_MODEL
    return run_turn(question, session=session, model=model_name)


def get_available_models() -> list[str]:
    """Return list of available model names."""
    return list(config.MODEL_CONFIGS.keys())


if __name__ == "__main__":
    question = input("Ask a question about the database: ")
    model = input(f"Model ({', '.join(get_available_models())}) [gpt-4.1-nano]: ").strip() or "gpt-4.1-nano"
    print(ask(question, model, session=new_chat()))
