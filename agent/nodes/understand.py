"""Reading a message into a structured understanding, and the tasks inside it."""

import re

from agent import llm, schema as schema_store
from agent.nodes.common import _fallback_understanding, _task_from_spec, _trace, _usage
from agent.router import same_question
from agent.nodes.prompts import _UNDERSTAND_SYSTEM
from agent.state import (
    Clarification,
    GraphState,
    MAX_TASKS_PER_REQUEST,
    TaskState,
    Understanding,
)

# Words that carry no meaning about what was asked, so a task made only of them is
# indistinguishable from one that was invented.
_FUNCTION_WORDS = {
    "how", "many", "much", "what", "which", "who", "whom", "whose", "where", "when",
    "why", "is", "are", "was", "were", "am", "be", "do", "does", "did", "can", "could",
    "would", "should", "will", "shall", "may", "might", "must", "have", "has", "had",
    "there", "here", "the", "a", "an", "of", "in", "on", "at", "to", "for", "from",
    "with", "and", "or", "but", "if", "then", "than", "that", "this", "these", "those",
    "it", "its", "me", "my", "i", "you", "your", "we", "our", "us", "they", "them",
    "get", "got", "give", "show", "tell", "list", "please", "about", "not", "no", "yes",
    "know", "want", "need", "make", "made", "say", "said", "ask", "asked", "question",
    "questions", "answer", "answered", "history", "conversation", "previous", "last",
    "first", "again", "still", "also", "just", "only", "ever", "never", "one", "two",
}


def _content_words(text: str) -> set[str]:
    return {
        word
        for word in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(word) > 2 and word not in _FUNCTION_WORDS
    }


def _grounded_in_the_conversation(tasks: list[TaskState], message: str, previous) -> bool:
    """Whether the tasks are about something the user actually said.

    The reading of a message may restate a short follow-up in words the user did not use,
    which is fine. It may not invent a subject the user has not raised at all, in this
    message or the one before it, and that is what a message objecting to being
    misunderstood looks like from here.
    """
    said = _content_words(message)
    if previous is not None:
        said |= _content_words(previous.user)
    if not said:
        return True
    asked = set()
    for task in tasks:
        asked |= _content_words(task.question)
        asked |= _content_words(" ".join(task.entities or []))
        for spec in task.filters or []:
            asked |= _content_words(str(spec.get("value") or ""))
            asked |= _content_words(str(spec.get("column_hint") or ""))
    return bool(asked & said)


def _last_answered_request(session):
    """The most recent turn the assistant actually answered something for, or None.

    A correction is a complaint about the last thing that was got wrong, and the thing that
    was got wrong is the last request that produced an answer. A greeting, a question the
    assistant asked, and a turn that ended in asking the user something are not that, which
    is why this looks for an answered task rather than taking the previous turn.
    """
    for turn in reversed(session.turns):
        if any(task.get("answer_summary") for task in turn.tasks):
            return turn
    return None


def _settled_by_the_conversation(session, question: str, options: list[str]) -> bool:
    """Whether the last turn already chose the thing this question is asking about.

    The case this settles is a follow-up: the subject was chosen a turn ago and carries
    over, so offering the user the choice again is asking a question the conversation has
    already answered. A first message has no such turn, so an entity that was never named
    still gets asked about.
    """
    if not question or not options or not session.turns:
        return False
    previous = " ".join(
        [session.turns[-1].question]
        + [task.get("question", "") for task in session.turns[-1].tasks]
    ).lower()
    if not previous.strip():
        return False
    for option in options:
        for word in re.findall(r"[a-z0-9]+", option.lower()):
            if len(word) > 2 and word in previous:
                return True
    return False


def _value_hint_lines(hits: list[dict]) -> list[str]:
    lines = []
    for hit in hits[:8]:
        lines.append(f'  "{hit["value"]}" is a real value of {hit["table"]}.{hit["column"]}')
    return lines


