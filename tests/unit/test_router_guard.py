"""is_small_talk: the only thing that decides a greeting now, with no model call behind it.

The old version of this test covered a guard that caught a wrong "greeting" verdict from a
classifier model. That classifier is gone; node_route no longer asks a model anything, so
there is nothing left to override. What is left to get right is is_small_talk itself: it has
to catch every genuine greeting and never misfire on a real question, since misfiring either
way means either a database query answered with small talk, or "thanks" answered with one.
"""

import pytest

from agent.router import is_small_talk

REAL_QUESTIONS = [
    "How many customers are from the USA?",
    "list all artists",
    "what genres do we have",
    "show me the top 5 tracks",
    "count the invoices",
    "who is AC/DC",
]

GENUINE_SMALL_TALK = [
    "hey",
    "thanks a lot",
    "good morning",
    "nice",
    "cool",
    "hi",
]


@pytest.mark.parametrize("message", REAL_QUESTIONS)
def test_a_real_question_is_never_small_talk(message):
    assert is_small_talk(message) is False


@pytest.mark.parametrize("message", GENUINE_SMALL_TALK)
def test_genuine_small_talk_is_recognised(message):
    assert is_small_talk(message) is True


def test_is_small_talk_requires_the_whole_message_to_match():
    assert is_small_talk("hi") is True
    assert is_small_talk("hi, how many customers are there?") is False
