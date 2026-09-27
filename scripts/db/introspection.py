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
