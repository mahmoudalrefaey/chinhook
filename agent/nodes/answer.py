"""Writing the reply, and the parts of a reply that are not prose."""

import json
import re
from typing import Any, Optional

from agent import llm, router, schema as schema_store
from agent.nodes.common import _current_task, _is_meta, _trace, _usage
from agent.nodes.prompts import _ANSWER_SYSTEM, _CONVERSATION_SYSTEM, _FAILURE_PHRASING
from agent.state import GraphState, TaskState, TokenUsage, Understanding


def _result_marker(task: TaskState) -> str:
    """A string that only appears in the answer if this task's result is actually in it.

    For a single value that is the value itself, for a list the first row's text. It is a
    check on the wording, not a source of anything: the text is only ever built from the
    task's own verified rows.
    """
    if not task.rows:
        return ""
    first = task.rows[0]
    for cell in first:
        text = str(cell).strip()
        if text and not text.replace(".", "").replace("-", "").isdigit():
            return text
    return str(first[0]).strip() if first else ""


def _ensure_every_result_is_reported(text: str, verified: list[TaskState]) -> str:
    """Add any verified result the reply left out, formatted from the result itself.

    A reply that silently drops one of the answers is a wrong reply even though every number
    in it is true, and the model is asked to write prose, so it will sometimes leave a
    section out. The fix is not to trust the count but to check it: a result whose own
    content is nowhere in the text is appended in a plain, readable form built from the rows
    that were verified.
    """
    if not verified:
        return text
    missing = []
    for task in verified:
        marker = _result_marker(task)
        if marker and _marker_present(marker, text):
            continue
        if task.row_count > 0 and task.columns:
            missing.append(task)
    if not missing:
        return text

    parts = [text.rstrip()] if text.strip() else []
    for task in missing:
        parts.append(_format_result(task))
    return "\n\n".join(parts)


def _marker_present(marker: str, text: str) -> bool:
    """Whether a value really appears in the reply, rather than inside a longer number.

    A count of 0 is not reported by a reply that happens to contain the digit 0 in "13", so
    a number has to be present as a number, matched by value so that a rounding or a
    thousands separator in the reply still counts as reported.
    """
    if marker.replace(".", "").replace("-", "").isdigit():
        for found in re.findall(r"-?\d[\d,]*(?:\.\d+)?", text):
            try:
                if abs(float(found.replace(",", "")) - float(marker)) < 1e-9:
                    return True
            except ValueError:
                continue
        return False
    return marker in text


def _format_result(task: TaskState) -> str:
    """A readable rendering of one verified result, built only from what was verified."""
    heading = task.question or task.raw
    if task.row_count == 1 and task.rows:
        value = task.rows[0][0]
        line = f"**{heading}**\n\n{value}"
        if len(task.rows[0]) > 1:
            line += " (" + ", ".join(
                f"{column}: {cell}" for column, cell in zip(task.columns, task.rows[0]) if column
            ) + ")"
        return line
    lines = [f"**{heading}**", ""]
    if task.row_count == 0:
        lines.append("The query ran and returned no rows.")
        return "\n".join(lines)
    lines.append("| " + " | ".join(task.columns) + " |")
    lines.append("| " + " | ".join("---" for _ in task.columns) + " |")
    for row in task.rows:
        lines.append("| " + " | ".join("" if cell is None else str(cell) for cell in row) + " |")
    return "\n".join(lines)


def node_conversation_answer(state: GraphState) -> dict:
    """Answer a question about this chat, or about the assistant, from what is known.

    The questions with a definite answer are answered from the record in code, which is both
    exact and free. Anything else is put to the model with the same record, so it can only
    say what was said. Neither path can produce a query: nothing here reaches the database.
    """
    session = state["session"]
    model = state["model"]
    usage = state.get("usage") or TokenUsage()
    question = state["question"]
    route = state.get("route")

    answered = _answer_from_record(question, session, route)
    if answered is not None:
        return {
            "answer": answered,
            "answer_kind": route or "conversation",
            "usage": usage,
            "trace": _trace(state, "conversation", "answered from the chat record",
                            detail="no data was looked up"),
        }

    record = _conversation_record(session)
    if not record:
        text = (
            "This is the first thing in our chat, so there is no earlier question or answer "
            "to look back at."
        )
        return {
            "answer": text,
            "answer_kind": "conversation",
            "usage": usage,
            "trace": _trace(state, "conversation", "nothing to look back at"),
        }

    text, spent = llm.chat_text(
        llm.grounding_model(model),
        _CONVERSATION_SYSTEM,
        f"Question about the conversation: {question}\n\nThe conversation so far:\n{record}",
        max_completion_tokens=400,
    )
    usage.add(spent)
    return {
        "answer": text,
        "answer_kind": route or "conversation",
        "usage": usage,
        "trace": _trace(state, "conversation", "answered from the chat record",
                        detail="no data was looked up"),
    }


