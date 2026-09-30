"""What differs between the SQL databases the app can connect to, and nothing else.

Everything that is the same everywhere (reading tables, columns and foreign keys, pooling,
quoting) goes through SQLAlchemy; the few things that are not, and that no library papers
over, live here: how a connection is made read-only, how a query is given a time limit, how
a large result is read without pulling all of it into memory, where the planner keeps its row
estimates, which functions a query must never call, and how the model is told to write SQL
for this database.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy.dialects import mysql, postgresql

import runtime

# Seconds-scale ceiling on anything the app itself runs (introspection, index scans), set on
# every borrowed connection. A user's question gets a tighter one of its own; see
# scripts/db/sql.py.
DEFAULT_TIMEOUT_MS = 30000


@dataclass(frozen=True)
class Dialect:
    name: str
    sqlglot: str
    prompt_rules: str
    # (connected schema, connected schema) -> rows of (table, table it references), bare
    # names only, for foreign keys between two tables of the connected schema.
    foreign_keys_sql: str = ""
    # Data types a column must have to be considered for the value index.
    text_types: frozenset[str] = field(default_factory=frozenset)
    # Schemas a query may read besides the connected one: the database's own catalog, for a
    # question about the database itself.
    system_schemas: frozenset[str] = field(default_factory=frozenset)
    # Functions with no legitimate place in a read-only question about the data. None of these
    # are stopped by "read-only" in the ordinary SQL sense, since the database itself lets a
    # SELECT call them.
    forbidden_functions: frozenset[str] = field(default_factory=frozenset)

    # ---- connecting ----

    def connect_args(self, db: runtime.DatabaseSettings) -> dict:
        return {}

    def configure_session(self, dbapi_conn) -> None:
        """Run once on every new connection, before it is used for anything."""

    def start(self, cursor, timeout_ms: int, schema: str) -> None:
        """Run at the start of every borrow: read-only transaction, time limit, and the
        connected schema as the one an unqualified table name refers to."""

    def set_timeout(self, cursor, timeout_ms: int) -> None:
        """Tighten the time limit for the next statement on this borrowed connection."""

    def streaming_cursor(self, dbapi_conn):
        """A cursor that fetches rows from the server as they are asked for."""
        return dbapi_conn.cursor()

    def finish_streaming(self, cursor, dbapi_conn, exhausted: bool) -> None:
        """Close a streaming cursor, whether or not every row was read from it."""
        cursor.close()

    # ---- reading the catalog ----

    def row_estimates(self, conn, schema: str) -> dict[str, int]:
        """Planner row estimates per table, from the catalog: never a scan of the data."""
        return {}

    def quote(self, name: str) -> str:
        raise NotImplementedError

    def qualified(self, schema: str, table: str) -> str:
        return f"{self.quote(schema)}.{self.quote(table)}"


# ---------- PostgreSQL ----------

_POSTGRES_RULES = (
    "You answer questions using this schema, querying a PostgreSQL database. "
    "Table and column names are case-sensitive — always wrap them in double "
    "quotes exactly as given below. Use PostgreSQL syntax only "
    "(e.g. CURRENT_DATE, NOW(), INTERVAL '7 days') — never SQLite or MySQL "
    "date functions like date('now', ...). "
    "When matching user-provided text values (names, titles, etc.) in WHERE "
    "clauses, use ILIKE instead of = or LIKE so matching is case-insensitive "
    "— the user may type a value in any case. This case-insensitive rule "
    "applies only to data values, never to table or column identifiers.\n"
)

_postgres_preparer = postgresql.dialect().identifier_preparer


class _Postgres(Dialect):
    def connect_args(self, db: runtime.DatabaseSettings) -> dict:
        # sslmode travels in the URL itself (see connection.py); this only bounds how long an
        # unreachable host can keep the setup screen waiting.
        return {"connect_timeout": 10}

    def configure_session(self, dbapi_conn) -> None:
        # Every transaction on this connection now opens as BEGIN READ ONLY. Issued per
        # transaction by psycopg2 itself rather than as a session-wide SET, which is what
        # keeps it in force behind a transaction-mode pooler (Supabase, Neon), where each
        # transaction may land on a different server connection than the last.
        dbapi_conn.set_session(readonly=True)

    def start(self, cursor, timeout_ms: int, schema: str) -> None:
        # LOCAL: both end with this transaction, so neither can leak to the next borrower.
        cursor.execute("SET LOCAL statement_timeout = %s", (int(timeout_ms),))
        # The model writes table names unqualified, as the schema it was shown lists them, so
        # they have to resolve in the connected schema rather than in public. public stays
        # after it for the functions extensions usually install there.
        path = self.quote(schema) + ("" if schema == "public" else ", public")
        cursor.execute(f"SET LOCAL search_path TO {path}")

    def set_timeout(self, cursor, timeout_ms: int) -> None:
        cursor.execute("SET LOCAL statement_timeout = %s", (int(timeout_ms),))

    def streaming_cursor(self, dbapi_conn):
        # A named cursor is a server-side one: rows arrive as they are fetched, so a query
        # with no LIMIT of its own does not pull a whole table into memory first.
        cursor = dbapi_conn.cursor(name=f"chinhook_{uuid.uuid4().hex}")
        cursor.itersize = 200
        return cursor

    def row_estimates(self, conn, schema: str) -> dict[str, int]:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT c.relname, c.reltuples
                   FROM pg_class c
                   JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'v', 'm', 'f')""",
                (schema,),
            )
            # -1 means the planner has never analysed the table: unknown, reported as 0.
            return {name: max(0, int(rows or 0)) for name, rows in cur.fetchall()}

    def quote(self, name: str) -> str:
        return _postgres_preparer.quote_identifier(name)


