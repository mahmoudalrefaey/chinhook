"""A handful of real questions through the actual agent workflow, live: Azure, Postgres
and Qdrant all genuinely called, exactly as a real user's question would.

Kept small and specific on purpose. This is not where breadth belongs, since every case here
costs a handful of real model calls; the unit suite is where the logic each of these depends
on is exercised in every combination that matters. What belongs here is the handful of cases
that only exist as an actual regression once every layer is wired together for real, which is
exactly what the two bugs below were.
"""

import pytest

from agent.graph import run_turn
from agent.session import new_chat

pytestmark = [pytest.mark.integration, pytest.mark.llm]


def test_a_real_question_after_a_greeting_is_answered_not_ignored():
    # The original bug: say hello, then ask a real question, and the classifier sometimes
    # answered the second message as though it were more small talk, with the database never
    # touched and no sign anything had gone wrong. node_route no longer asks a model to tell
    # the two apart, but the same failure shape is still worth guarding: a real question
    # right after a greeting must reach node_understand and get a real answer, not silently
    # inherit the greeting route.
    session = new_chat()
    run_turn("hi there", session=session, model="gpt-4.1-mini")
    result = run_turn(
        "How many customers are from the USA?", session=session, model="gpt-4.1-mini"
    )
    assert result.get("route") == "question"
    assert result.get("ok") is True
    assert "13" in (result.get("answer") or "")


def test_an_ambiguous_metric_is_never_silently_answered_by_price():
    # The semantic gap this whole project exists to close: "top selling" has no single
    # meaning in the schema, and the original bug answered it by sorting on price, silently
    # and with no indication that was a guess. The acceptable outcomes now are either a
    # clarifying question naming the real choice, or a direct answer that is transparent
    # about having used quantity sold. What is never acceptable is answering from price with
    # no acknowledgement that "top selling" was read as anything in particular.
    session = new_chat()
    result = run_turn(
        "What are the top 5 selling tracks?", session=session, model="gpt-4.1-mini"
    )
    assert result.get("ok") is True

    if result.get("kind") == "clarification":
        question = (result.get("answer") or "").lower()
        assert "revenue" in question or "quantity" in question or "sold" in question
        return

    sql = (result.get("sql") or "").lower()
    answer = (result.get("answer") or "").lower()
    assert "quantity" in sql, f"did not sort by quantity sold, the original bug's fix: {sql}"
    assert "quantity" in answer or "sold" in answer, (
        f"answer did not say what 'top selling' was taken to mean: {answer}"
    )


def test_a_grouped_count_question_is_answered_not_reported_as_a_failure():
    session = new_chat()
    result = run_turn(
        "how many tracks are in each genre", session=session, model="gpt-4.1-mini"
    )
    assert result.get("ok") is True
    assert result.get("kind") == "answer"
    assert (result.get("rows") or [])


def test_a_plain_question_costs_three_model_calls():
    # The number the whole graph rewrite was for: understand, write the SQL, write the
    # answer. Down from six to eight in the graph this replaced.
    session = new_chat()
    result = run_turn(
        "How many customers are from the USA?", session=session, model="gpt-4.1-mini"
    )
    assert result.get("ok") is True
    assert result.get("usage", {}).get("llm_calls") == 3


def test_a_clarification_answer_resumes_the_original_request():
    # The whole point of moving to one request-level clarification: a word naming a group of
    # people that exists in more than one table (customer and employee both hold a country)
    # holds the turn instead of guessing, and the reply that follows is read as the answer to
    # that question, not as a new request of its own.
    session = new_chat()
    asked = run_turn("How many Americans are there?", session=session, model="gpt-4.1-mini")
    assert asked.get("kind") == "clarification"
    assert asked.get("clarification")

    resumed = run_turn("customers", session=session, model="gpt-4.1-mini")
    assert resumed.get("ok") is True
    assert resumed.get("kind") == "answer"
    assert "13" in (resumed.get("answer") or "")
