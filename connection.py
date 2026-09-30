"""Turning what a person typed on the setup screen into settings the app can connect with.

A database can be given as one connection URL, the form every hosting provider hands out
(Supabase, Neon, Railway, RDS, PlanetScale), or as separate fields. Both end up as the same
runtime.DatabaseSettings, with the driver this app actually uses for that database and the
schema questions will be answered from.
"""

from __future__ import annotations

import os

from sqlalchemy.engine import URL, make_url

import runtime

# The scheme a provider writes -> the SQLAlchemy driver this app connects with.
_DRIVERS = {
    "postgres": "postgresql+psycopg2",
    "postgresql": "postgresql+psycopg2",
    "mysql": "mysql+pymysql",
    "mariadb": "mysql+pymysql",
}

_DEFAULT_PORTS = {runtime.POSTGRES: 5432, runtime.MYSQL: 3306}


class ConnectionSetupError(ValueError):
    """The connection details cannot be used, with a reason a person can act on."""


def _driver_for(scheme: str) -> str:
    backend = scheme.split("+", 1)[0].lower()
    driver = _DRIVERS.get(backend)
    if driver is None:
        raise ConnectionSetupError(
            f'"{scheme}" is not a supported database. Use a PostgreSQL or MySQL connection URL, '
            "starting with postgresql:// or mysql://."
        )
    return driver


def _ssl_from_mysql_query(query: dict) -> str:
    """The TLS intent of a MySQL URL's own query string, which PyMySQL cannot take as-is."""
    for key in ("ssl-mode", "ssl_mode", "sslmode"):
        value = str(query.get(key) or "").lower()
        if value in {"disabled", "disable"}:
            return "disable"
        if value:
            return "require"
    if query.get("ssl") or query.get("sslaccept") or query.get("ssl_ca"):
        return "require"
    return ""


def database_settings_from_url(text: str, schema: str = "", ssl_mode: str = "prefer") -> runtime.DatabaseSettings:
    """Settings from one connection URL, as a provider gives it."""
    text = (text or "").strip()
    if not text:
        raise ConnectionSetupError("Enter a connection URL.")
    try:
        url = make_url(text)
    except Exception as exc:  # noqa: BLE001
        raise ConnectionSetupError(
            "That connection URL could not be read. It should look like "
            "postgresql://user:password@host:5432/database. A password containing @, :, / or # "
            "has to be percent-encoded in a URL; entering the details as separate fields "
            "avoids that."
        ) from exc

    url = url.set(drivername=_driver_for(url.drivername))
    dialect = url.get_backend_name()
    if not url.host:
        raise ConnectionSetupError("The connection URL has no host.")
    if not url.database:
        raise ConnectionSetupError("The connection URL has no database name.")

    query = dict(url.query)
    if dialect == runtime.POSTGRES:
        # libpq's own sslmode, when the URL carries one, is what the provider meant.
        if query.get("sslmode"):
            ssl_mode = str(query["sslmode"])
        else:
            url = url.update_query_dict({"sslmode": ssl_mode})
    else:
        # PyMySQL takes TLS as a connection argument (see scripts/db/dialects.py), not as a
        # URL parameter, and fails on any parameter it does not know; the URL keeps none.
        ssl_mode = _ssl_from_mysql_query(query) or ssl_mode
        url = url.set(query={})

    return _settings(url, schema, ssl_mode)


def database_settings_from_fields(
    dialect: str,
    host: str,
    port: int | str | None,
    database: str,
    username: str,
    password: str,
    schema: str = "",
    ssl_mode: str = "prefer",
) -> runtime.DatabaseSettings:
    """Settings from separate fields. The password is taken exactly as typed."""
    if dialect not in runtime.SUPPORTED_DIALECTS:
        raise ConnectionSetupError(f"Unsupported database: {dialect}")
    host = (host or "").strip()
    database = (database or "").strip()
    if not host:
        raise ConnectionSetupError("Enter the database host.")
    if not database:
        raise ConnectionSetupError("Enter the database name.")
    try:
        port_number = int(port) if str(port or "").strip() else _DEFAULT_PORTS[dialect]
    except ValueError as exc:
        raise ConnectionSetupError("The port must be a number.") from exc

    query = {"sslmode": ssl_mode} if dialect == runtime.POSTGRES else {}
    url = URL.create(
        _DRIVERS[dialect],
        username=(username or "").strip() or None,
        password=password or None,
        host=host,
        port=port_number,
        database=database,
        query=query,
    )
    return _settings(url, schema, ssl_mode)


def _settings(url: URL, schema: str, ssl_mode: str) -> runtime.DatabaseSettings:
    dialect = url.get_backend_name()
    if ssl_mode not in runtime.SSL_MODES and dialect == runtime.MYSQL:
        ssl_mode = "require"
    schema = (schema or "").strip()
    if dialect == runtime.MYSQL:
        # In MySQL a schema is a database: there is nothing else it could be.
        schema = url.database or ""
    elif not schema:
        schema = "public"
    return runtime.DatabaseSettings(url=url, schema=schema, ssl_mode=ssl_mode)


def llm_settings(base_url: str, api_key: str, model: str, fast_model: str = "") -> runtime.LLMSettings:
    base_url = (base_url or "").strip().rstrip("/")
    model = (model or "").strip()
    if not base_url.startswith(("http://", "https://")):
        raise ConnectionSetupError("The base URL must start with https:// (or http://).")
    if not (api_key or "").strip():
        raise ConnectionSetupError("Enter the API key.")
    if not model:
        raise ConnectionSetupError("Enter the model name.")
    return runtime.LLMSettings(
        base_url=base_url, api_key=api_key.strip(), model=model, fast_model=(fast_model or "").strip()
    )


def runtime_from_env() -> runtime.Runtime:
    """A connection described by environment variables, for tests and local scripts.

    Never used by the web app, where every visitor brings their own: DATABASE_URL,
    DATABASE_SCHEMA, LLM_BASE_URL, LLM_API_KEY, LLM_MODEL and LLM_FAST_MODEL.
    """
    db = database_settings_from_url(
        os.environ["DATABASE_URL"], schema=os.getenv("DATABASE_SCHEMA", "")
    )
    llm = runtime.LLMSettings(
        base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
        api_key=os.getenv("LLM_API_KEY", ""),
        model=os.getenv("LLM_MODEL", ""),
        fast_model=os.getenv("LLM_FAST_MODEL", ""),
    )
    return runtime.Runtime(db=db, llm=llm)
