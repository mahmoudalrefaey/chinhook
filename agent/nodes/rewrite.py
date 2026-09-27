"""Rewriting one message so it means exactly one thing.

The workflow works on the rewritten text from here on, so the rewrite has to be the user's
own intent with the gaps filled in, and nothing more. Three things protect that, all of them
in code rather than in the prompt:

* A rewrite that changes a number, a filter value, or the subject it is about is rejected,
  and the original is used instead. The prompt is told not to do this; this is what happens
  when it does.
* Where the rewrite leaves something open, that becomes a question to the user rather than a
  guess.
* The original is kept on the state next to the rewrite, so every later node can see both and
  decide for itself which one it needs. Nodes that are about what the user wanted read the
  rewrite; nodes that are about what the user said read the original.

Two kinds of message are passed through without a model call: small talk, which has no intent
to resolve, and a reply to a question the assistant already asked, which is as precise as it
is going to get and would only be put in the way of matching the reply to the options.
"""

import re
from typing import Any, Optional

from agent import llm, router
from agent.nodes.common import _trace, _usage
from agent.nodes.prompts import _REWRITE_SYSTEM
from agent.state import Clarification, GraphState, TokenUsage

# A number in the message is a limit, a count, a year or a threshold. Whatever it is, a
# rewrite that changes it has changed the question.
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")

# A name the user typed in quotes is the one thing they were most likely being precise about.
_QUOTED = re.compile(r"['\"‘’“”]([^'\"‘’“”]{2,})['\"‘’“”]")


def numbers_in(text: str) -> set[str]:
    return {match.group(0).rstrip(".,") for match in _NUMBER.finditer(text or "")}


def quoted_in(text: str) -> set[str]:
    return {
        match.group(1).strip()
        for match in _QUOTED.finditer(text or "")
        if match.group(1).strip()
    }


def _named_tables(text: str) -> set[str]:
    try:
        from agent import schema as schema_store

        return set(schema_store.mentions_table_word(text))
    except Exception:  # noqa: BLE001
        return set()


def _previous_turn(session) -> str:
    if not session or not session.turns:
        return ""
    last = session.turns[-1]
    return f"{last.user} {last.question}"


def node_rewrite(state: GraphState) -> dict:
    """Rewrite the message, or record why it was left alone."""
    session = state["session"]
    model = state["model"]
    raw = state.get("raw_question") or state["question"]

    # Small talk has no intent to resolve, and rewriting it would change a message the
    # system has nothing to do with.
    if router.is_small_talk(raw):
        return _unchanged(state, "small talk has nothing to resolve")

    # A reply to a question the assistant is already waiting on is already as precise as it
    # gets. Rewriting it would only put a paraphrase between it and the options it answers.
    pending = session.pending_clarification
    if pending is not None and not pending.resolved and pending.asked:
        reply = router.resolve_clarification_reply(raw, pending)
        if reply.resolved or len(router.normalize(raw).split()) <= 6:
            return _unchanged(state, "a reply to an open question is already precise")

    catalog = ""
    try:
        from agent import schema as schema_store

        catalog = schema_store.catalog_text()
    except Exception:  # noqa: BLE001
        catalog = ""

    parts = [f"Message from the user:\n{raw}"]
    history = session.context_summary()
    if history:
        parts.append(f"Earlier in this chat:\n{history}")
    if catalog:
        parts.append(f"The schema of the database that is connected:\n{catalog}")
    if not history and not session.turns:
        parts.append("This is the first message of the chat.")

    payload, spent = llm.chat_json(
        llm.grounding_model(model), _REWRITE_SYSTEM, "\n\n".join(parts), max_completion_tokens=700
    )
    usage = _usage(state, spent)
    payload = payload or {}
    rewrite = str(payload.get("rewrite") or "").strip()
    resolved = [
        str(item).strip()
        for item in (payload.get("resolved") or [])
        if str(item).strip()
    ]
    ambiguity = str(payload.get("ambiguity") or "").strip()
    options = [str(item).strip() for item in (payload.get("options") or []) if str(item).strip()]

    if not rewrite or rewrite.lower() == raw.strip().lower():
        return _unchanged(state, "the message needed no rewriting", usage=usage)

    # The message the user sent and the conversation before it are what the rewrite is
    # checked against, and what decides whether the rewrite is used at all.
    previous = _previous_turn(session)
    conflict = conflict_between(raw, rewrite, previous)
    if conflict:
        trace = _trace(
            state,
            "rewrite",
            "rejected",
            detail=conflict,
            original=raw,
            proposed=rewrite,
        )
        return {
            "question": raw,
            "rewrite": rewrite,
            "rewrite_conflict": conflict,
            "usage": usage,
            "trace": trace,
            "phase": "rewritten",
        }

    if subject_was_chosen(raw, rewrite, previous):
        # Not a conflict with what the user wrote, but a subject nobody had established. The
        # original is used unchanged and the rest of the workflow decides, which for a
        # genuine choice of readings means asking rather than picking.
        return _unchanged(
            state,
            "the rewrite chose a subject the conversation had not established",
            usage=usage,
        )

    trace = _trace(
        state,
        "rewrite",
        "rewritten",
        detail="; ".join(resolved) or "made the message stand on its own",
        original=raw,
        rewrite=rewrite,
    )

    result: dict[str, Any] = {
        "question": rewrite,
        "rewrite": rewrite,
        "rewrite_conflict": "",
        "usage": usage,
        "trace": trace,
        "phase": "rewritten",
    }

    if ambiguity:
        # Something the rewrite could not settle. The user is asked, rather than the rewrite
        # choosing for them, and the rest of the message is carried as it stands.
        asked = Clarification(
            question=ambiguity,
            options=options,
            reason=ambiguity,
            scope="request",
            original_question=raw,
        )
        asked.asked = True
        session.pending_clarification = asked
        result["clarification"] = asked
        result["clarifications"] = [asked]
        result["trace"] = trace + [
            {
                "node": "rewrite",
                "event": "needs clarification",
                "detail": ambiguity,
                "options": options,
            }
        ]
    return result


