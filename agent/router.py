"""Deciding where a message should go, before anything expensive happens.

The workflow used to send every message through the same path: read the question, search the
schema, write SQL, run it. That is right for a question about the data and wrong for almost
everything else. A greeting, a question about the conversation itself, a reply to a
clarification, or a question about what the assistant just did have no business reaching the
database, and each one that did cost a Qdrant search, a query and two model calls to produce
an answer that either was not asked for or was nonsense.

This module classifies a message into one of:

    greeting        small talk, no information needed
    conversation    a question about this chat, answered from memory
    meta            a question about the assistant, its reading, or what it can do
    clarification   an answer to the question the assistant is waiting on
    followup        a change or continuation of the previous question
    database        a question whose answer is rows in the database

Order matters, and the cheapest test that can decide comes first:

1. A reply to an open clarification is recognised from the options it was given, with no
   model call at all. Getting this wrong is what made a user answer the same question twice.
2. Small talk is recognised from the shape of the message, also with no model call.
3. Everything else is classified by the model, because telling a question about the
   conversation apart from a question about the data is a semantic judgement that keywords
   get wrong: "show me the first 3 albums" is a data question, "what was the first thing I
   asked" is not, and both start with "first".

The classification is deliberately the only model call the router makes, on the cheaper
deployment, with a one word answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Optional

from agent import llm
from agent.state import Clarification, TokenUsage

Route = Literal["greeting", "conversation", "meta", "clarification", "followup", "database"]

ROUTES = ("greeting", "conversation", "meta", "clarification", "followup", "database")

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


@dataclass
class RouteDecision:
    route: Route
    reason: str = ""
    reply: Optional[ClarificationReply] = None
    usage: TokenUsage = None

    def __post_init__(self) -> None:
        if self.usage is None:
            self.usage = TokenUsage()


# ---------- deterministic steps ----------

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


def _same_question(first: str, second: str) -> bool:
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


def same_question(first: str, second: str) -> bool:
    return _same_question(first, second)


# ---------- model classification ----------

_ROUTE_SYSTEM = (
    "You decide where a message in a conversation about a database should go. The database "
    "is whatever is connected, and the catalog below, when there is one, is its schema. "
    "Reply with one JSON object and nothing else:\n"
    '{"route": "greeting" | "conversation" | "meta" | "clarification" | "followup" | '
    '"database", "reason": "a few words"}\n\n'
    "The routes mean:\n"
    '- "greeting": small talk with nothing being asked. Never anything with a question mark.\n'
    '- "conversation": a question about this conversation itself. What was asked, what was '
    "said, what came earlier, what the previous answer was, how many questions there have "
    "been. It is about the messages, not about the records in the database.\n"
    '- "meta": a question about the assistant: what it understood, what it can do, what it '
    "has looked at, whether it can answer something, what data it has.\n"
    '- "clarification": an answer to a question the assistant has just asked.\n'
    '- "followup": a change or continuation of the question just asked, where the answer is '
    "still rows in the database. Changing a filter, a limit, a sort or an entity.\n"
    '- "database": the answer is rows in the database.\n\n'
    "Rules:\n"
    "- A question about the messages of this conversation is never a database question, "
    "even when it mentions something the database holds. \"What was the first thing I asked?\" "
    "is about the conversation. \"Who was our first customer?\" is about the data.\n"
    "- A request for data, a number, a list, a total or a comparison is a database question, "
    "however it is phrased.\n"
    "- A message that answers the question the assistant asked in its last reply is a "
    "clarification. A message that asks something else entirely is not, even when a "
    "question is still open.\n"
    "- Something that continues the previous question without repeating it is a followup.\n"
    "- A short message that changes one part of the question just asked is a followup, "
    "however few words it is. If the last question was about rows and this message changes "
    "the filter, the limit, the entity or the wording of that same question, the answer is "
    "still rows in the database. Judge it by what it is asking for, not by its length or by "
    "the word it opens with.\n"
    "- A question about the assistant asks about the assistant. If the words after the "
    "question word are a country, a place, a person, a number or a thing, the question is "
    "about the data, not about the conversation.\n"
    "- If a message could be either, pick the one a careful reader would, and say why in the "
    "reason. Only say the message is a conversation question when it really is about the "
    "messages."
)


def _looks_like_a_new_request(message: str, pending: Clarification) -> bool:
    """Whether a message that did not match the options is a different question entirely.

    Asked here rather than left to the model, because the model is being asked the same
    question twice in a row and answering it differently. What settles it is the shape of
    the message: a complete question of its own, which names none of the options, is a new
    question. A short fragment is not, and is passed on to be read as a reply.
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


