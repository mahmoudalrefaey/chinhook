"""Running a read-only query, and the tool definition the model is given to ask for one.

Two layers enforce "read-only", not one. The database connection itself uses a role that
cannot write, wherever that role has been created (see docs/READ_ONLY_ROLE.md), which is the
layer that actually guarantees it. validate_sql below is the second layer, checked before a
query is even sent: it used to look only at the outermost statement type, which let a
SELECT built around a CTE that deletes, updates or inserts through unnoticed, since the
statement as a whole still parses as a SELECT. It now walks the entire parsed query, not
just its root, so nothing written inside a CTE, a subquery or anywhere else escapes it.
"""

import uuid

import sqlglot
from sqlglot import exp

from scripts.db.clients import get_connection

STATEMENT_TIMEOUT_MS = 3000
MAX_ROWS = 500

# Keys sqlglot assigns to statement types that write or change something, or that are not a
# plain query at all. Checked against every node in the tree, not only the root, so a
# CTE that deletes and then selects from itself does not read as a plain SELECT overall.
_FORBIDDEN_KEYS = {
    "insert", "update", "delete", "create", "drop", "alter", "truncate", "merge",
    "into", "command", "set", "copy", "grant", "revoke", "transaction",
}

# Functions with no legitimate place in a read-only question about the data: reading files
# or directories off the server, talking to another database, pausing the connection, or
# mutating a sequence. None of these are blocked by "read-only" in the ordinary SQL sense,
# since Postgres itself classifies them as callable from a SELECT.
_FORBIDDEN_FUNCTIONS = {
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_ls_logdir", "pg_ls_waldir",
    "lo_import", "lo_export", "lo_get", "lo_put",
    "dblink", "dblink_connect", "dblink_exec",
    "pg_sleep", "pg_sleep_for", "pg_sleep_until",
    "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf",
    "set_config", "setval", "nextval",
    "pg_advisory_lock", "pg_advisory_xact_lock",
}


def validate_sql(query: str) -> tuple[bool, str]:
    """Whether a query is a single, plain, read-only SELECT.

    Returns (True, "") when it is, or (False, reason) otherwise. A query that fails to parse
    at all is rejected the same way as one that parses into something disallowed, rather than
    letting sqlglot's own parse error escape to the caller.
    """
    try:
        statements = [s for s in sqlglot.parse(query, read="postgres") if s is not None]
    except Exception as exc:  # noqa: BLE001
        return False, f"could not parse the query: {exc}"

    if len(statements) != 1:
        return False, "only a single statement is allowed"

    statement = statements[0]
    if statement.key != "select":
        return False, "only SELECT queries are allowed"

    for node in statement.walk():
        current = node[0] if isinstance(node, tuple) else node
        if current.key in _FORBIDDEN_KEYS:
            return False, f'"{current.key}" is not allowed inside a query'
        if isinstance(current, exp.Func):
            name = (current.name or "").lower()
            if name in _FORBIDDEN_FUNCTIONS:
                return False, f'the function "{name}" is not allowed'

    return True, ""


def run_sql_query(query: str):
    """Run a validated, read-only query and return its columns and rows.

    Uses a named (server-side) cursor rather than the default client-side one, so a query
    with no LIMIT of its own does not pull an entire large table into memory before this
    function gets the chance to cap it; only MAX_ROWS + 1 rows are ever actually fetched.
    Anything past that cap is dropped and the result says so, rather than silently returning
    a partial answer that looks complete.
    """
    ok, reason = validate_sql(query)
    if not ok:
        return {"error": reason}

    try:
        with get_connection() as connection:
            # A named cursor wraps every statement it is given as DECLARE ... CURSOR FOR
            # <statement>, which is exactly the incremental-fetch behaviour that bounds
            # memory below, but which also means the timeout cannot be set through the same
            # cursor: DECLARE CURSOR FOR SET LOCAL ... is not valid SQL. It goes through a
            # separate, ordinary cursor first, on the same connection and so the same
            # transaction, immediately before the named cursor opens.
            with connection.cursor() as setup:
                # LOCAL, not the session-wide setting the query path used before: this pool
                # hands the same physical connection to a different caller on its next
                # checkout, and a plain SET would otherwise leak this timeout, or whatever a
                # prior caller set, onto a query that never asked for it.
                setup.execute("SET LOCAL statement_timeout = %s", (STATEMENT_TIMEOUT_MS,))
            with connection.cursor(name=f"chinhook_{uuid.uuid4().hex}") as cur:
                cur.itersize = 200
                cur.execute(query)
                # A named cursor does not populate .description until something has
                # actually been fetched through it, unlike a plain cursor where it is set
                # the moment execute() returns. Checking it before this fetch always found
                # None and reported every query, however good, as having no result set.
                rows = cur.fetchmany(MAX_ROWS + 1)
                if cur.description is None:
                    return {"columns": [], "rows": []}
                cols = [d[0] for d in cur.description]
                truncated = len(rows) > MAX_ROWS
                result = {"columns": cols, "rows": rows[:MAX_ROWS]}
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