POSTGRES = _Postgres(
    name=runtime.POSTGRES,
    sqlglot="postgres",
    prompt_rules=_POSTGRES_RULES,
    # From pg_constraint rather than information_schema: Postgres only shows a table's
    # constraints in information_schema to a user with more than SELECT on it, so a
    # read-only login would see no foreign keys at all there. Joined to pg_class by name
    # rather than cast through regclass, which quotes a mixed-case name ("Order") and so
    # never matched the bare name everything else uses.
    foreign_keys_sql="""SELECT src.relname, dst.relname
        FROM pg_constraint c
        JOIN pg_class src ON src.oid = c.conrelid
        JOIN pg_namespace ns ON ns.oid = src.relnamespace
        JOIN pg_class dst ON dst.oid = c.confrelid
        JOIN pg_namespace nd ON nd.oid = dst.relnamespace
        WHERE c.contype = 'f' AND ns.nspname = %s AND nd.nspname = %s""",
    text_types=frozenset({"text", "character varying", "character", "citext"}),
    system_schemas=frozenset({"information_schema", "pg_catalog"}),
    forbidden_functions=frozenset({
        "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_ls_logdir", "pg_ls_waldir",
        "lo_import", "lo_export", "lo_get", "lo_put",
        "dblink", "dblink_connect", "dblink_exec",
        "pg_sleep", "pg_sleep_for", "pg_sleep_until",
        "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf",
        "set_config", "setval", "nextval",
        "pg_advisory_lock", "pg_advisory_xact_lock",
    }),
)

# ---------- MySQL ----------

_MYSQL_RULES = (
    "You answer questions using this schema, querying a MySQL database. "
    "Table and column names are shown in double quotes below; in your query write each "
    "one exactly as given, wrapped in backticks (`name`), never in double quotes. "
    "Use MySQL syntax only (e.g. CURDATE(), NOW(), DATE_SUB(CURDATE(), INTERVAL 7 DAY), "
    "LIMIT n) — never PostgreSQL-only syntax such as ILIKE, :: casts or FULL OUTER JOIN. "
    "When matching user-provided text values (names, titles, etc.) in WHERE clauses, "
    "compare case-insensitively, e.g. LOWER(`column`) = LOWER('value') or "
    "LOWER(`column`) LIKE LOWER('%value%') — the user may type a value in any case.\n"
)

_mysql_preparer = mysql.dialect().identifier_preparer


