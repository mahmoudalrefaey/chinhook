import os
import json
from openai import AzureOpenAI
from dotenv import load_dotenv

from scripts.db_module import (
    get_relevant_schema,
    run_sql_query,
    tools,
)

import config

load_dotenv()


# ---------- Chat Agent ----------
def chat_with_db(question: str, model_name: str = None) -> str:
    """Main chat function with model selection support."""
    if model_name is None:
        model_name = config.DEFAULT_MODEL

    schema_context = get_relevant_schema(question)

    messages = [
        {"role": "system", "content": (
            "You answer questions using this schema, querying a PostgreSQL database. "
            "Table and column names are case-sensitive — always wrap them in double "
            "quotes exactly as given below. Use PostgreSQL syntax only "
            "(e.g. CURRENT_DATE, NOW(), INTERVAL '7 days') — never SQLite or MySQL "
            "date functions like date('now', ...). "
            "When matching user-provided text values (names, titles, etc.) in WHERE "
            "clauses, use ILIKE instead of = or LIKE so matching is case-insensitive "
            "— the user may type a value in any case. This case-insensitive rule "
            "applies only to data values, never to table or column identifiers.\n"
            f"{schema_context}"
        )},
        {"role": "user", "content": question},
    ]

    client = config.create_azure_client(model_name)
    deployment = config.get_model_config(model_name)["deployment"]

    resp = client.chat.completions.create(
        model=deployment,
        messages=messages,
        tools=tools,
        temperature=0,
    )
    msg = resp.choices[0].message

    if msg.tool_calls:
        messages.append(msg)
        for call in msg.tool_calls:
            args = json.loads(call.function.arguments)
            result = run_sql_query(args["query"])
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": str(result)
            })

        final = client.chat.completions.create(
            model=deployment,
            messages=messages,
            temperature=0,
        )
        return final.choices[0].message.content

    return msg.content


def ask(question: str, model_name: str = None) -> str:
    """Main entry point for querying the database with natural language."""
    return chat_with_db(question, model_name)


def generate_response(user_query: str, db_context: str, model_name: str = None) -> str:
    """Generate a response using Azure OpenAI with database context."""
    if model_name is None:
        model_name = config.DEFAULT_MODEL

    client = config.create_azure_client(model_name)
    deployment = config.get_model_config(model_name)["deployment"]

    response = client.chat.completions.create(
        messages=[
            {
                "role": "system",
                "content": f"You are a helpful assistant. Here is the relevant data from the database to answer user queries: {db_context}",
            },
            {
                "role": "user",
                "content": user_query,
            },
        ],
        max_completion_tokens=13107,
        temperature=0.0,
        top_p=1.0,
        frequency_penalty=0.0,
        presence_penalty=0.0,
        model=deployment,
    )
    return response.choices[0].message.content


def get_available_models() -> list[str]:
    """Return list of available model names."""
    return list(config.MODEL_CONFIGS.keys())


if __name__ == "__main__":
    question = input("Ask a question about the database: ")
    model = input(f"Model ({', '.join(get_available_models())}) [gpt-4.1-nano]: ").strip() or "gpt-4.1-nano"
    print(ask(question, model))