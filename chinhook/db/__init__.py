"""Database access, split by what it is for.

    clients         per-database connection pools, the Qdrant client, the embedder
    dialects        what differs between Postgres and MySQL
    introspection   what tables, columns and foreign keys the database has
    fingerprinting  whether the index still matches the database's shape
    evidence        a plain description of what each table actually represents
    values          which columns are worth indexing by value, and what they hold
    indexing        writing tables and values into the vector store, and its status
    retention       how long an index is kept after it was last used
    retrieval       the table definitions and values a question is answered from
    sql             running a read-only query, and the tool the model is given
"""
