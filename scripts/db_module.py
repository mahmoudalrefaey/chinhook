import hashlib
import json
import psycopg2
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, Distance, VectorParams, Filter, FieldCondition, MatchValue

import config
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


# ---------- Automatic evidence generation ----------
#
# A table named "invoice_line" doesn't say anywhere that it represents a completed sale
# rather than, say, a price list or a playlist membership. Column names and types alone
# were not enough signal for the model to tell those apart, which is what let "top selling
# tracks" get answered by sorting on price instead of actual units sold.
#
# Rather than have a person write that distinction down once (which goes stale the moment
# the data changes, and has to be redone by hand for a different database entirely), a
# short description of what each table actually represents is generated automatically from
# its structure and a few of its real rows, at indexing time. It is regenerated automatically
# whenever a table's fingerprint changes, so it never goes stale and needs no code changes
# if the whole database were swapped for something else.

def get_sample_rows(conn, table_name: str, limit: int = 3) -> list[tuple]:
    """A few real rows from a table, used to ground the automatically generated evidence."""
    with conn.cursor() as cur:
        cur.execute(f'SELECT * FROM "{table_name}" LIMIT %s', (limit,))
        return cur.fetchall()


def generate_evidence(table_def: str, sample_rows: list[tuple]) -> str:
    """Infer what a table represents in the real world, from its structure and real data.

    Uses the nano deployment since this is a short, well-defined summarisation task, not
    something that needs the larger model's reasoning.
    """
    sample_text = "\n".join(str(row) for row in sample_rows) or "(table is currently empty)"
    prompt = (
        "You are documenting a database table for another AI that will write SQL queries "
        "against it. Given the table's structure and a few real rows, write ONE short "
        "sentence stating what this table actually represents in the real world. "
        "Specifically say whether it represents a completed transaction or event with "
        "measurable facts (a sale, a booking, a play), a reference or lookup table, or a "
        "pure relationship between two other tables with no facts of its own. Be concrete, "
        "not generic, and base this only on the structure and values shown, not the name.\n\n"
        f"Table definition: {table_def}\n"
        f"Sample rows:\n{sample_text}"
    )
    try:
        client = config.create_azure_client("gpt-4.1-nano")
        deployment = config.get_model_config("gpt-4.1-nano")["deployment"]
        resp = client.chat.completions.create(
            model=deployment,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_completion_tokens=120,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        # A transient failure generating evidence for one table should not fail the whole
        # index. The table still gets indexed, just without the extra context this run.
        print(f"Evidence generation failed for this table, indexing it without it: {e}")
        return ""


# ---------- Indexing ----------
def ensure_collection():
    """Create Qdrant collection if it doesn't exist."""
    if not qdrant.collection_exists(QDRANT_COLLECTION):
        qdrant.create_collection(
            QDRANT_COLLECTION,
            vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
        )


def stable_point_id(table_name: str) -> int:
    """Deterministic point id for a table, stable across process restarts.

    Python's built-in hash() is randomized per process (PYTHONHASHSEED), so the same table
    name produced a different id every run. That meant every re-index inserted a fresh
    duplicate of each table instead of updating the existing point, and retrieval quality
    degraded a little more with each restart as duplicates crowded out real variety.
    """
    return int(hashlib.sha256(table_name.encode()).hexdigest()[:8], 16)


def index_table(table_def: str, fingerprint: dict) -> PointStruct:
    """Create a point for a table with its fingerprint in payload.

    The stored table_def is enriched with automatically generated evidence about what the
    table represents, so both retrieval (which tables look relevant to a question) and the
    final SQL generation benefit from it, with no change needed in either of those places.
    """
    table_name = fingerprint["table_name"]
    point_id = stable_point_id(table_name)
    sample_rows = get_sample_rows(conn, table_name)
    evidence = generate_evidence(table_def, sample_rows)
    enriched_def = f"{table_def}\n  Represents: {evidence}" if evidence else table_def
    return PointStruct(
        id=point_id,
        vector=embed(enriched_def),
        payload={"table_def": enriched_def, "fingerprint": fingerprint},
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
            point_id = stable_point_id(table_name)
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


SMALL_SCHEMA_THRESHOLD = 25


def get_relevant_schema(question: str, top_k=5) -> str:
    """Table definitions relevant to a question.

    Retrieval only helps once a schema is too big to show the model in full. Below that, it
    is pure downside: similarity search can rank a table low for reasons that have nothing to
    do with whether it is actually needed, the way a literal word in the question ("tracks")
    pulled unrelated track-named tables above the one table that actually holds sales data.
    Below the threshold, every table is returned and nothing gets silently left out.
    """
    total = qdrant.count(QDRANT_COLLECTION).count

    if total <= SMALL_SCHEMA_THRESHOLD:
        points = qdrant.scroll(
            collection_name=QDRANT_COLLECTION,
            limit=total,
            with_payload=True,
            with_vectors=False,
        )[0]
        return "\n".join(p.payload["table_def"] for p in points)

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

    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = 3000;")
            cur.execute(query)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchmany(50)
            return {"columns": cols, "rows": rows}
    except Exception as e:
        # conn is a single connection shared by every caller. Without this rollback, a
        # failed query left it in an aborted transaction and every query after it failed
        # too, for anyone, until the process was restarted.
        conn.rollback()
        return {"error": str(e)}


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