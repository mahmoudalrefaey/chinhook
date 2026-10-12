"""The JSON API behind the page: one endpoint per thing a visitor can do.

Every endpoint works on the caller's own session (chinhook/web/sessions.py), found from the
token the page sends in the X-Session header. The jobs that take a while (checking a new
connection, indexing a database, answering a question) run on a thread of their own and
report back as they go, one JSON object per line, so the page can show each step as it
happens instead of a spinner. Everything else is an ordinary request and response.

Errors a person can act on are returned as {"error": "..."} with a matching status, worded for
whoever is using the app; the technical detail behind an unexpected one is returned alongside
as "detail", for whoever runs the deployment.
"""

from __future__ import annotations

import json
import queue
import secrets
import threading
import time
from typing import Callable, Literal

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from chinhook import chat_engine
from chinhook import config
from chinhook import connection
from chinhook import rate_limit
from chinhook import runtime
from chinhook import settings_file
from chinhook.db.indexing import get_index_status
from chinhook.web import render
from chinhook.web.sessions import Session, store

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

TOKEN_HEADER = "x-session"

# Questions that make sense for any database, since nothing here knows what the connected
# one holds until it has been read.
EXAMPLE_QUESTIONS = [
    "What tables are in this database?",
    "Which table has the most rows?",
    "What can I ask about this data?",
    "Summarise what this database is about",
]

_DIALECT_NAMES = {runtime.POSTGRES: "PostgreSQL", runtime.MYSQL: "MySQL"}

_READ_ONLY_SQL = {
    runtime.POSTGRES: (
        "CREATE ROLE chat_reader WITH LOGIN PASSWORD 'choose-a-password';\n"
        "GRANT CONNECT ON DATABASE your_database TO chat_reader;\n"
        "GRANT USAGE ON SCHEMA public TO chat_reader;\n"
        "GRANT SELECT ON ALL TABLES IN SCHEMA public TO chat_reader;\n"
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO chat_reader;"
    ),
    runtime.MYSQL: (
        "CREATE USER 'chat_reader'@'%' IDENTIFIED BY 'choose-a-password';\n"
        "GRANT SELECT, SHOW VIEW ON your_database.* TO 'chat_reader'@'%';"
    ),
}

MAX_QUESTION_LENGTH = 4000


# ---------- errors ----------

