"""Durable storage for a chat's conversation history, so it survives a restart.

Everything the LangGraph workflow computes while answering one question is deliberately
transient: run_turn builds a fresh GraphState for every question, and none of that is meant
to outlive the call that produced it. What is meant to outlive a single question, and what
this exists to persist, is the conversation itself: the record of what was asked and
answered, held in ChatSession, which otherwise lives only in the web interface's in-memory
session state and disappears the moment the process restarts.

Held in its own small table, using a plain read-write connection of its own rather than the
pooled read-only one the query path uses: writing this table is exactly the kind of write
that role is deliberately not allowed to make.

A clarification a user has not yet answered is persisted too, but only the part of it that
is plain data. The task-level detail behind a clarification scoped to one task of several
(pending_tasks, itself a list of full TaskState objects) is not reconstructed on load, since
doing that faithfully would mean serialising the workflow's own internal state rather than
the conversation record this module is for. A restored session with such a clarification
still resumes correctly: the code that resumes one already treats an empty pending_tasks as
"resume the whole original request from the answer", which is exactly what happens here, just
without the narrower per-task shortcut a same-process resume would have taken.
"""

from __future__ import annotations

import json
import threading
from typing import Optional

import psycopg2

import config
from agent.state import ChatSession, Clarification, SchemaCache, Turn

_lock = threading.Lock()
_ensured = False


def _connection():
    # SSL mode comes from the URL itself, the same as the pooled read path, so a local or CI
    # database without TLS is not refused by a mode forced on here.
    return psycopg2.connect(config.DATABASE_URL)


def _ensure_table() -> None:
    global _ensured
    if _ensured:
        return
    with _lock:
        if _ensured:
            return
        with _connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """CREATE TABLE IF NOT EXISTS chat_sessions (
                           session_id TEXT PRIMARY KEY,
                           data JSONB NOT NULL,
                           updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                       )"""
                )
            conn.commit()
        _ensured = True


def _session_to_dict(session: ChatSession) -> dict:
    pending = None
    held = session.pending_clarification
    if held is not None:
        pending = held.as_dict()
        pending["asked"] = held.asked
        pending["resolved"] = held.resolved
    return {
        "session_id": session.session_id,
        "turns": [
            {
                "user": t.user,
                "question": t.question,
                "intent": t.intent,
                "tasks": t.tasks,
                "tables": t.tables,
                "answer": t.answer,
            }
            for t in session.turns
        ],
        "clarification_history": session.clarification_history,
        "schema_cache": dict(session.schema_cache.tables),
        "pending_clarification": pending,
    }


def _dict_to_session(data: dict) -> ChatSession:
    session = ChatSession(session_id=data.get("session_id") or "")
    session.turns = [
        Turn(
            user=t.get("user", ""),
            question=t.get("question", ""),
            intent=t.get("intent", ""),
            tasks=t.get("tasks") or [],
            tables=t.get("tables") or [],
            answer=t.get("answer", ""),
        )
        for t in (data.get("turns") or [])
    ]
    session.clarification_history = data.get("clarification_history") or []
    session.schema_cache = SchemaCache(tables=dict(data.get("schema_cache") or {}))
    pending = data.get("pending_clarification")
    if pending:
        session.pending_clarification = Clarification(
            question=pending.get("question", ""),
            options=pending.get("options") or [],
            reason=pending.get("reason", ""),
            resolved=bool(pending.get("resolved", False)),
            asked=bool(pending.get("asked", False)),
            scope=pending.get("scope", "request"),
            original_question=pending.get("original_question", ""),
            task_ids=pending.get("task_ids") or [],
        )
    return session


def save(session: ChatSession) -> None:
    """Write this chat's current state, replacing whatever was stored for it before.

    Failures here are swallowed rather than raised: losing the ability to resume a chat
    after a restart is a real cost, but a much smaller one than a question failing to answer
    because the durability layer underneath it had a problem.
    """
    try:
        _ensure_table()
        payload = json.dumps(_session_to_dict(session))
        with _connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO chat_sessions (session_id, data, updated_at)
                       VALUES (%s, %s, now())
                       ON CONFLICT (session_id)
                       DO UPDATE SET data = EXCLUDED.data, updated_at = now()""",
                    (session.session_id, payload),
                )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not save chat history ({type(exc).__name__}: {exc}); continuing without it.")


def load(session_id: str) -> Optional[ChatSession]:
    """The saved chat with this id, or None if there is not one, or if reading it failed."""
    if not session_id:
        return None
    try:
        _ensure_table()
        with _connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT data FROM chat_sessions WHERE session_id = %s", (session_id,))
                row = cur.fetchone()
        if row is None:
            return None
        return _dict_to_session(row[0])
    except Exception as exc:  # noqa: BLE001
        print(f"Could not load saved chat history ({type(exc).__name__}: {exc}); starting fresh.")
        return None
