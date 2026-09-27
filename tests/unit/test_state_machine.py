"""Regression tests for the state-machine bugs found by actually running the workflow:
a clarifying question that could never be closed, and a task that could be marked verified
without a query ever running.
"""

from agent.nodes.common import _is_meta, _task_from_spec
from agent.state import TaskState


def test_reset_for_retry_clears_the_held_question():
    # Without this, answering a clarifying question did not close it: the same question came
    # back, worded identically, with no way out short of starting a new chat.
    task = TaskState(task_id="t1", raw="how many tracks by Nirvana", question="how many tracks by Nirvana")
    task.pending_ambiguity = 'Nothing in this database matches "Nirvana". Which did you mean?'
    task.pending_ambiguity_options = ["Nirvana Unplugged", "Nirvana Tribute Band"]
    task.attempts = 2

    task.reset_for_retry()

    assert task.pending_ambiguity == ""
    assert task.pending_ambiguity_options == []


def test_reset_for_retry_resets_the_attempt_count():
    # A task resumed after a clarification was not a failed attempt, so it should not resume
    # with an already-exhausted repair budget.
    task = TaskState(task_id="t1", raw="", question="")
    task.attempts = 2
    task.reset_for_retry()
    assert task.attempts == 0


def test_mislabelled_meta_task_with_entities_is_reclassified():
    spec = {
        "intent": "meta",
        "question": "how many tracks does the genre Rock have",
        "entities": ["genre", "track"],
        "filters": [{"column_hint": "name", "value": "Rock", "table_hint": "genre"}],
        "metrics": [],
    }
    task = _task_from_spec(spec, 0, spec["question"])
    assert task.intent == "other"
    assert task.needs_sql is True
    assert _is_meta(task) is False


def test_mislabelled_meta_task_caught_by_catalog_word_alone(monkeypatch):
    # This path's whole point is to catch a mislabelled task from the catalog alone, with no
    # entities or filters to go on, so it has to be exercised with a real (here, stubbed)
    # catalog rather than a live database: the signal itself only exists once real schema
    # words are available to match against.
    import agent.nodes.common as common

    monkeypatch.setattr(common.schema_store, "mentions_table_word", lambda text: ["customer"])
    spec = {"intent": "meta", "question": "list all the customers",
            "entities": [], "filters": [], "metrics": []}
    task = _task_from_spec(spec, 0, spec["question"])
    assert task.intent == "other"
    assert task.needs_sql is True


def test_genuine_meta_task_is_left_alone():
    spec = {"intent": "meta", "question": "what can you help me with",
            "entities": [], "filters": [], "metrics": []}
    task = _task_from_spec(spec, 0, spec["question"])
    assert task.intent == "meta"
    assert task.needs_sql is False
    assert _is_meta(task) is True
