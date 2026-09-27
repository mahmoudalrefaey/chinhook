"""The clients the database tooling shares, and the one that owns them.

There is one Postgres connection and one Qdrant client for the whole process, created once
here and imported by everything else. Keeping them in one place is what stops a split of
this module turning into a second connection, which is the failure that was already fixed
here once.
"""

import psycopg2
import ollama
from qdrant_client import QdrantClient

from config import DATABASE_URL, QDRANT_URL, EMBED_MODEL, OLLAMA_HOST

ollama_client = ollama.Client(host=OLLAMA_HOST)
qdrant = QdrantClient(url=QDRANT_URL)
conn = psycopg2.connect(DATABASE_URL, sslmode="require")


def get_connection():
    """The shared connection, replaced if it has gone away.

    conn is module level and shared by everything, and a proxy in front of a hosted
    database closes idle connections on its own schedule. Once psycopg2 notices, every
    later query from every caller raises "connection already closed" until the process is
    restarted, and a long chat hits that. Reading through this helper means a dead
    connection costs one reconnect instead of the rest of the session.
    """
    global conn
    if conn.closed:
        conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    return conn


def embed(text: str) -> list[float]:
    resp = ollama_client.embeddings(model=EMBED_MODEL, prompt=text)
    return resp["embedding"]
