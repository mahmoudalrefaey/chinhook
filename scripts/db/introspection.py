"""What tables the database has, and what columns they hold."""


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


def get_foreign_keys(conn, schema_name="public") -> dict[str, set[str]]:
    """Which tables a table is joined to by a real foreign key, in both directions.

    A join path between two retrieved tables is what makes a schema slice actually
    answerable rather than just topically related: a question about artists and sales needs
    Album, Track and InvoiceLine in between even though neither the words "album" nor "track"
    appear in the question. The relationship is read straight from pg_constraint, not
    inferred from column names, and each pair is added both ways so the graph can be walked
    from either side without special-casing which table happens to hold the constraint.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT c.conrelid::regclass::text, c.confrelid::regclass::text
               FROM pg_constraint c
               JOIN pg_namespace n ON n.oid = c.connamespace
               WHERE c.contype = 'f' AND n.nspname = %s""",
            (schema_name,),
        )
        rows = cur.fetchall()

    neighbours: dict[str, set[str]] = {}
    for left, right in rows:
        neighbours.setdefault(left, set()).add(right)
        neighbours.setdefault(right, set()).add(left)
    return neighbours
