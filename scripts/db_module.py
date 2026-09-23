import json
import os
import psycopg2
import sqlglot
import ollama
from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, Distance, VectorParams

load_dotenv()

# ---------- Config ----------
COLLECTION = "schema_tables"
EMBED_MODEL = "nomic-embed-text"
EMBED_DIM = 768

ollama_client = ollama.Client(host="http://127.0.0.1:11434")
qdrant = QdrantClient(url="http://localhost:6333")

DATABASE_URL = os.environ["DATABASE_URL"]
conn = psycopg2.connect(DATABASE_URL, sslmode="require")


# ---------- 1. Introspect schema ----------
def get_table_defs(conn, schema_name="public"):
    with conn.cursor() as cur:
        cur.execute(
            """ SELECT table_name, column_name, data_type
             FROM information_schema.columns
             WHERE table_schema = %s
             ORDER BY table_name, ordinal_position """, (schema_name,))
        rows = cur.fetchall()

    tables = {}
    for table, col, dtype in rows:
        tables.setdefault(table, []).append(f"{col} ({dtype})")
    return [f'"{t}"({", ".join(cols)})' for t, cols in tables.items()]


# ---------- 2. Embed + index tables ----------
def embed(text: str):
    resp = ollama_client.embeddings(model=EMBED_MODEL, prompt=text)
    return resp["embedding"]


def index_schema(conn):
    table_defs = get_table_defs(conn)
    if qdrant.collection_exists(COLLECTION):
        qdrant.delete_collection(COLLECTION)

    qdrant.create_collection(
        COLLECTION,
        vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
    )
    points = [
        PointStruct(id=i, vector=embed(t), payload={"table_def": t})
        for i, t in enumerate(table_defs)
    ]
    qdrant.upsert(COLLECTION, points)
    print(f"Indexed {len(points)} tables.")


# ---------- 3. Retrieve relevant tables per question ----------
def get_relevant_schema(question: str, top_k=5):
    hits = qdrant.query_points(
        collection_name=COLLECTION,
        query=embed(question),
        limit=top_k,
    ).points
    return "\n".join(h.payload["table_def"] for h in hits)


# ---------- 4. SQL validation + execution ----------
def validate_sql(query: str) -> bool:
    parsed = sqlglot.parse_one(query, read="postgres")
    return parsed.key.upper() == "SELECT"


def run_sql_query(query: str):
    if not validate_sql(query):
        return {"error": "Only SELECT queries are allowed."}

    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = 3000;")
        cur.execute(query)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(50)
        return {"columns": cols, "rows": rows}


# ---------- Tools definition for OpenAI ----------
tools = [{
    "type": "function",
    "function": {
        "name": "run_sql_query",
        "description": "Run a read-only SQL SELECT against the database",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
}]


if __name__ == "__main__":
    index_schema(conn)
    question = input("Ask a question about the database: ")
    schema_context = get_relevant_schema(question)
    print(schema_context)