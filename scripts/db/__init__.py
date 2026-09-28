"""Database access, split by what it is for.

    clients         the pooled Postgres connections, the Qdrant client, the embedder
    introspection   what tables, columns and foreign keys the database has
    fingerprinting  whether the index still matches the database's shape
    evidence        a plain description of what each table actually represents
    values          which columns are worth indexing by value, and what they hold
    indexing        writing tables and values into the vector store
    retrieval       the table definitions and values a question is answered from
    sql             running a read-only query, and the tool the model is given

Everything that used to import from scripts.db_module still can: that module is now a thin
re-export of this package, so callers outside this folder were not touched.
"""

from scripts.db.clients import embed, embed_batch, get_connection, qdrant
from scripts.db.evidence import generate_evidence, get_sample_rows
from scripts.db.fingerprinting import (
    compare_fingerprints,
    compute_table_fingerprint,
    get_db_fingerprint,
    get_qdrant_fingerprint,
)
from scripts.db.indexing import (
    check_and_index,
    ensure_collections,
    full_reindex,
    incremental_reindex,
    stable_point_id,
)
from scripts.db.introspection import get_foreign_keys, get_table_defs, get_table_names
from scripts.db.retrieval import (
    all_schema_text,
    expand_with_joins,
    get_relevant_schema,
    join_graph,
    retrieve_tables,
    search_values,
)
from scripts.db.sql import run_sql_query, tools, validate_sql
from scripts.db.values import low_cardinality_values

__all__ = [
    "all_schema_text",
    "check_and_index",
    "compare_fingerprints",
    "compute_table_fingerprint",
    "embed",
    "embed_batch",
    "ensure_collections",
    "expand_with_joins",
    "full_reindex",
    "generate_evidence",
    "get_connection",
    "get_db_fingerprint",
    "get_foreign_keys",
    "get_qdrant_fingerprint",
    "get_relevant_schema",
    "get_sample_rows",
    "get_table_defs",
    "get_table_names",
    "incremental_reindex",
    "join_graph",
    "low_cardinality_values",
    "qdrant",
    "retrieve_tables",
    "run_sql_query",
    "search_values",
    "stable_point_id",
    "tools",
    "validate_sql",
]