def classify(
    message: str,
    session,
    model: str,
) -> RouteDecision:
    """Decide where this message goes, using the cheapest test that can decide."""
    pending = session.pending_clarification
    if pending is not None and (pending.resolved or not pending.asked):
        # A clarification that was never put to the user, or one already answered, is not
        # something the next message is replying to.
        pending = None

    if pending is not None:
        reply = resolve_clarification_reply(message, pending)
        if reply.resolved:
            return RouteDecision(route="clarification", reason=reply.reason, reply=reply)

        if _looks_like_a_new_request(message, pending):
            # The user asked something else. It is read as a question of its own, with no
            # mention of the open one, and the open one is dropped rather than left to
            # swallow the next message.
            return _ask_model(message, session, model, None)

        # A short reply that named none of the options. Only this goes to the model with
        # the question in front of it, because only this is genuinely a question of intent.
        decision = _ask_model(message, session, model, pending)
        if decision.route == "clarification":
            return RouteDecision(
                route="clarification",
                reason=decision.reason or "a reply that did not match the options",
                reply=reply,
            )
        return RouteDecision(
            route=decision.route,
            reason=f"a different request while a question was open: {decision.reason}",
            usage=decision.usage,
        )

    if is_small_talk(message):
        return RouteDecision(route="greeting", reason="small talk with no question in it")

    decided = _decide_by_shape(message, session)
    if decided is not None:
        return decided

    return _ask_model(message, session, model, None)


# Words that make a message about the messages rather than about the rows. Kept deliberately
# narrow: it is worse to send a question about the chat to the database than the other way
# round, so anything ambiguous is left to the model rather than claimed here.
_ABOUT_THE_CHAT = re.compile(
    r"\b(what\s+(did|do|does|was|were)\s+(you|i|we)|what'?s\s+my|my\s+(first|last|previous)|"
    r"the\s+(first|last|previous|earlier)\s+(question|answer|thing|message)|"
    r"what\s+(was|were)\s+your|did\s+i\s+ask|have\s+i\s+asked|"
    r"what\s+have\s+i\s+asked|what\s+was\s+your|your\s+(last|previous|first)\s+"
    r"(answer|reply|question)|earlier|before\s+that|you\s+said|you\s+asked|"
    r"we\s+(talked|discussed|said)|the\s+conversation)\b",
    re.IGNORECASE,
)


def _decide_by_shape(message: str, session) -> Optional[RouteDecision]:
    """The messages that can be routed without asking anyone.

    Three of them, and each is decided on what the message is about rather than on how it is
    phrased: a question about the exchange, a message naming something the database holds,
    and a short question that plainly continues the last one.
    """
    if _is_meta_question(message):
        return RouteDecision(route="meta", reason="it asks about the assistant")
    if _ABOUT_THE_CHAT.search(message or ""):
        return RouteDecision(
            route="conversation", reason="it asks about the exchange, not about the rows"
        )

    try:
        from agent import schema as schema_store

        if schema_store.mentions_catalog_entity(message):
            return RouteDecision(
                route="database", reason="it names something this database holds"
            )
    except Exception:  # noqa: BLE001
        pass

    text = (message or "").strip()
    words = normalize(text).split()
    last_was_data = bool(session.turns) and any(
        (task.get("answer_summary") or "") for task in session.turns[-1].tasks
    )
    if last_was_data and 1 < len(words) <= 8 and text.endswith("?"):
        # A short question straight after an answer continues that answer, unless it named
        # something the database holds, which was settled above.
        return RouteDecision(
            route="followup", reason="a short question continuing the answer just given"
        )
    return None


# A question about the assistant rather than about the data is recognisable by its shape,
# and getting it wrong sends a question about itself into a query.
_META = re.compile(
    r"""^\s*(what|which)\s+(do\s+you|can\s+you|are\s+you|have\s+you|should\s+you|did\s+you)
        .*\b(understand|interpret|make\s+of|think|mean|do|can|able|offer|support
              |capabilit|retrieve|look\s+up|search|see|know|have|got|gotten)\b""",
    re.IGNORECASE | re.VERBOSE,
)
_META_SUBJECTS = re.compile(
    r"\b(you|your|yourself)\b.*\b(understand|interpret|capabilit|do|can|able|retrieve|"
    r"look\s+up|search|data|information|have|got|seen|show|offer|support)\b",
    re.IGNORECASE,
)


def _is_meta_question(message: str) -> bool:
    return bool(_META.match(message or "") or _META_SUBJECTS.search(message or ""))


def _ask_model(
    message: str, session, model: str, pending: Optional[Clarification]
) -> RouteDecision:
    catalog = ""
    try:
        from agent import schema as schema_store

        catalog = schema_store.catalog_text()
    except Exception:  # noqa: BLE001
        catalog = ""

    parts = [f"Message: {message}"]
    if pending is not None:
        parts.append(
            "The assistant asked the user this and is waiting for an answer:\n"
            f"  {pending.question}\n"
            f"  options offered: {', '.join(pending.options) or 'none'}\n"
            "If this message answers that question, the route is clarification. If it is a "
            "different request of its own, it is not a reply at all and you must not call it "
            "one."
        )
    if catalog:
        parts.append(f"Database catalog:\n{catalog}")
    history = session.turn_summary(limit=3)
    if history:
        parts.append(f"The exchange so far:\n{history}")

    payload, spent = llm.chat_json(
        llm.grounding_model(model),
        _ROUTE_SYSTEM,
        "\n\n".join(parts),
        max_completion_tokens=200,
    )
    route = str((payload or {}).get("route") or "").strip().lower()
    if route not in ROUTES:
        route = "database"
    if pending is None and _is_meta_question(message):
        # A question about what the assistant did is never a question about the rows, no
        # matter how the model read it.
        route = "meta"
    return RouteDecision(
        route=route,  # type: ignore[arg-type]
        reason=str((payload or {}).get("reason") or "").strip(),
        usage=spent,
    )
