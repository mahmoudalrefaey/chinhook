"""Writing the reply, and the parts of a reply that are not prose."""

import json
import re
from typing import Any

from agent import llm, schema as schema_store
from agent.nodes.common import _is_meta, _trace
from agent.nodes.prompts import _ANSWER_SYSTEM
from agent.state import GraphState, TaskState, TokenUsage


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


def _cell(value) -> str:
    """A value as a table cell: a pipe in the data would otherwise split the row."""
    return "" if value is None else str(value).replace("|", "\\|")


def _format_result(task: TaskState, include_heading: bool = True) -> str:
    """A readable rendering of one verified result, built only from what was verified.

    Markdown, like the replies it is appended to, so it is styled the same way. With
    include_heading (the default), this is the safety net for a reply that left a verified
    result out entirely. With it off, this is just the table on its own: the heading and the
    sentence introducing it were the model's own to write, for a result too large to have it
    also hand-write every row (see table_supplied_separately in node_answer below); only the
    exact, verified rows are ever built here, in both cases.
    """
    heading = task.question or task.raw
    if task.row_count == 1 and task.rows:
        value = task.rows[0][0]
        line = f"**{value}**"
        if len(task.rows[0]) > 1:
            line += " (" + ", ".join(
                f"{column}: {cell}" for column, cell in zip(task.columns, task.rows[0], strict=True) if column
            ) + ")"
        return f"### {heading}\n\n{line}" if include_heading else line
    lines = [f"### {heading}", ""] if include_heading else []
    if task.row_count == 0:
        lines.append("The query ran and returned no rows.")
        return "\n".join(lines)
    lines.append("| " + " | ".join(task.columns) + " |")
    lines.append("| " + " | ".join("---" for _ in task.columns) + " |")
    for row in task.rows:
        lines.append("| " + " | ".join(_cell(cell) for cell in row) + " |")
    if task.truncated:
        lines.append("")
        lines.append(f"*More than {task.row_count} rows matched; only the first {task.row_count} are shown.*")
    return "\n".join(lines)


def _conversation_record(session) -> str:
    """What has been asked and answered in this chat, as material rather than as a reply."""
    if not session.turns:
        return ""
    lines = []
    for position, turn in enumerate(session.turns, start=1):
        lines.append(f"Question {position} the user asked: {turn.user}")
        lines.append(f"What it was understood as: {turn.question}")
        for task in turn.tasks:
            outcome = task.get("answer_summary") or "not answered"
            lines.append(f"  - {task.get('question')}: {outcome}")
        if turn.tables:
            lines.append(f"  tables searched for it: {', '.join(turn.tables)}")
        if turn.answer:
            lines.append(f"Answer given: {turn.answer}")
    return "\n".join(lines)


def _failure_blocks(failed: list[TaskState]) -> str:
    """What to write when nothing verified: the tasks, and how each of them failed.

    The kind of failure is given as a description of what happened rather than as the raw
    error, so the reply can explain it without quoting machinery the user did not ask about.
    """
    lines = [
        "The user asked a question and none of it could be answered from the database. "
        "Write a short reply saying so, in plain words, one line per part of their question. "
        "There are no results to report, so there is nothing to tabulate."
    ]
    for task in failed:
        kind = task.failure_kind or "unknown"
        lines.append(
            f"- What they asked: {task.question or task.raw}\n"
            f"  What happened: {kind}. Do not use the word \"kind\" and do not name any "
            "system, table, query or error. Explain it as something that happened while "
            "answering."
        )
    return "\n".join(lines)


