import os
import json
from openai import AzureOpenAI
from dotenv import load_dotenv
from scripts.db_module import (
    get_relevant_schema,
    run_sql_query,
    tools,
    conn,
)

load_dotenv()

# ---------- Azure OpenAI Configuration ----------
AZURE_OPENAI_KEY = os.environ["AZURE_OPENAI_KEY"]
AZURE_OPENAI_ENDPOINT = os.environ["AZURE_OPENAI_ENDPOINT"]
DEPLOYMENT_NAME = os.environ["DEPLOYMENT_NAME"]
MODEL_NAME = os.environ["MODEL_NAME"]

azure_client = AzureOpenAI(
    api_key=AZURE_OPENAI_KEY,
    api_version="2024-10-21",
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
)


# ---------- Chat Agent ----------
def chat_with_db(question: str) -> str:
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

    resp = azure_client.chat.completions.create(
        model=DEPLOYMENT_NAME,
        messages=messages,
        tools=tools,
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

        final = azure_client.chat.completions.create(
            model=DEPLOYMENT_NAME,
            messages=messages,
        )
        return final.choices[0].message.content

    return msg.content


def ask(question: str) -> str:
    """Main entry point for querying the database with natural language."""
    return chat_with_db(question)


def generate_response(user_query: str, db_context: str) -> str:
    """Generate a response using Azure OpenAI with database context."""
    response = azure_client.chat.completions.create(
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
        model=DEPLOYMENT_NAME,
    )
    return response.choices[0].message.content


if __name__ == "__main__":
    question = input("Ask a question about the database: ")
    print(ask(question))