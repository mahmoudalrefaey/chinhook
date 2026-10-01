"""What tables the database has, what columns they hold, and how they join.

Everything here reads the connected schema only (runtime.DatabaseSettings.schema), and treats
a view exactly like a table: it is listed, described, fingerprinted and indexed the same way,
because a question can be answered from one just as well. Listing views in one place and not
another is what used to leave a view permanently "changed", re-indexed on every check.

Each function is one query for the whole schema rather than one per table, so reading the
catalog of a remote database costs a few round trips regardless of how many tables it has.
"""

from __future__ import annotations

from chinhook.db.clients import dialect as current_dialect
from chinhook.db.clients import schema as current_schema

_KINDS = {"BASE TABLE": "table", "VIEW": "view"}


def get_tables(conn, schema: str | None = None) -> dict[str, str]:
    """Table and view name -> "table" or "view", in name order."""
    schema = schema or current_schema()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT table_name, table_type
               FROM information_schema.tables
               WHERE table_schema = %s AND table_type IN ('BASE TABLE', 'VIEW')
               ORDER BY table_name""",
            (schema,),
        )
        return {name: _KINDS[kind] for name, kind in cur.fetchall()}


def get_columns(conn, schema: str | None = None) -> dict[str, list[tuple[str, str, str, str]]]:
    """Table name -> [(column, data type, nullable, default)], in column order."""
    schema = schema or current_schema()
    tables = get_tables(conn, schema)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT table_name, column_name, data_type, is_nullable, column_default
               FROM information_schema.columns
               WHERE table_schema = %s
               ORDER BY table_name, ordinal_position""",
            (schema,),
        )
        rows = cur.fetchall()
    columns: dict[str, list[tuple[str, str, str, str]]] = {name: [] for name in tables}
    for table, column, dtype, nullable, default in rows:
        if table in columns:
            columns[table].append((column, str(dtype).lower(), str(nullable), str(default)))
    return columns


def get_catalog(conn, schema: str | None = None) -> dict[str, list[tuple[str, str]]]:
    """Table name -> [(column, data type)]: what a prompt or a validity check needs."""
    return {
        table: [(column, dtype) for column, dtype, _nullable, _default in columns]
        for table, columns in get_columns(conn, schema).items()
    }


def get_table_defs(conn, schema: str | None = None) -> list[str]:
    """One line per table, the form both the index and the prompts use:
    "name"(column (type), column (type), ...)."""
    return [
        f'"{table}"({", ".join(f"{column} ({dtype})" for column, dtype in columns)})'
        for table, columns in get_catalog(conn, schema).items()
    ]


def get_table_names(conn, schema: str | None = None) -> list[str]:
    return list(get_tables(conn, schema))


def get_foreign_keys(conn, schema: str | None = None) -> dict[str, set[str]]:
    """Which tables a table is joined to by a real foreign key, in both directions.

    A join path between two retrieved tables is what makes a schema slice actually
    answerable rather than just topically related: a question about artists and sales needs
    the tables in between even though the question never names them. The relationship is
    read from the database's own constraint catalog, not inferred from column names, and
    each pair is added both ways so the graph can be walked from either side.

    Names come back bare, exactly as get_tables lists them, never quoted or qualified with
    their schema, so a mixed-case table such as "Order" is recognised as the same table
    everywhere else.
    """
    schema = schema or current_schema()
    with conn.cursor() as cur:
        cur.execute(current_dialect().foreign_keys_sql, (schema, schema))
        rows = cur.fetchall()

    neighbours: dict[str, set[str]] = {}
    for left, right in rows:
        if left == right:
            continue
        neighbours.setdefault(left, set()).add(right)
        neighbours.setdefault(right, set()).add(left)
    return neighbours


def get_row_estimates(conn, schema: str | None = None, tables: dict[str, str] | None = None) -> dict[str, int]:
    """An approximate row count per table, from the planner's statistics.

    A table the planner has never analysed reports 0 there. For those tables only, and never
    for a view (whose count could mean running the whole view), a bounded count stands in:
    it stops at a cap, so it costs the same on a table of a thousand rows or a billion. The
    app cannot refresh the statistics itself: every connection it opens is read-only.
    """
    schema = schema or current_schema()
    tables = tables if tables is not None else get_tables(conn, schema)
    dialect = current_dialect()
    estimates = dialect.row_estimates(conn, schema)
    result: dict[str, int] = {}
    for table, kind in tables.items():
        estimate = estimates.get(table, 0)
        if estimate <= 0 and kind == "table":
            estimate = _bounded_count(conn, schema, table)
        result[table] = estimate
    return result


_COUNT_CAP = 10000


def _bounded_count(conn, schema: str, table: str) -> int:
    dialect = current_dialect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) FROM (SELECT 1 FROM {dialect.qualified(schema, table)} "
                f"LIMIT {_COUNT_CAP}) AS limited_rows"
            )
            (count,) = cur.fetchone()
            return int(count or 0)
    except Exception:  # noqa: BLE001
        restart(conn)
        return 0


def restart(conn) -> None:
    """Recover a borrowed connection after one of its statements failed.

    A failed statement aborts the whole transaction on Postgres, so every query after it
    would fail too. Rolling back and starting the read-only transaction again lets the
    caller carry on with the next table or column rather than giving up on all of them.
    """
    from chinhook.db import dialects

    conn.rollback()
    with conn.cursor() as cur:
        current_dialect().start(cur, dialects.DEFAULT_TIMEOUT_MS, current_schema())