def _unchanged(state: GraphState, reason: str, usage: Optional[TokenUsage] = None) -> dict:
    raw = state.get("raw_question") or state["question"]
    return {
        "question": raw,
        "rewrite": "",
        "rewrite_conflict": "",
        "usage": usage if usage is not None else (state.get("usage") or TokenUsage()),
        "trace": _trace(state, "rewrite", "left as written", detail=reason),
        "phase": "rewritten",
    }


def conflict_between(raw: str, rewrite: str, previous: str = "") -> str:
    """What the rewrite and the message the user sent disagree about, or "".

    Narrow on purpose. It fires on the three things that turn a rewrite into a wrong answer:
    a number that changed, a quoted name that was dropped, and a subject the user never
    mentioned being swapped for one they did. It stays quiet on paraphrase, because saying
    "Americans" as "Country = 'USA'" is the work being asked for, not a conflict.
    """
    raw, rewrite = raw or "", rewrite or ""

    lost = numbers_in(raw) - numbers_in(rewrite)
    if lost:
        return f"the rewrite does not carry the number(s) {', '.join(sorted(lost))}"

    for name in quoted_in(raw):
        if name.lower() not in rewrite.lower():
            return f"the rewrite does not carry {name!r}"

    rewritten = _named_tables(rewrite)
    if rewritten and _named_tables(raw):
        # Only when the message named a subject itself. A message that named nothing is a
        # follow-up, and naming its subject for it is what the rewrite is for.
        swapped = rewritten - (_named_tables(raw) | _named_tables(previous))
        if swapped:
            return f"the rewrite is about {', '.join(sorted(swapped))}, which was not asked for"
    return ""


def subject_was_chosen(raw: str, rewrite: str, previous: str = "") -> bool:
    """Whether the rewrite picked a subject that nothing in the conversation had established.

    A follow-up inherits its subject from the turn before it, and naming it is the work.
    A first message that names none, being told which table it was about, is a choice made
    on the user's behalf, and "How many Americans?" is exactly that: customers and employees
    both answer it. The rewrite is not the place to settle that, so it is not used at all
    for that turn and the rest of the workflow asks, as it already knows how to.
    """
    if _named_tables(raw) or _named_tables(previous):
        return False
    return bool(_named_tables(rewrite))
