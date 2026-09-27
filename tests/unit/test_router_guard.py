"""The deterministic guard that overrules a wrong "greeting" verdict from the classifier.

The bug this closes: say hello, then ask a real question, and the classifier sometimes
labelled the real question "greeting" too, with its own stated reason arguing the opposite.
This guard is what catches that without needing another model call. It has to do two things
at once: catch every case like that, and never fire on a message that really is just small
talk, since misfiring the other way would have the assistant answer "thanks" with a database
query.
"""

import pytest

from agent.router import _looks_like_a_database_question, is_small_talk

SHOULD_OVERRIDE = [
    "How many customers are from the USA?",
    "list all artists",
    "what genres do we have",
    "show me the top 5 tracks",
    "count the invoices",
    "who is AC/DC",
]

SHOULD_NOT_OVERRIDE = [
    "hey how's it going",
    "thanks a lot, that was helpful",
    "good morning",
    "nice, appreciate it",
    "ok cool",
]


@pytest.mark.parametrize("message", SHOULD_OVERRIDE)
def test_overrides_a_real_data_question(message):
    assert _looks_like_a_database_question(message, message, []) is True


@pytest.mark.parametrize("message", SHOULD_NOT_OVERRIDE)
def test_does_not_override_genuine_small_talk(message):
    assert _looks_like_a_database_question(message, message, []) is False


def test_a_catalog_word_alone_is_enough_to_override():
    # No question mark, no data verb: only the fact that it names something the connected
    # database holds. This is the case that survives even when the message was rewritten
    # into something that no longer reads as a question.
    assert _looks_like_a_database_question("the customers thing", "the customers thing",
                                            ["customer"]) is True


def test_the_rewrite_is_checked_even_when_the_raw_message_is_not_a_question():
    # A rewrite can turn a statement into an explicit question; either text having a
    # question mark is enough.
    assert _looks_like_a_database_question("how many customers", "How many customers are there?",
                                            []) is True


def test_is_small_talk_requires_the_whole_message_to_match():
    assert is_small_talk("hi") is True
    assert is_small_talk("hi, how many customers are there?") is False
