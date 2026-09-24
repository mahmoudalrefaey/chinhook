import hashlib
import json
import psycopg2
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, Distance, VectorParams, Filter, FieldCondition, MatchValue

from config import (
    DATABASE_URL,
    QDRANT_URL,
    QDRANT_COLLECTION,
    EMBED_MODEL,
    EMBED_DIM,
    OLLAMA_HOST,
)

import ollama

ollama_client = ollama.Client(host=OLLAMA_HOST)
qdrant = QdrantClient(url=QDRANT_URL)
conn = psycopg2.connect(DATABASE_URL, sslmode="require")


# ---------- Embedding ----------
def embed(text: str) -> list[float]:
    resp = ollama_client.embeddings(model=EMBED_MODEL, prompt=text)
    return resp["embedding"]


# ---------- Schema Introspection ----------
def get_table_defs(conn, schema_name="public"):
    with conn.cursor() as cur:
        cur.execute(
            """ SELECT table_name, column_name, data_type
             FROM information_schema.columns
             WHERE table_schema = %s
             ORDER BY table_name, ordinal_position """,
            (schema_name,),
        )
        rows = cur.fetchall()

    tables = {}
    for table, col, dtype in rows:
        tables.setdefault(table, []).append(f"{col} ({dtype})")
    return [f'"{t}"({", ".join(cols)})' for t, cols in tables.items()]


def get_table_names(conn, schema_name="public"):
    with conn.cursor() as cur:
        cur.execute(
            """ SELECT table_name
             FROM information_schema.tables
             WHERE table_schema = %s AND table_type = 'BASE TABLE'
             ORDER BY table_name """,
            (schema_name,),
        )
        return [row[0] for row in cur.fetchall()]


# ---------- Fingerprinting ----------
def compute_table_fingerprint(conn, table_name: str) -> dict:
    """Compute fingerprint for a single table: row count + schema hash."""
    with conn.cursor() as cur:
        # Row count
        cur.execute(f'SELECT COUNT(*) FROM "{table_name}"')
        row_count = cur.fetchone()[0]

        # Schema hash (column definitions)
        cur.execute(
            """ SELECT column_name, data_type, is_nullable, column_default
             FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = %s
             ORDER BY ordinal_position """,
            (table_name,),
        )
        cols = cur.fetchall()
        schema_str = json.dumps(cols, sort_keys=True)
        schema_hash = hashlib.sha256(schema_str.encode()).hexdigest()[:16]

        # Data checksum (sample-based for large tables)
        if row_count > 10000:
            cur.execute(f'SELECT * FROM "{table_name}" TABLESAMPLE SYSTEM (10) LIMIT 1000')
        else:
            cur.execute(f'SELECT * FROM "{table_name}"')
        rows = cur.fetchall()
        data_str = json.dumps(rows, sort_keys=True, default=str)
        data_hash = hashlib.sha256(data_str.encode()).hexdigest()[:16]

    return {
        "table_name": table_name,
        "row_count": row_count,
        "schema_hash": schema_hash,
        "data_hash": data_hash,
    }


def get_db_fingerprint(conn) -> dict:
    """Get fingerprint for all tables in the database."""
    table_names = get_table_names(conn)
    fingerprint = {}
    for table_name in table_names:
        fingerprint[table_name] = compute_table_fingerprint(conn, table_name)
    return fingerprint