class _MySQL(Dialect):
    def connect_args(self, db: runtime.DatabaseSettings) -> dict:
        args: dict = {"connect_timeout": 10, "charset": "utf8mb4"}
        if db.ssl_mode == "disable":
            args["ssl_disabled"] = True
        elif db.ssl_mode == "require":
            # Any non-empty ssl option makes PyMySQL refuse a server without TLS. The
            # certificate is not verified, the same meaning "require" has for Postgres.
            args["ssl"] = {"check_hostname": False}
        # "prefer" passes nothing: PyMySQL then uses TLS whenever the server offers it.
        return args

    def configure_session(self, dbapi_conn) -> None:
        with dbapi_conn.cursor() as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            cur.execute("SELECT VERSION()")
            (version,) = cur.fetchone()
        # MariaDB spells the statement time limit differently, and in seconds.
        dbapi_conn._chinhook_mariadb = "mariadb" in str(version).lower()

    def start(self, cursor, timeout_ms: int, schema: str) -> None:
        # The database named in the URL is already the default one: nothing to point at.
        self.set_timeout(cursor, timeout_ms)
        # Explicit, per borrow, on top of the session default above: it also holds behind a
        # proxy that does not keep session state between transactions.
        cursor.execute("START TRANSACTION READ ONLY")

    def set_timeout(self, cursor, timeout_ms: int) -> None:
        if getattr(cursor.connection, "_chinhook_mariadb", False):
            cursor.execute("SET SESSION max_statement_time = %s", (int(timeout_ms) / 1000,))
        else:
            cursor.execute("SET SESSION max_execution_time = %s", (int(timeout_ms),))

    def streaming_cursor(self, dbapi_conn):
        import pymysql.cursors

        return dbapi_conn.cursor(pymysql.cursors.SSCursor)

    def finish_streaming(self, cursor, dbapi_conn, exhausted: bool) -> None:
        if exhausted:
            cursor.close()
            return
        # Closing an unbuffered cursor reads every remaining row off the network first,
        # which for a result capped at a few hundred rows of millions is the very transfer
        # the cap exists to avoid. Dropping the connection instead ends the query at once;
        # the pool opens a fresh one for whoever needs it next. The result is told it is no
        # longer streaming first, or PyMySQL tries the same drain again, on a closed socket,
        # when it is garbage collected.
        result = getattr(cursor, "_result", None)
        if result is not None:
            result.unbuffered_active = False
        dbapi_conn.invalidate()

    def row_estimates(self, conn, schema: str) -> dict[str, int]:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT table_name, table_rows FROM information_schema.tables
                   WHERE table_schema = %s""",
                (schema,),
            )
            return {name: max(0, int(rows or 0)) for name, rows in cur.fetchall()}

    def quote(self, name: str) -> str:
        return _mysql_preparer.quote_identifier(name)


MYSQL = _MySQL(
    name=runtime.MYSQL,
    sqlglot="mysql",
    prompt_rules=_MYSQL_RULES,
    foreign_keys_sql="""SELECT DISTINCT table_name, referenced_table_name
        FROM information_schema.key_column_usage
        WHERE table_schema = %s AND referenced_table_schema = %s
          AND referenced_table_name IS NOT NULL""",
    text_types=frozenset({"varchar", "char", "text", "tinytext", "mediumtext", "longtext", "enum", "set"}),
    system_schemas=frozenset({"information_schema"}),
    forbidden_functions=frozenset({
        "sleep", "benchmark", "load_file",
        "get_lock", "release_lock", "release_all_locks", "is_free_lock", "is_used_lock",
        "sys_exec", "sys_eval",
        "master_pos_wait", "source_pos_wait",
        "wait_for_executed_gtid_set", "wait_until_sql_thread_after_gtids",
    }),
)

_BY_NAME = {POSTGRES.name: POSTGRES, MYSQL.name: MYSQL}


def for_settings(db: runtime.DatabaseSettings) -> Dialect:
    return for_name(db.dialect)


def for_name(name: str) -> Dialect:
    try:
        return _BY_NAME[name]
    except KeyError:
        raise ValueError(f"Unsupported database: {name}") from None