class ApiError(Exception):
    def __init__(self, status: int, message: str, code: str = "", detail: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.detail = detail


@app.exception_handler(ApiError)
async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
    body = {"error": exc.message}
    if exc.code:
        body["code"] = exc.code
    if exc.detail:
        body["detail"] = exc.detail
    return JSONResponse(body, status_code=exc.status)


def _too_many(wait: float) -> ApiError:
    return ApiError(429, f"Too many attempts in a short time. Try again in about {wait:.0f} seconds.")


def _take(bucket: rate_limit.TokenBucket) -> None:
    allowed, wait = bucket.take()
    if not allowed:
        raise _too_many(wait)


# ---------- sessions ----------

def session(request: Request) -> Session:
    found = store.get(request.headers.get(TOKEN_HEADER))
    if found is None:
        raise ApiError(401, "This session has ended.", code="session")
    return found


def signed_in(s: Session = Depends(session)) -> Session:
    if not s.authenticated:
        raise ApiError(403, "Enter the passphrase first.", code="phase")
    return s


def connected(s: Session = Depends(signed_in)) -> Session:
    if s.runtime is None:
        raise ApiError(409, "Connect a database first.", code="phase")
    return s


def ready(s: Session = Depends(connected)) -> Session:
    if not s.index_ready:
        raise ApiError(409, "The database is still being prepared.", code="phase")
    return s


def _claim(s: Session) -> None:
    """Mark the session busy for one long job, or refuse if it is already running one."""
    if not s.busy.acquire(blocking=False):
        raise ApiError(409, "Still working on the previous request. Wait for it to finish.")


def _not_busy(s: Session) -> None:
    if s.busy.locked():
        raise ApiError(409, "Wait for the current answer to finish first.")


# ---------- streaming ----------

def _stream(job: Callable[[Callable[[dict], None]], None], on_finish: Callable[[], None] | None = None):
    """Run job on a thread of its own and send each event it emits as one line of JSON.

    The job keeps running if the browser goes away halfway, so an answer or an index is
    finished and kept rather than left half-done; the page picks it up when it comes back.
    """
    events: queue.Queue = queue.Queue()
    finished = object()

    def run() -> None:
        try:
            job(events.put)
        except Exception as exc:  # noqa: BLE001
            events.put({
                "type": "error",
                "message": "Something went wrong on the server. Trying again usually works.",
                "detail": f"{type(exc).__name__}: {exc}",
            })
        finally:
            if on_finish is not None:
                on_finish()
            events.put(finished)

    threading.Thread(target=run, daemon=True).start()

    def lines():
        while True:
            event = events.get()
            if event is finished:
                return
            yield json.dumps(event, ensure_ascii=False) + "\n"

    return StreamingResponse(
        lines(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


# ---------- cached reads ----------
# Keyed by the connected database: these are shared by every session on this server, and a
# cache keyed by nothing would hand one visitor's schema to another.

class _Cache:
    def __init__(self, seconds: float) -> None:
        self._seconds = seconds
        self._values: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def get(self, key: str, compute: Callable[[], object]):
        with self._lock:
            hit = self._values.get(key)
            if hit and time.monotonic() - hit[0] < self._seconds:
                return hit[1]
        value = compute()
        with self._lock:
            self._values[key] = (time.monotonic(), value)
        return value

    def clear(self, key: str) -> None:
        with self._lock:
            self._values.pop(key, None)


_status_cache = _Cache(120)
_schema_cache = _Cache(300)


def _forget_cached(tenant: str) -> None:
    _status_cache.clear(tenant)
    _schema_cache.clear(tenant)


# ---------- views ----------

def _connection_view(rt: runtime.Runtime) -> dict:
    url = rt.db.url
    return {
        "dialect": rt.db.dialect,
        "dialect_name": _DIALECT_NAMES.get(rt.db.dialect, rt.db.dialect),
        "database": url.database or "",
        "host": url.host or "",
        "port": url.port,
        "schema": rt.db.schema if rt.db.dialect == runtime.POSTGRES else "",
        "describe": rt.db.describe(),
        "model": rt.llm.model,
        "fast_model": rt.llm.fast_model,
    }


def _setup_info() -> dict:
    """What the setup screen offers. The same for everyone; nothing here is a secret."""
    return {
        "providers": connection.PROVIDERS,
        "ssl_modes": list(runtime.SSL_MODES),
        "read_only_sql": _READ_ONLY_SQL,
        "min_passphrase": settings_file.MIN_PASSPHRASE_LENGTH,
        "retention_days": config.INDEX_RETENTION_DAYS,
    }


# ---------- session and sign-in ----------

@app.post("/api/session")
def new_session() -> dict:
    return {"token": store.create().token}


@app.get("/api/state")
def state(s: Session = Depends(session)) -> dict:
    body: dict = {"phase": s.phase}
    if s.phase == "auth":
        return body
    body["setup"] = _setup_info()
    if s.runtime is not None:
        body["connection"] = _connection_view(s.runtime)
    if s.phase == "chat":
        body["examples"] = EXAMPLE_QUESTIONS
        body["pipeline"] = render.pipeline()
        body["busy"] = s.busy.locked()
        body["messages"] = [
            render.message_view(index, message, s.answered)
            for index, message in enumerate(s.messages)
        ]
    return body


class SignIn(BaseModel):
    passphrase: str = Field(max_length=1000)


@app.post("/api/auth")
def sign_in(body: SignIn, s: Session = Depends(session)) -> dict:
    """The optional shared passphrase in front of the whole app (APP_PASSPHRASE).

    Meant for a small evaluation audience on a link that is otherwise public, not a real
    login system: everyone who has the passphrase shares one identity.
    """
    if s.authenticated:
        return {"ok": True}
    _take(s.auth_bucket)
    # compare_digest rather than ==, so how long the comparison takes does not itself leak how
    # many characters were guessed correctly. Encoded to bytes first: compare_digest refuses
    # two str arguments outright unless both are pure ASCII.
    if not secrets.compare_digest(
        body.passphrase.encode("utf-8"), (config.APP_PASSPHRASE or "").encode("utf-8")
    ):
        raise ApiError(400, "That passphrase is not correct.")
    s.authenticated = True
    return {"ok": True}


# ---------- connecting ----------

class ConnectForm(BaseModel):
    dialect: Literal["postgresql", "mysql"]
    mode: Literal["url", "fields"] = "url"
    url: str = Field("", max_length=4000)
    host: str = Field("", max_length=500)
    port: str = Field("", max_length=10)
    database: str = Field("", max_length=500)
    username: str = Field("", max_length=500)
    password: str = Field("", max_length=1000)
    schema_name: str = Field("", max_length=500, alias="schema")
    ssl_mode: str = Field("prefer", max_length=20)
    base_url: str = Field("", max_length=2000)
    api_key: str = Field("", max_length=4000)
    model: str = Field("", max_length=300)
    fast_model: str = Field("", max_length=300)


def _candidate(form: ConnectForm) -> runtime.Runtime:
    if form.ssl_mode not in runtime.SSL_MODES:
        raise ApiError(400, "Choose an SSL mode from the list.")
    schema = form.schema_name if form.dialect == runtime.POSTGRES else ""
    try:
        if form.mode == "url":
            db = connection.database_settings_from_url(form.url, schema=schema, ssl_mode=form.ssl_mode)
            if db.dialect != form.dialect:
                raise connection.ConnectionSetupError(
                    f"That is a {_DIALECT_NAMES[db.dialect]} URL, but "
                    f"{_DIALECT_NAMES[form.dialect]} is selected."
                )
        else:
            db = connection.database_settings_from_fields(
                form.dialect, form.host, form.port, form.database, form.username, form.password,
                schema=schema, ssl_mode=form.ssl_mode,
            )
        llm_settings = connection.llm_settings(form.base_url, form.api_key, form.model, form.fast_model)
    except connection.ConnectionSetupError as exc:
        raise ApiError(400, str(exc)) from exc
    return runtime.Runtime(db=db, llm=llm_settings)


@app.post("/api/connect")
def connect(form: ConnectForm, s: Session = Depends(signed_in)):
    """Log in to the database and try the model for real, and connect when both work."""
    _take(s.setup_bucket)
    candidate = _candidate(form)
    _claim(s)

    def job(emit) -> None:
        step = "database"
        try:
            emit({"type": "step", "id": "database", "state": "running",
                  "text": f"Connecting to {candidate.db.describe()}"})
            count = connection.check_database(candidate)
            emit({"type": "step", "id": "database", "state": "done",
                  "text": f"Connected: {count} tables and views to ask about"})
            step = "model"
            emit({"type": "step", "id": "model", "state": "running",
                  "text": f"Checking {candidate.llm.model}"})
            connection.check_llm(candidate.llm)
            emit({"type": "step", "id": "model", "state": "done",
                  "text": f"{candidate.llm.model} answers and calls tools"})
        except connection.ConnectionSetupError as exc:
            emit({"type": "step", "id": step, "state": "failed"})
            emit({"type": "error", "message": str(exc)})
            return
        s.forget_connection()
        s.runtime = candidate
        emit({"type": "done"})

    return _stream(job, on_finish=s.busy.release)


class ConfigFile(BaseModel):
    content: str = Field(max_length=1_000_000)


class ConfigUnlock(ConfigFile):
    passphrase: str = Field("", max_length=1000)
    password: str = Field("", max_length=1000)
    api_key: str = Field("", max_length=4000)


@app.post("/api/config/inspect")
def inspect_config(body: ConfigFile, s: Session = Depends(signed_in)) -> dict:
    """Whether an exported file can be loaded, and whether it needs a passphrase to be."""
    try:
        info = settings_file.inspect(body.content.encode("utf-8"))
    except settings_file.SettingsFileError as exc:
        raise ApiError(400, str(exc)) from exc
    return {"encrypted": info.encrypted, "label": info.label}


@app.post("/api/config/load")
def load_config(body: ConfigUnlock, s: Session = Depends(signed_in)) -> dict:
    """The setup form's values, read from an exported file.

    The file came from this visitor's own browser, so its contents go back to it to fill the
    form; nothing of it is kept here.
    """
    _take(s.import_bucket)
    try:
        loaded = settings_file.load_settings(body.content.encode("utf-8"), body.passphrase)
    except settings_file.SettingsFileError as exc:
        raise ApiError(400, str(exc)) from exc
    loaded = loaded.with_secrets(body.password, body.api_key)
    return {
        "dialect": loaded.dialect,
        "mode": "url",
        "url": loaded.url,
        "schema": loaded.schema,
        "ssl_mode": loaded.ssl_mode,
        "provider": loaded.provider,
        "base_url": loaded.base_url,
        "model": loaded.model,
        "fast_model": loaded.fast_model,
        "api_key": loaded.api_key,
        "summary": loaded.describe(),
    }


@app.post("/api/disconnect")
def disconnect(s: Session = Depends(signed_in)) -> dict:
    _not_busy(s)
    s.forget_connection()
    return {"ok": True}


# ---------- indexing ----------

@app.post("/api/prepare")
def prepare(s: Session = Depends(connected)):
    """Bring the connected database's index up to date, then the chat can start.

    The first connection to a database builds its index; a later one reuses it and only
    re-indexes tables whose shape changed since, so reconnecting is usually instant.
    """
    _claim(s)
    rt = s.runtime

    def job(emit) -> None:
        def progress(done, total, message):
            emit({"type": "progress", "done": done, "total": total, "message": message})

        try:
            with runtime.use(rt):
                chat_engine.prepare_index(progress)
        except Exception as exc:  # noqa: BLE001
            emit({
                "type": "error",
                "message": chat_engine.describe_index_error(exc, rt.db),
                "detail": f"{type(exc).__name__}: {exc}",
            })
            return
        _forget_cached(rt.tenant_id)
        if s.runtime is rt:
            s.index_ready = True
        emit({"type": "done"})

    return _stream(job, on_finish=s.busy.release)


@app.get("/api/status")
def index_status(s: Session = Depends(ready)) -> dict:
    rt = s.runtime

    def compute():
        with runtime.use(rt):
            status = get_index_status()
        return {
            "tables": status["db_tables"],
            "indexed": status["qdrant_tables"],
            "stale": status["needs_reindex"],
            "changed": status["changed_tables"],
        }

    try:
        return _status_cache.get(rt.tenant_id, compute)
    except Exception:  # noqa: BLE001
        return {"unavailable": True}


@app.post("/api/index/refresh")
def refresh_index(s: Session = Depends(ready)) -> dict:
    """Re-index only the tables whose shape changed since the index was built."""
    _take(s.setup_bucket)
    _claim(s)
    rt = s.runtime
    try:
        with runtime.use(rt):
            count = chat_engine.prepare_index()
    except Exception as exc:  # noqa: BLE001
        raise ApiError(502, chat_engine.describe_index_error(exc, rt.db),
                       detail=f"{type(exc).__name__}: {exc}") from exc
    finally:
        s.busy.release()
    _forget_cached(rt.tenant_id)
    return {"count": count}


@app.post("/api/index/delete")
def delete_index(s: Session = Depends(connected)) -> dict:
    """Remove everything this deployment holds about the connected database, and disconnect."""
    _not_busy(s)
    rt = s.runtime
    try:
        with runtime.use(rt):
            chat_engine.delete_index()
    except Exception as exc:  # noqa: BLE001
        raise ApiError(502, chat_engine.describe_index_error(exc, rt.db),
                       detail=f"{type(exc).__name__}: {exc}") from exc
    _forget_cached(rt.tenant_id)
    s.forget_connection()
    return {"ok": True}


@app.get("/api/schema")
def schema(s: Session = Depends(ready)) -> dict:
    """Every table and view in the connected schema, read from the database itself."""
    rt = s.runtime

    def compute():
        with runtime.use(rt):
            return chat_engine.schema_overview()

    try:
        tables = _schema_cache.get(rt.tenant_id, compute)
    except Exception as exc:  # noqa: BLE001
        raise ApiError(502, "Could not read the schema.", detail=f"{type(exc).__name__}: {exc}") from exc
    return {
        "tables": [
            {
                "name": table["table"],
                "kind": table.get("kind") or "table",
                "rows": table["rows"],
                "columns": [[str(name), str(kind)] for name, kind in table["columns"]],
            }
            for table in tables
        ]
    }


# ---------- chat ----------

class Question(BaseModel):
    question: str = Field(max_length=MAX_QUESTION_LENGTH)
    # Set when this is the answer to a clarifying question: that message's index, so its
    # options are not offered again.
    answers: int | None = None


@app.post("/api/ask")
def ask(body: Question, s: Session = Depends(ready)):
    question = body.question.strip()
    if not question:
        raise ApiError(400, "Type a question first.")
    _claim(s)
    try:
        allowed, notice = rate_limit.check(s.rate_bucket)
        if not allowed:
            # Refused before the question is added to the history at all, so it is not shown
            # as though it were asked and never answered.
            raise ApiError(429, notice)
        if body.answers is not None:
            s.answered.add(body.answers)
        if s.chat is None:
            s.chat = chat_engine.new_chat()
        s.messages.append({"role": "user", "content": question})
    except Exception:
        s.busy.release()
        raise
    rt, chat, asked = s.runtime, s.chat, len(s.messages) - 1

    def job(emit) -> None:
        emit({"type": "question", "message": render.message_view(asked, s.messages[asked], s.answered)})
        streamed: list[str] = []
        last_sent = [0.0]

        def on_stage(label):
            emit({"type": "stage", "key": chat_engine.STAGE_TO_KEY.get(label), "label": label})

        def on_token(chunk):
            # Rendered on the server, like the finished answer, so what streams in is the same
            # sanitised HTML; sent at most every 80 ms rather than per token.
            streamed.append(chunk)
            now = time.monotonic()
            if now - last_sent[0] >= 0.08:
                last_sent[0] = now
                emit({"type": "partial", "html": render.markdown_to_html("".join(streamed))})

        with runtime.use(rt):
            result = chat_engine.answer(question, on_stage=on_stage, on_token=on_token, session=chat)

        if result["ok"] and result.get("answer"):
            message = {"role": "assistant", "content": result["answer"], "result": result}
        else:
            message = {"role": "assistant", "content": "", "failed": True, "result": result}
        s.messages.append(message)
        emit({"type": "message",
              "message": render.message_view(len(s.messages) - 1, message, s.answered)})

    return _stream(job, on_finish=s.busy.release)


@app.post("/api/chat/new")
def new_chat(s: Session = Depends(ready)) -> dict:
    _not_busy(s)
    s.new_chat()
    return {"ok": True}


# ---------- exporting the connection ----------

class Export(BaseModel):
    include_secrets: bool = True
    passphrase: str = Field("", max_length=1000)
    passphrase_again: str = Field("", max_length=1000)


@app.post("/api/export")
def export(body: Export, s: Session = Depends(connected)) -> Response:
    """The connection as a file, to import on the setup screen next time.

    Made for this one download: this server keeps no copy. See chinhook/settings_file.py for
    what is in it and how the password and key are encrypted.
    """
    rt = s.runtime
    try:
        if body.include_secrets:
            settings_file.check_new_passphrase(body.passphrase, body.passphrase_again)
            data = settings_file.export_settings(rt, body.passphrase)
        else:
            data = settings_file.export_settings(rt)
    except settings_file.SettingsFileError as exc:
        raise ApiError(400, str(exc)) from exc
    name = settings_file.file_name(rt)
    return Response(
        data,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
        },
    )