def _conversation_record(session) -> str:
    if not session.turns:
        return ""
    lines = []
    for position, turn in enumerate(session.turns, start=1):
        lines.append(f"Question {position} the user asked: {turn.user}")
        lines.append(f"What it was understood as: {turn.question}")
        for task in turn.tasks:
            outcome = task.get("answer_summary") or "not answered"
            lines.append(f"  - {task.get('question')}: {outcome}")
        if turn.answer:
            lines.append(f"Answer given: {turn.answer}")
    return "\n".join(lines)


def _answer_from_record(question: str, session, route: Optional[str]) -> Optional[str]:
    """The conversation questions that have one exact answer, answered from the record.

    Only questions whose answer is a fact about the exchange itself, so the answer is decided
    here rather than estimated. Anything else returns None and is put to the model with the
    same record.
    """
    text = router.normalize(question)
    turns = session.turns
    if not turns:
        return None

    wants_first = bool(re.search(r"\b(first|earliest|initial|very first)\b", text))
    wants_last = bool(re.search(r"\b(last|latest|most recent|previous|preceding|just now)\b", text))
    asks_count = bool(re.search(r"\bhow many\b", text))
    asks_answer = bool(re.search(r"\b(answer|reply|respond|said|told me)\b", text))
    asks_question = bool(re.search(r"\b(ask|question|prompt|request)\b", text))

    if asks_count and re.search(r"\b(question|message|turn|ask)s?\b", text):
        return f"You have asked {len(turns)} question{'s' if len(turns) != 1 else ''} so far."

    if route == "meta":
        # A question about the assistant is answered from what the chat knows about it,
        # rather than by listing the questions, which is a different question.
        return _describe_last_turn(session, question)

    if asks_answer and wants_last:
        return turns[-1].answer or "I have not answered anything yet."
    if asks_answer and wants_first:
        return turns[0].answer or "I have not answered anything yet."
    if asks_question and wants_first:
        return f"The first thing you asked was: \"{turns[0].user}\""
    if asks_question and wants_last:
        return f"The last thing you asked was: \"{turns[-1].user}\""
    if wants_first and asks_question:
        return f"The first thing you asked was: \"{turns[0].user}\""
    if asks_question:
        return (
            "You have asked:\n"
            + "\n".join(f"{i}. {turn.user}" for i, turn in enumerate(turns, start=1))
        )

    if route == "meta":
        return _describe_last_turn(session)
    return None


def _capabilities() -> str:
    """What this assistant can be asked about, taken from the database that is connected.

    Read from the schema rather than written here, so it describes whatever is actually
    there and stays right when the connection points somewhere else.
    """
    tables = sorted(schema_store.catalog())
    if not tables:
        return "I answer questions about the connected database."
    listed = ", ".join(tables[:-1]) + f" and {tables[-1]}" if len(tables) > 1 else tables[0]
    return (
        f"I answer questions about the connected database, which has {len(tables)} "
        f"tables: {listed}. I also remember this conversation, so a follow-up continues the "
        f"question before it."
    )


def _describe_last_turn(session, asked: str = "") -> str:
    """What the assistant understood, could do, or has looked at, from the chat's own state."""
    last = session.turns[-1]
    questions = [task.get("question") for task in last.tasks if task.get("question")]
    tables = ", ".join(last.tables) if last.tables else "none yet"
    asks_about_capabilities = bool(
        re.search(r"\b(can you do|what can you|what do you do|how do i|what are you able|"
                  r"help|what are you|who are you)\b", asked or "", re.IGNORECASE)
    )
    parts = []
    if asks_about_capabilities:
        parts.append(_capabilities())
        parts.append("")
    parts.append(f'Your last message was: "{last.user}"')
    parts.append(f"I read that as: {last.question}")
    if questions:
        parts.append("I split it into:\n" + "\n".join(f"- {q}" for q in questions))
    parts.append(f"Tables I have looked at in this chat: {tables}.")
    if not asks_about_capabilities:
        parts.append("")
        parts.append(_capabilities())
    return "\n".join(parts)