def node_understand(state: GraphState) -> dict:
    session = state["session"]
    model = state["model"]
    # When a message answered a question the assistant asked, the request being read is the
    # one that caused the question, with the answer in hand. The reply is never the subject.
    resumed_answer = state.get("clarification_answer") or ""
    original = state.get("original_question") or ""
    question = original if original else state["question"]
    raw = state.get("raw_question") or question

    schema_text, retrieved_names = schema_store.retrieve_for_message(question)
    table_names = schema_store.table_names_text()
    value_hits = schema_store.search_values_for(question)

    context = session.context_summary()
    last_clarification = session.clarification_history[-1]["question"] if session.clarification_history else ""
    previous = session.turns[-1] if session.turns else None
    recovered_turn = _last_answered_request(session)

    parts = [f"Tables retrieved as relevant to this message:\n{schema_text or '(none matched)'}"]
    parts.append(f"Every table name this database has: {table_names}")
    if value_hits:
        parts.append("Real values that match words in this message:\n" + "\n".join(_value_hint_lines(value_hits)))

    said = raw
    if said.strip() != question.strip():
        parts.append(
            f"Message the user sent:\n{said}\n\n"
            f"The request being read:\n{question}\n\n"
            "Read the message the user sent, against the request above."
        )
    else:
        parts.append(f"Current user message:\n{question}")
    if resumed_answer:
        parts.append(
            f"The request this may be correcting, or that this message answers a question "
            f"about:\n{question}\n\n"
            f"The assistant asked something about it, or offered a reading of it that may "
            f"have been rejected, and the user replied:\n{resumed_answer}\n\n"
            "Answer the original request above. Apply the reply to the part the assistant "
            "was unsure about, or read the original request again if the reply says the "
            "previous reading was wrong, and do not ask the same thing again."
        )
    elif recovered_turn is not None:
        parts.append(
            "The request this may be correcting, if this message is a complaint about the "
            f"last answer rather than a new question:\n{recovered_turn.question or recovered_turn.user}"
        )
    if previous is not None and not resumed_answer:
        parts.append(
            "The request the user may be referring to, if this message is about the previous "
            "exchange rather than a new one:\n"
            f"  they asked: {previous.user}\n"
            f"  it was understood as: {previous.question}"
        )
    if context and not resumed_answer:
        parts.append(f"Conversation so far in this chat:\n{context}")
    if last_clarification and not resumed_answer:
        parts.append(f"A question was asked earlier that has not been answered: {last_clarification}")
    if not session.turns and not resumed_answer:
        parts.append("This is the first message of the chat, so there is no earlier context.")

    payload, spent = llm.chat_json(model, _UNDERSTAND_SYSTEM, "\n\n".join(parts))
    usage = _usage(state, spent)
    trace = _trace(
        state,
        "understand",
        "read the question",
        detail=f"{len((payload or {}).get('tasks') or [])} task(s) detected"
        if payload
        else "the model returned unusable structure; falling back to the message as one task",
        tables=retrieved_names,
    )

    if payload is None:
        understanding = _fallback_understanding(question)
    else:
        kind = str(payload.get("kind") or "request").lower().strip()
        if kind not in {"request", "correction", "about_chat"}:
            kind = "request"
        clarity = str(payload.get("clarity") or "clear").lower().strip()
        if clarity not in {"clear", "ambiguous", "insufficient_context", "unsupported"}:
            clarity = "clear"
        raw_tasks = payload.get("tasks")
        tasks = [
            _task_from_spec(spec, index, question)
            for index, spec in enumerate(raw_tasks or [])
            if isinstance(spec, dict) and str(spec.get("question") or "").strip()
        ][:MAX_TASKS_PER_REQUEST]
        asks_something = "?" in (state["question"] or "") or "?" in (said or "")
        if not tasks and clarity in {"clear", "ambiguous", "insufficient_context"} and asks_something:
            tasks = [TaskState(task_id="T1", raw=question, question=question, intent="other")]

        # A message that asks nothing, or a set of tasks about something neither the user nor
        # the resolved request raised, is not something to run: read deterministically as the
        # recovered request instead of spending a second model call arbitrating it.
        ungrounded = bool(tasks) and not _grounded_in_the_conversation(
            tasks, f"{said} {state['question']}", previous
        )
        gave_nothing = (not asks_something) and (
            clarity in {"insufficient_context", "ambiguous"} or not tasks
        )
        if previous is not None and (ungrounded or gave_nothing) and recovered_turn is not None:
            recovered = recovered_turn.question or recovered_turn.user
            trace = trace + [{
                "node": "understand",
                "event": "fallback to the last answered request",
                "detail": f"the tasks were not grounded in what the user said: {said}",
                "recovering": recovered,
            }]
            understanding = Understanding(
                kind="correction",
                clarity="clear",
                resolved_question=recovered,
                tasks=[TaskState(task_id="T1", raw=recovered, question=recovered, intent="other")],
            )
            return {
                "understanding": understanding,
                "tasks": understanding.tasks,
                "usage": usage,
                "trace": trace,
            }

        unsupported_reason = str(payload.get("unsupported_reason") or "").strip()
        if clarity == "unsupported" and tasks:
            clarity = "clear"

        clarification = None
        spec = payload.get("clarification")
        spec = spec if isinstance(spec, dict) else {}
        text = str(spec.get("question") or "").strip()
        options = [str(o).strip() for o in (spec.get("options") or []) if str(o).strip()]
        if clarity in {"ambiguous", "insufficient_context"}:
            usable = [entry for entry in tasks if (entry.question or entry.raw).strip()]
            carried = _settled_by_the_conversation(session, text, options)
            if carried:
                clarity = "clear"
            elif resumed_answer:
                if text and last_clarification and same_question(text, last_clarification):
                    text = ""
                elif usable:
                    clarity = "clear"
            if not text and usable:
                clarity = "clear"
            elif clarity in {"ambiguous", "insufficient_context"}:
                clarification = Clarification(
                    question=text or _default_clarification_text(tasks),
                    options=options,
                    reason=str(spec.get("reason") or ""),
                    original_question=question,
                )

        understanding = Understanding(
            kind=kind,  # type: ignore[arg-type]
            clarity=clarity,  # type: ignore[arg-type]
            resolved_question=str(payload.get("resolved_question") or question).strip(),
            context_notes=str(payload.get("context_notes") or "").strip(),
            tasks=tasks,
            clarification=clarification,
            unsupported_reason=unsupported_reason,
        )

    trace.append({
        "node": "understand",
        "event": "decision",
        "detail": understanding.clarity,
        "data": {
            "resolved_question": understanding.resolved_question,
            "context_notes": understanding.context_notes,
            "tasks": [t.question or t.raw for t in understanding.tasks],
        },
    })

    return {
        "understanding": understanding,
        "tasks": understanding.tasks,
        "usage": usage,
        "trace": trace,
    }


def _default_clarification_text(tasks: list) -> str:
    asked = next((task.question for task in tasks if (task.question or task.raw).strip()), "")
    if asked:
        return f"{asked.rstrip('?')}: what exactly should I look up?"
    return "What would you like me to look up?"
