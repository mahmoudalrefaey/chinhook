"""Regression tests for the state-machine bugs found by actually running the workflow:
a task that could be marked verified without a query ever running.
"""

from agent.nodes.common import _is_meta, _task_from_spec


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
