"""Database access, split by what it is for.

The modules here are the parts of what used to be scripts/db_module.py:

    clients         the pooled Postgres connections, the Qdrant client, the embedder
    introspection   what tables and columns the database has
    fingerprinting  whether the index still matches the database
    evidence        a plain description of what each table actually represents
    indexing        writing the schema into the vector store
    retrieval       the table definitions a question is answered from
    sql             running a read-only query, and the tool the model is given

Everything that used to import from scripts.db_module still can: that module is now a thin
re-export of this package, so callers outside this folder were not touched.
"""

from scripts.db.clients import embed, get_connection, ollama_client, qdrant
from scripts.db.evidence import generate_evidence, get_sample_rows
from scripts.db.fingerprinting import (
    compare_fingerprints,
    compute_table_fingerprint,
    get_db_fingerprint,
    get_qdrant_fingerprint,
)
from scripts.db.indexing import (
    check_and_index,
    ensure_collection,
    full_reindex,
    incremental_reindex,
    index_table,
    stable_point_id,
)
from scripts.db.introspection import get_table_defs, get_table_names
from scripts.db.retrieval import SMALL_SCHEMA_THRESHOLD, get_relevant_schema
from scripts.db.sql import run_sql_query, tools, validate_sql

__all__ = [
    "SMALL_SCHEMA_THRESHOLD",
    "check_and_index",
    "compare_fingerprints",
    "compute_table_fingerprint",
    "embed",
    "ensure_collection",
    "full_reindex",
    "generate_evidence",
    "get_connection",
    "get_db_fingerprint",
    "get_qdrant_fingerprint",
    "get_relevant_schema",
    "get_sample_rows",
    "get_table_defs",
    "get_table_names",
    "index_table",
    "incremental_reindex",
    "ollama_client",
    "qdrant",
    "run_sql_query",
    "stable_point_id",
    "tools",
    "validate_sql",
]