def node_answer(state: GraphState) -> dict:
    understanding = state["understanding"]
    session = state["session"]
    tasks = state.get("tasks") or []
    model = state["model"]
    usage = state.get("usage") or TokenUsage()
    on_token = state.get("on_token")

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

    if not verified and not meta:
        # Nothing verified. A request this ambiguous to ask about never reaches this node at
        # all: node_clarify ends the turn before any task runs. What is left here is only
        # genuine failure, so the reply is written by the model from the tasks and the kind
        # of each failure. It is given the kind and not the technical reason, so a reply
        # cannot end up quoting an error message; the reason stays in the trace, where it is
        # readable by whoever is looking after this.
        detail = _failure_blocks(failed)
        text, spent = llm.chat_stream(
            model, _ANSWER_SYSTEM, detail, on_token=on_token, max_completion_tokens=600
        )
        usage.add(spent)
        text = (text or "").strip()
        if not text:
            # A reply that says nothing is worse than a plain statement of the fact.
            text = "\n".join(
                f"- {task.question or task.raw} could not be answered." for task in failed
            ) or "That could not be answered."
            if on_token:
                on_token(text)
        return {
            "answer": text,
            "answer_kind": "failed",
            "usage": usage,
            "trace": _trace(
                state,
                "answer",
                "nothing verified",
                detail=text,
                reasons={t.task_id: t.failure_reason for t in failed},
            ),
        }

    # What the user typed, not the version of it the workflow worked on: this line is what
    # makes the reply traceable back to the question they asked.
    blocks = [
        f"Original request: {state.get('raw_question') or state.get('original_question') or state['question']}"
    ]
    # A result with more than one row gets its table appended after the model's own text
    # (see the loop that builds `appended_tables` below), built from the verified rows
    # directly rather than hand-written, which is what actually saves the cost: writing out
    # a table is real, unavoidable output tokens per row, spent on data the model is only
    # ever copying, not reasoning about. Dropping "rows" from its payload here is not just
    # tidiness, it is the other half of that saving - there is nothing left for a 500-row
    # result to spend input tokens on either, only the columns and count needed to introduce
    # it. A single-value result stays exactly as before: one number is cheap to write and
    # reads better as the model's own sentence than as a one-row table appended after it.
    table_supplied = {task.task_id for task in verified if task.row_count > 1}
    for index, task in enumerate(verified, start=1):
        shape = "single value" if task.row_count <= 1 else f"{task.row_count} row(s)"
        payload: dict[str, Any] = {
            "task": task.question or task.raw,
            "intent": task.intent,
            "columns": task.columns,
            "row_count": task.row_count,
            "result_shape": shape,
        }
        if task.task_id in table_supplied:
            payload["table_supplied_separately"] = True
        else:
            payload["rows"] = [list(r) for r in task.rows[:50]]
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
        # The truncation note moves with the table: the appended table (built from
        # _format_result, which already covers this) is the one place that still needs to
        # say so once the model itself is not the one writing the table anymore.
        if task.truncated and task.task_id not in table_supplied:
            payload["truncated"] = (
                f"more than {task.row_count} rows matched; only the first "
                f"{task.row_count} are shown. Say so plainly rather than stating this as "
                "the total."
            )
        blocks.append(f"Verified result {index}:\n{json.dumps(payload, ensure_ascii=False, default=str)}")
    for task in failed:
        # Only the kind of failure goes to the reply, so an exception or a database message
        # cannot be quoted back to the user as if it were an explanation. The reason itself
        # is in the trace, where it belongs.
        blocks.append(
            f"Task that could not be answered: {task.question or task.raw}. "
            f"What happened: {task.failure_kind or 'unknown'}. Explain that in plain words "
            "as something that went wrong while answering, without naming any system, table, "
            "query or error. There is no result for it and you must not supply one, and you "
            "must not report any figure for it, including a count of zero."
        )
    if meta:
        # A task with no query has no rows, so on its own it gives the model nothing to
        # write from and it fills the gap with whatever seems plausible. What it may answer
        # these from is the schema of the database and the record of the conversation, so
        # that is what it is given.
        blocks.append(
            "Tasks that were answered without running a query. There is no result for these, "
            "and you may only answer them from the schema of the database and the "
            "conversation record given below. If what they ask is in neither, say you do not "
            "know it."
        )
        for task in meta:
            blocks.append(f"- {task.question or task.raw}")
        catalog = schema_store.catalog_text()
        if catalog:
            blocks.append(f"The schema of the connected database:\n{catalog}")
        record = _conversation_record(session)
        if record:
            blocks.append(f"The conversation so far:\n{record}")
    if understanding.unsupported_reason:
        blocks.append(
            "Part of the request that this database cannot answer: "
            f"{understanding.unsupported_reason}. There is no result for it and you must "
            "not supply one, only say that it is not something this database holds."
        )
    context = session.context_summary(limit=2)
    if context:
        blocks.append(f"Earlier in this conversation:\n{context}")

    # Enough room for a reply that carries a heading and a sentence of context for each of
    # several requested items, plus the rare single-value table small enough to be worth the
    # model writing itself, rather than one that gets cut off mid-answer. A large result's
    # own table no longer counts against this: table_supplied_separately keeps the model
    # from having to write it at all.
    text, spent = llm.chat_stream(
        model, _ANSWER_SYSTEM, "\n\n".join(blocks), on_token=on_token, max_completion_tokens=2000
    )
    usage.add(spent)

    table_tasks = [t for t in verified if t.task_id in table_supplied]
    if table_tasks:
        appended = "\n\n".join(_format_result(t, include_heading=False) for t in table_tasks)
        before_tables = len(text)
        text = f"{text.rstrip()}\n\n{appended}" if text.strip() else appended
        if on_token and len(text) > before_tables:
            on_token(text[before_tables:])

    before = len(text)
    text = _ensure_every_result_is_reported(text, verified)
    if on_token and len(text) > before:
        # Only ever appends a result the reply left out, so the new tail is exactly what
        # streamed out never carried; the caller's own display just needs the difference.
        on_token(text[before:])
    kind = "partial" if failed else "answer"
    return {
        "answer": text,
        "answer_kind": kind,
        "usage": usage,
        "trace": _trace(
            state,
            "answer",
            "written",
            detail=f"{len(verified)} verified, {len(failed)} failed",
            tasks=[t.task_id for t in verified],
            failed_tasks=[t.task_id for t in failed],
        ),
    }
