import os
import psycopg2
from openai import AzureOpenAI
from dotenv import load_dotenv
from scripts.query_db import ask

load_dotenv()

endpoint = os.getenv("ENDPOINT_URL")
deployment = os.getenv("DEPLOYMENT_NAME")
model_name = os.getenv("MODEL_NAME")
subscription_key = os.getenv("OPENAI_KEY")
api_version = "2024-12-01-preview"

client = AzureOpenAI(
    api_version=api_version,
    azure_endpoint=endpoint,
    api_key=subscription_key,
)

def generate_response(user_query: str, db_context: str):
    response = client.chat.completions.create(
        messages=[
            {
                "role": "system",
                "content": f"You are a helpful assistant. Here is the relevant data from the database to answer user queries: {db_context}",
            },
            {
                "role": "user",
                "content": user_query,
            }
        ],
        max_completion_tokens=13107,
        temperature=0.0,
        top_p=1.0,
        frequency_penalty=0.0,
        presence_penalty=0.0,
        model=deployment
    )

    return(response.choices[0].message.content)