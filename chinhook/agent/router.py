"""Deciding where a message should go, before anything expensive happens.

Three routes, decided from the shape of the message alone, with no model call:

    greeting        small talk, no information needed
    clarification   an answer to the question the assistant is waiting on
    question        everything else, read by node_understand

understand is where a real semantic judgement gets made, such as telling a question about the
conversation apart from a question about the data ("what was the first thing I asked" versus
"who was our first customer"). It already has to read the message and the schema together to
split it into tasks, so asking a separate, cheaper model to pre-classify the same message
first was doing the same reading twice: this module keeps only the two decisions that never
needed a model to make correctly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from chinhook.agent.state import Clarification

_AFFIRMATIVE = {"yes", "yeah", "yep", "yup", "correct", "right", "exactly", "sure", "ok",
                "okay", "that", "thats", "that is", "true", "indeed", "affirmative"}
_NEGATIVE = {"no", "nope", "nah", "wrong", "incorrect", "negative", "neither"}
_BOTH = {"both", "all", "all of them", "all of those", "either", "each", "everyone",
         "everybody", "all those", "any of them"}

_GREETING = re.compile(
    r"""^\s*
        (hi|hey|hello|yo|hiya|howdy|heya|sup|greetings|good\s+(morning|afternoon|evening|day))
        (\s+there)?[.!]*\s*$""",
    re.IGNORECASE | re.VERBOSE,
)
_THANKS = re.compile(
    r"""^\s*
        (thanks?|thank\s+you|ta|cheers|nice|great|awesome|perfect|cool|lovely|helpful
        |you'?re\s+welcome|no\s+problem|my\s+pleasure|good\s+night|bye|goodbye|see\s+you
        |good\s+bye|farewell|good\s+luck)
        (\s+(a\s+lot|so\s+much|later|soon|now|around|then|!+))*[\s.!]*$""",
    re.IGNORECASE | re.VERBOSE,
)


@dataclass
class ClarificationReply:
    """What a reply to an open clarification resolved to."""

    resolved: bool
    selection: list[str] = None      # the options it picked, when it picked any
    affirmative: bool = False
    negative: bool = False
    reason: str = ""


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower()).strip(" .!?,;")


def is_small_talk(message: str) -> bool:
    """True for a message that carries no question at all.

    Deliberately strict. A message only counts as small talk when the whole of it matches,
    so "hi, how many customers are there?" is a real question and not a greeting.
    """
    text = (message or "").strip()
    if not text or len(text) > 40:
        return False
    if "?" in text:
        return False
    return bool(_GREETING.match(text) or _THANKS.match(text))


def resolve_clarification_reply(message: str, pending: Clarification) -> ClarificationReply:
    """Read a reply against the options that were actually offered.

    Handles the ways people answer a question with options: naming one, naming several,
    saying both, saying yes, saying no. It is deliberately conservative. A reply it cannot
    read is reported as unresolved so the user is asked only about what is still missing,
    rather than the whole question being put again.
    """
    text = normalize(message)
    if not text or not pending.options:
        return ClarificationReply(resolved=False, reason="nothing to match against")

    for negative in _NEGATIVE:
        if text == negative:
            return ClarificationReply(resolved=True, negative=True, reason="declined")
    for affirmative in _AFFIRMATIVE:
        if text == affirmative:
            return ClarificationReply(
                resolved=True, affirmative=True, selection=[pending.options[0]],
                reason="agreed with the reading that was offered",
            )

    chosen: list[str] = []
    for option in pending.options:
        needle = normalize(option)
        if not needle:
            continue
        if text == needle or re.search(rf"\b{re.escape(needle)}\b", text):
            chosen.append(option)

    if len(chosen) >= 2:
        return ClarificationReply(resolved=True, selection=chosen, reason="named several options")
    if chosen:
        # "both" and one of the options together, or the option named on its own.
        return ClarificationReply(resolved=True, selection=chosen, reason="named an option")

    for both in _BOTH:
        if text == both or re.search(rf"\b{re.escape(both)}\b", text):
            return ClarificationReply(
                resolved=True, selection=list(pending.options), reason="asked for all of them"
            )

    if text in {normalize(option) for option in pending.options}:
        return ClarificationReply(resolved=True, selection=[text], reason="matched an option")

    return ClarificationReply(resolved=False, reason="did not match any of the options")


def looks_like_a_new_request(message: str, pending: Clarification) -> bool:
    """Whether a message that did not match the options is a different question entirely.

    What settles it is the shape of the message: a complete question of its own, which names
    none of the options, is a new question. A short fragment is not, and is passed on to
    node_understand to be read as an attempt to answer the open one.
    """
    text = (message or "").strip()
    if not text:
        return False
    words = normalize(text).split()
    if len(words) >= 3 and text.endswith("?"):
        return True
    if len(words) > 8 and re.search(r"\b(how many|show|list|what is|which|total|count|top)\b", text):
        return True
    return False


def same_question(first: str, second: str) -> bool:
    """Whether two clarification questions are the same question in other words.

    Used as a guard: having just been told the answer, the workflow must not put the same
    question again. Comparing the words is enough for that, because a repeat is a repeat
    however it is phrased.
    """
    def words(text: str) -> set[str]:
        return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 3}

    left, right = words(first), words(second)
    if not left or not right:
        return False
    return len(left & right) / len(left | right) >= 0.6
