"""Running a read-only query, and the tool definition the model is given to ask for one."""

from scripts.db.clients import get_connection


def validate_sql(query: str) -> bool:
    import sqlglot
    parsed = sqlglot.parse_one(query, read="postgres")
    return parsed.key.upper() == "SELECT"


def run_sql_query(query: str):
    if not validate_sql(query):
        return {"error": "Only SELECT queries are allowed."}

    connection = get_connection()
    try:
        with connection.cursor() as cur:
            cur.execute("SET statement_timeout = 3000;")
            cur.execute(query)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchmany(50)
            return {"columns": cols, "rows": rows}
    except Exception as e:
        # conn is a single connection shared by every caller. Without this rollback, a
        # failed query left it in an aborted transaction and every query after it failed
        # too, for anyone, until the process was restarted.
        try:
            connection.rollback()
        except Exception:  # noqa: BLE001
            # The connection itself is gone, so the next call reconnects.
            pass
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
