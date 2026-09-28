"""agent/persistence.py against the real database: a session saved and loaded back, and a
session id that was never saved coming back as None rather than raising.
"""

import uuid

import pytest

from agent.persistence import load, save
from agent.state import ChatSession, Clarification, Turn

pytestmark = pytest.mark.integration


@pytest.fixture
def temp_session_id():
    session_id = f"pytest-{uuid.uuid4().hex}"
    yield session_id
    # Cleaned up regardless of whether the test itself saved anything, so a failing
    # assertion never leaves rows behind in a database other tests, or a person, might look
    # at later.
    import psycopg2

    import config

    conn = psycopg2.connect(config.DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM chat_sessions WHERE session_id = %s", (session_id,))
        conn.commit()
    finally:
        conn.close()


def test_save_then_load_round_trips_a_conversation(temp_session_id):
    session = ChatSession(session_id=temp_session_id)
    session.record_turn(Turn(
        user="how many customers", question="how many customers", intent="count",
        tasks=[{"question": "how many customers", "intent": "count"}],
        tables=["customer"], answer="There are 59 customers.",
    ))
    session.schema_cache.tables["customer"] = '"customer"(customer_id, name)'
    session.pending_clarification = Clarification(
        question="quantity or revenue?", options=["quantity", "revenue"],
        asked=True, scope="request", original_question="top selling tracks",
    )

    save(session)
    restored = load(temp_session_id)

    assert restored is not None
    assert restored.session_id == temp_session_id
    assert len(restored.turns) == 1
    assert restored.turns[0].answer == "There are 59 customers."
    assert restored.schema_cache.tables == session.schema_cache.tables
    assert restored.pending_clarification is not None
    assert restored.pending_clarification.question == "quantity or revenue?"
    assert restored.pending_clarification.options == ["quantity", "revenue"]
    assert restored.pending_clarification.pending_tasks == []


def test_saving_again_replaces_rather_than_duplicates(temp_session_id):
    session = ChatSession(session_id=temp_session_id)
    session.record_turn(Turn(user="first", question="first", answer="one"))
    save(session)

    session.record_turn(Turn(user="second", question="second", answer="two"))
    save(session)

    restored = load(temp_session_id)
    assert len(restored.turns) == 2


def test_loading_an_id_that_was_never_saved_returns_none():
    assert load(f"never-saved-{uuid.uuid4().hex}") is None


def test_loading_an_empty_id_returns_none_without_touching_the_database():
    assert load("") is None
    assert load(None) is None
