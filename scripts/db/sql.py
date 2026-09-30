"""Running a read-only query, and the tool definition the model is given to ask for one.

Two layers enforce "read-only", not one. Every connection the app opens is read-only at the
database itself (see scripts/db/dialects.py), which is the layer that actually guarantees it,
whatever the login the user connected with is allowed to do. validate_sql below is the second
layer, checked before a query is even sent: it walks the entire parsed query, not just its
root, so nothing written inside a CTE, a subquery or anywhere else escapes it.
"""

from typing import Optional

import sqlglot
from sqlglot import exp

from scripts.db import dialects
from scripts.db.clients import dialect as current_dialect
from scripts.db.clients import get_connection

# A question's own query gets a tighter limit than the app's own catalog reads: it is the one
# thing here written by a model, against a database whose size nobody checked in advance.
STATEMENT_TIMEOUT_MS = 10000
MAX_ROWS = 500

# Keys sqlglot assigns to statement types that write or change something, or that are not a
# plain query at all. Checked against every node in the tree, not only the root, so a
# CTE that deletes and then selects from itself does not read as a plain SELECT overall.
_FORBIDDEN_KEYS = {
    "insert", "update", "delete", "create", "drop", "alter", "truncate", "merge",
    "into", "command", "set", "copy", "grant", "revoke", "transaction",
}


def validate_sql(query: str, dialect: Optional[dialects.Dialect] = None) -> tuple[bool, str]:
    """Whether a query is a single, plain, read-only SELECT.

    Returns (True, "") when it is, or (False, reason) otherwise. A query that fails to parse
    at all is rejected the same way as one that parses into something disallowed, rather than
    letting sqlglot's own parse error escape to the caller. dialect defaults to the current
    session's database.
    """
    dialect = dialect or current_dialect()
    try:
        statements = [s for s in sqlglot.parse(query, read=dialect.sqlglot) if s is not None]
    except Exception as exc:  # noqa: BLE001
        return False, f"could not parse the query: {exc}"

    if len(statements) != 1:
        return False, "only a single statement is allowed"

    statement = statements[0]
    # UNION, INTERSECT and EXCEPT combine two SELECTs into one read-only result set, so they
    # are allowed at the top level alongside a plain SELECT. Anything written or mutating
    # inside either branch, including inside a CTE either one draws from, is still caught
    # below: the walk covers the whole tree, not just this top node.
    if statement.key not in {"select", "union", "intersect", "except"}:
        return False, "only SELECT queries are allowed"

    for node in statement.walk():
        current = node[0] if isinstance(node, tuple) else node
        if current.key in _FORBIDDEN_KEYS:
            return False, f'"{current.key}" is not allowed inside a query'
        if isinstance(current, exp.Func):
            name = (current.name or "").lower()
            if not name and isinstance(current, exp.Anonymous):
                name = str(current.this or "").lower()
            if name in dialect.forbidden_functions:
                return False, f'the function "{name}" is not allowed'

    return True, ""


def run_sql_query(query: str):
    """Run a validated, read-only query and return its columns and rows.

    Read through a server-side cursor where the database has one, so a query with no LIMIT of
    its own does not pull an entire large table into memory before this function gets the
    chance to cap it; only MAX_ROWS + 1 rows are ever actually fetched. Anything past that
    cap is dropped and the result says so, rather than silently returning a partial answer
    that looks complete.

    The query is executed with no parameters at all, so a % in a LIKE pattern is sent as the
    literal character it is rather than read by the driver as a placeholder.
    """
    dialect = current_dialect()
    ok, reason = validate_sql(query, dialect)
    if not ok:
        return {"error": reason}

    try:
        with get_connection() as connection:
            # Set through an ordinary cursor first, in the same transaction, immediately
            # before the streaming cursor opens: a server-side cursor wraps whatever it is
            # given, and a SET is not something it can wrap.
            with connection.cursor() as setup:
                dialect.set_timeout(setup, STATEMENT_TIMEOUT_MS)
            cur = dialect.streaming_cursor(connection)
            rows: list = []
            try:
                cur.execute(query)
                # A server-side cursor does not populate .description until something has
                # actually been fetched through it, unlike a plain cursor where it is set
                # the moment execute() returns.
                rows = cur.fetchmany(MAX_ROWS + 1)
                if cur.description is None:
                    return {"columns": [], "rows": []}
                cols = [d[0] for d in cur.description]
            finally:
                dialect.finish_streaming(cur, connection, exhausted=len(rows) <= MAX_ROWS)
            truncated = len(rows) > MAX_ROWS
            result = {"columns": cols, "rows": [tuple(row) for row in rows[:MAX_ROWS]]}
            if truncated:
                result["truncated"] = True
            return result
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


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