def node_answer(state: GraphState) -> dict:
    understanding = state["understanding"]
    session = state["session"]
    tasks = state.get("tasks") or []
    model = state["model"]
    usage = state.get("usage") or TokenUsage()

    if understanding.clarity == "unsupported":
        reason = understanding.unsupported_reason or "it is not something this database can answer."
        text = f"I can't answer that: {reason}"
        return {
            "answer": text,
            "answer_kind": "unsupported",
            "usage": usage,
            "trace": _trace(state, "answer", "unsupported", detail=reason),
        }

    verified = [t for t in tasks if t.status == "verified" and not _is_meta(t)]
    failed = [t for t in tasks if t.status == "failed"]
    meta = [t for t in tasks if _is_meta(t)]
    pending_questions = list(state.get("clarifications") or [])
    needs_clarification = [t for t in failed if t.semantic_ambiguity]
    broken = [t for t in failed if not t.semantic_ambiguity]

    if not verified and not meta:
        if pending_questions:
            question = pending_questions[0]
            trace = _trace(
                state,
                "answer",
                "asked for clarification",
                detail=question.question,
            )
            return {
                "answer": question.question,
                "answer_kind": "clarification",
                "clarification": question,
                "usage": usage,
                "trace": trace,
            }
        # Nothing verified and nothing to ask. Written here rather than put to a model with no
        # results to write from, and in the plain words each failure is named by. The
        # technical reason is in the trace, where it belongs, rather than in the reply.
        parts = [
            f"{task.question or task.raw}: "
            f"{_FAILURE_PHRASING.get(task.failure_kind, 'it could not be answered.')}"
            for task in failed
        ]
        detail = " ".join(parts) if parts else "No part of this could be answered."
        return {
            "answer": detail,
            "answer_kind": "failed",
            "usage": usage,
            "trace": _trace(
                state,
                "answer",
                "nothing verified",
                detail=detail,
                reasons={t.task_id: t.failure_reason for t in failed},
            ),
        }

    blocks = [f"Original request: {state.get('original_question') or state['question']}"]
    for index, task in enumerate(verified, start=1):
        shape = "single value" if task.row_count <= 1 else f"{task.row_count} row(s)"
        payload: dict[str, Any] = {
            "task": task.question or task.raw,
            "intent": task.intent,
            "columns": task.columns,
            "row_count": task.row_count,
            "rows": [list(r) for r in task.rows[:50]],
            "result_shape": shape,
        }
        if task.semantic:
            payload["measure"] = task.semantic
        if task.metrics:
            payload["metric_requested"] = ", ".join(task.metrics)
        if task.filters:
            payload["filters_applied"] = [
                f"{f.get('column_hint', '?')} = {f.get('value', '?')}" for f in task.filters
            ]
        if task.verification_notes:
            payload["note"] = task.verification_notes
        blocks.append(f"Verified result {index}:\n{json.dumps(payload, ensure_ascii=False, default=str)}")
    for task in needs_clarification:
        blocks.append(
            f"Task awaiting an answer from the user: {task.question or task.raw}. "
            f"Put this question to the user, word for word, at the end of your reply: "
            f"{task.semantic_ambiguity}"
        )
    for task in broken:
        # Only the plain description of the failure goes to the reply, so an exception or a
        # database message cannot be quoted back to the user as if it were an explanation.
        blocks.append(
            f"Task that could not be answered: {task.question or task.raw}. "
            f"What went wrong: {_FAILURE_PHRASING.get(task.failure_kind, 'it could not be answered.')} "
            "There is no result for it and you must not supply one, and you must not report "
            "any figure for it, including a count of zero."
        )
    if meta:
        blocks.append("Tasks that need no database access: " + ", ".join(t.question or t.raw for t in meta))
    if understanding.unsupported_reason:
        blocks.append(
            "Part of the request that this database cannot answer: "
            f"{understanding.unsupported_reason}. There is no result for it and you must "
            "not supply one, only say that it is not something this database holds."
        )
    context = session.context_summary(limit=2)
    if context:
        blocks.append(f"Earlier in this conversation:\n{context}")

    text, spent = llm.chat_text(
        model, _ANSWER_SYSTEM, "\n\n".join(blocks), max_completion_tokens=1400
    )
    usage.add(spent)
    text = _ensure_every_result_is_reported(text, verified)
    kind = "partial" if failed or pending_questions else "answer"
    payload: dict[str, Any] = {
        "answer": text,
        "answer_kind": kind,
        "usage": usage,
        "trace": _trace(
            state,
            "answer",
            "written",
            detail=f"{len(verified)} verified, {len(broken)} failed, "
                   f"{len(needs_clarification)} awaiting an answer from the user",
            tasks=[t.task_id for t in verified],
            failed_tasks=[t.task_id for t in broken],
            clarification_tasks=[t.task_id for t in needs_clarification],
        ),
    }
    if pending_questions:
        payload["clarification"] = pending_questions[0]
    return payload