def get_qdrant_fingerprint() -> dict:
    """Extract stored fingerprint from Qdrant payload."""
    fingerprint = {}
    try:
        # Scroll through all points to get payload metadata
        offset = None
        while True:
            result = qdrant.scroll(
                collection_name=QDRANT_COLLECTION,
                limit=100,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            points, offset = result
            for point in points:
                payload = point.payload
                if "fingerprint" in payload:
                    fp = payload["fingerprint"]
                    fingerprint[fp["table_name"]] = fp
            if offset is None:
                break
    except Exception:
        pass
    return fingerprint


def compare_fingerprints(db_fp: dict, qdrant_fp: dict) -> tuple[bool, list[str]]:
    """Compare fingerprints. Returns (needs_reindex, changed_tables)."""
    changed = []
    all_tables = set(db_fp.keys()) | set(qdrant_fp.keys())

    for table in all_tables:
        db_entry = db_fp.get(table)
        qdrant_entry = qdrant_fp.get(table)

        if db_entry is None:
            changed.append(table)  # Table dropped
        elif qdrant_entry is None:
            changed.append(table)  # New table
        elif db_entry["schema_hash"] != qdrant_entry["schema_hash"]:
            changed.append(table)  # Schema changed
        elif db_entry["data_hash"] != qdrant_entry["data_hash"]:
            changed.append(table)  # Data changed
        elif db_entry["row_count"] != qdrant_entry["row_count"]:
            changed.append(table)  # Row count changed

    return len(changed) > 0, changed


# ---------- Indexing ----------
def ensure_collection():
    """Create Qdrant collection if it doesn't exist."""
    if not qdrant.collection_exists(QDRANT_COLLECTION):
        qdrant.create_collection(
            QDRANT_COLLECTION,
            vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
        )


def index_table(table_def: str, fingerprint: dict) -> PointStruct:
    """Create a point for a table with its fingerprint in payload."""
    table_name = fingerprint["table_name"]
    point_id = abs(hash(table_name)) % (2**31)
    return PointStruct(
        id=point_id,
        vector=embed(table_def),
        payload={"table_def": table_def, "fingerprint": fingerprint},
    )


def full_reindex():
    """Full re-index of all tables."""
    ensure_collection()
    table_defs = get_table_defs(conn)
    db_fp = get_db_fingerprint(conn)

    points = []
    for table_def in table_defs:
        # Extract table name from definition
        table_name = table_def.split('"')[1].split('"')[0]
        fingerprint = db_fp.get(table_name, {"table_name": table_name})
        points.append(index_table(table_def, fingerprint))

    qdrant.upsert(QDRANT_COLLECTION, points)
    print(f"Full re-index complete: {len(points)} tables")
    return len(points)


def incremental_reindex(changed_tables: list[str]):
    """Re-index only the changed tables."""
    ensure_collection()
    table_defs = get_table_defs(conn)
    db_fp = get_db_fingerprint(conn)

    # Build lookup for table definitions
    defs_by_table = {}
    for td in table_defs:
        tn = td.split('"')[1].split('"')[0]
        defs_by_table[tn] = td

    points = []
    for table_name in changed_tables:
        if table_name in defs_by_table:
            fingerprint = db_fp.get(table_name, {"table_name": table_name})
            points.append(index_table(defs_by_table[table_name], fingerprint))
        else:
            # Table was dropped - delete from Qdrant
            point_id = abs(hash(table_name)) % (2**31)
            qdrant.delete(QDRANT_COLLECTION, [point_id])

    if points:
        qdrant.upsert(QDRANT_COLLECTION, points)
        print(f"Incremental re-index complete: {len(points)} tables updated")
    return len(points)


def check_and_index():
    """Check if re-indexing is needed and perform it."""
    db_fp = get_db_fingerprint(conn)
    qdrant_fp = get_qdrant_fingerprint()

    needs_reindex, changed = compare_fingerprints(db_fp, qdrant_fp)

    if not needs_reindex:
        print("Index is up to date. No changes detected.")
        return 0

    if len(changed) == len(db_fp) or not qdrant_fp:
        print("Full re-index needed")
        return full_reindex()
    else:
        print(f"Incremental re-index needed for: {changed}")
        return incremental_reindex(changed)


def get_relevant_schema(question: str, top_k=5) -> str:
    """Retrieve relevant table definitions for a question."""
    hits = qdrant.query_points(
        collection_name=QDRANT_COLLECTION,
        query=embed(question),
        limit=top_k,
    ).points
    return "\n".join(h.payload["table_def"] for h in hits)


# ---------- SQL Execution ----------
def validate_sql(query: str) -> bool:
    import sqlglot
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


# ---------- Tools ----------
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
    check_and_index()
    question = input("Ask a question about the database: ")
    schema_context = get_relevant_schema(question)
    print(schema_context)