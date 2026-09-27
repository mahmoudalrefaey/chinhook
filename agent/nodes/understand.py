"""Reading a message into a structured understanding, and the tasks inside it."""

import re
from typing import Any

from agent import llm, router, schema as schema_store
from agent.nodes.clarify import _hold_clarification
from agent.nodes.common import _current_task, _fallback_understanding, _is_meta, _task_from_spec, _trace, _usage
from agent.nodes.prompts import _UNDERSTAND_SYSTEM
from agent.state import Clarification, GraphState, TaskState, TokenUsage, Understanding


def _hold_only_what_the_question_is_about(
    tasks: list[TaskState], question: str, original: str
) -> list[TaskState]:
    """The tasks a question is actually about, when the message raised more than one.

    A question put about part of a message should not stop the rest of it. Which part it is
    about is decided by what the question and the task have in common: a question about one
    entity or one word shares that word with the one task it came from. When nothing matches
    closely, the question is taken to be about the whole message and no task is held, so the
    cautious reading is the one that happens when this cannot be told.
    """
    if len(tasks) < 2 or not question:
        return []

    def words(text: str) -> set[str]:
        return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 3}

    asked = words(question)
    if not asked:
        return []
    matches = [
        task for task in tasks
        if asked & words(f"{task.question} {task.raw} {' '.join(task.entities)}")
        and not _question_is_about_what_the_task_already_says(task)
    ]
    if len(matches) == len(tasks):
        return []
    if not matches:
        return []
    for task in matches:
        task.status = "failed"
        task.failure_kind = "ambiguous"
        task.failure_reason = f"needs clarification: {question}"
        task.semantic_ambiguity = question
        task.pending_ambiguity = question
    return matches


def _question_is_about_what_the_task_already_says(task: TaskState) -> bool:
    """Whether the task's own wording has already chosen between the options.

    A task about customers does not need to ask whether it means customers or employees. A
    task whose own wording names both of them, on the other hand, is exactly the question,
    and holding it back would answer the thing nobody asked.
    """
    question = task.pending_ambiguity
    options = [o for o in task.pending_ambiguity_options if o.strip()]
    if not question or not options:
        return False
    said = " ".join([task.question or "", task.raw, " ".join(task.entities or [])]).lower()
    named = [
        option for option in options
        if any(word and word in said for word in re.findall(r"[a-z0-9]+", option.lower()))
    ]
    return bool(named) and len(named) < len(options)


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


def _task_clarifications(tasks: list[TaskState], question: str) -> list[Clarification]:
    """Turn a clause the reading of the message could not resolve into its own question.

    A reference such as "there" or "its title" can point at more than one entity the same
    message raised. When that happens only that clause is held back, and only it is asked
    about, so the rest of the message is answered while the user decides.

    A clause that has already settled the very thing the question is about is not held: a
    task about customers does not need to ask whether it means customers.
    """
    held: list[Clarification] = []
    for task in tasks:
        if task.ambiguity_kind == "value":
            # A value is not the message's to settle. Handing it on means the data decides
            # it, and the user is asked only if the data cannot.
            task.pending_ambiguity = ""
            task.pending_ambiguity_options = []
            task.ambiguity_kind = ""
            continue
        if not task.pending_ambiguity or _question_is_about_what_the_task_already_says(task):
            task.pending_ambiguity = ""
            task.pending_ambiguity_options = []
            task.ambiguity_kind = ""
            continue
        held.append(
            Clarification(
                question=task.pending_ambiguity,
                options=list(task.pending_ambiguity_options),
                reason=task.pending_ambiguity,
                scope="task",
                original_question=question,
                task_ids=[task.task_id],
                pending_tasks=list(tasks),
            )
        )
        task.status = "failed"
        task.failure_kind = "ambiguous"
        task.failure_reason = f"needs clarification: {task.pending_ambiguity}"
        task.semantic_ambiguity = task.pending_ambiguity
    return held


def _default_clarification_text(tasks: list, options: list[str]) -> str:
    """A question to put to the user, built from what they asked, when the model gave none.

    Only reached when the reading of the message called something ambiguous but did not say
    what to ask. The wording is the user's own task text rather than a stock sentence, so a
    question that has to be asked here still refers to the thing they actually asked.
    """
    asked = next(
        (task.question for task in tasks if (task.question or task.raw).strip()),
        "",
    )
    if options:
        return f"{asked.rstrip('?')}: {' or '.join(options)}?" if asked else (
            f"Which of these did you mean: {', '.join(options)}?"
        )
    if asked:
        return f"{asked.rstrip('?')} — what exactly should I look up?"
    return "What would you like me to look up?"


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


def node_understand(state: GraphState) -> dict:
    session = state["session"]
    model = state["model"]
    # When a message answered a question the assistant asked, the request being read is the
    # one that caused the question, with the answer in hand. The reply is never the subject.
    resumed_answer = state.get("clarification_answer") or ""
    original = state.get("original_question") or ""
    question = original if original else state["question"]
    # A failed introspection is not a reason to refuse the question. catalog() degrades to
    # an empty catalog, and the schema each task retrieves is still real, so understanding
    # continues without it.
    catalog = schema_store.catalog_text()
    context = session.context_summary()
    last_clarification = session.clarification_history[-1]["question"] if session.clarification_history else ""

    parts = [f"Database catalog:\n{catalog}"]
    previous = session.turns[-1] if session.turns else None
    said = state.get("raw_question") or question
    if said.strip() != question.strip():
        # The workflow is working on a rewrite of what the user sent. Both are given, so the
        # reading is settled from the user's own words and the rewrite is used only for what
        # it made explicit.
        parts.append(
            f"Message the user sent:\n{said}\n\n"
            f"Rewritten to remove ambiguity, same intent:\n{question}\n\n"
            "Read the message the user sent. The rewrite has resolved what they were "
            "referring to; use it for that, and not to decide what they wanted."
        )
    if resumed_answer:
        parts.append(
            f"The user asked:\n{question}\n\n"
            f"The assistant then asked a question about it, and the user answered:\n"
            f"{resumed_answer}\n\n"
            "Answer the original request. Apply the answer above to the part of it the "
            "assistant was unsure about, and do not ask that question again."
        )
    else:
        if said.strip() == question.strip():
            parts.append(f"Current user message:\n{question}")
    if previous is not None and not resumed_answer:
        parts.append(
            "The request the user may be referring to, if this message is about the previous "
            "exchange rather than a new one:\n"
            f"  they asked: {previous.user}\n"
            f"  it was understood as: {previous.question}"
        )
    if context and not resumed_answer:
        # A reply to a question the assistant asked already carries the original request
        # and the answer, so the earlier turns are not repeated: the same exchange said
        # three times over is both longer and, on a hosted deployment, a prompt that can
        # trip a content filter for no gain.
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
        ]
        said = state.get("raw_question") or state["question"]
        # A message asks something when either the user's words or the rewrite of them do.
        # The rewrite is the working text, so a question it made explicit still counts.
        asks_something = "?" in (state["question"] or "") or "?" in (said or "")
        if not tasks and clarity in {"clear", "ambiguous", "insufficient_context"} and asks_something:
            # The model said the message was answerable but produced no task for it. Treat
            # the message itself as the task rather than answering nothing. Only for a
            # message that asks something: a message that asks nothing is not a question
            # that got lost, it is a statement about the exchange, and turning it into a
            # query is how "I am not asking a question about history" ends up counting
            # something.
            tasks = [TaskState(task_id="T1", raw=question, question=question, intent="other")]

        # A message that asks nothing, or a set of tasks about something neither the user nor
        # the rewrite raised, is not something to run. Either way it is what a message
        # saying "that is not what I asked" looks like from here, so the earlier request is
        # read again as the subject rather than an invented one being run. A task is grounded
        # in what the user said or in the rewrite of it, since the rewrite carries their
        # intent forward; the guard is here to catch a subject that appears in neither.
        ungrounded = bool(tasks) and not _grounded_in_the_conversation(
            tasks, f"{said} {state['question']}", previous
        )
        gave_nothing = (not asks_something) and (
            clarity in {"insufficient_context", "ambiguous"} or not tasks
        )
        if previous is not None and (kind == "correction" or ungrounded or gave_nothing):
            recovered_turn = _last_answered_request(session)
            if recovered_turn is not None:
                recovered = recovered_turn.question or recovered_turn.user
                trace = trace + [
                    {
                        "node": "understand",
                        "event": "correction",
                        "detail": f"the tasks were not in what the user said: {said}",
                        "recovering": recovered,
                    }
                ]
                retry, spent = llm.chat_json(
                    model,
                    _UNDERSTAND_SYSTEM,
                    "\n\n".join(
                        parts
                        + [
                            f"The user sent this message:\n{said}\n\n"
                            "It is not a question of its own. It is the user telling you that "
                            "what you produced from the message before is not what they asked "
                            "for. The request to answer is the one they were making:\n"
                            f"{recovered}\n\n"
                            "Answer that request. Do not answer this message, and do not answer "
                            "anything that is not in it."
                        ]
                    ),
                )
                usage.add(spent)
                kind = "correction"
                question = recovered
                original = original or recovered
                retried = [
                    _task_from_spec(spec, index, recovered)
                    for index, spec in enumerate((retry or {}).get("tasks") or [])
                    if isinstance(spec, dict) and str(spec.get("question") or "").strip()
                ]
                # Whatever the reading produced, the turn is about the recovered request,
                # so the bare request is used when nothing usable came back.
                tasks = retried or [TaskState(
                    task_id="T1", raw=recovered, question=recovered, intent="other"
                )]
                # The first reading's verdict was about a message that turned out not to be
                # a request, so the recovered request is read on its own terms from here.
                clarity = str((retry or {}).get("clarity") or "clear").lower().strip()
                if clarity not in {"clear", "ambiguous", "insufficient_context", "unsupported"}:
                    clarity = "clear"
                text = ""
                options = []

        unsupported_reason = str(payload.get("unsupported_reason") or "").strip()
        if clarity == "unsupported" and tasks:
            # Part of a message this database cannot answer does not make the rest of it
            # unanswerable. The answerable tasks run, and the part that is not supported is
            # passed on so the reply can say plainly that it has no result either.
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
                # The last turn chose the thing this question is about, so the question is
                # not asked again. A follow-up inherits its subject.
                for entry in tasks:
                    entry.pending_ambiguity = ""
                    entry.pending_ambiguity_options = []
                clarity = "clear"
            elif resumed_answer:
                # The user has already answered a question in this chat. A repeat of it is
                # not a new question, and this is the point where that has to be enforced.
                if text and last_clarification and router.same_question(text, last_clarification):
                    text = ""
                elif usable:
                    clarity = "clear"
            if not text and usable:
                # The message was called thin or ambiguous, but it resolved into a task that
                # stands on its own. A resolved task is worth more than a question about it.
                clarity = "clear"
            elif clarity in {"ambiguous", "insufficient_context"}:
                if str(spec.get("kind") or "").lower() == "value":
                    # A value is settled from the data, not from the user. Hand the whole
                    # request on: grounding looks the values up, and asks only about one it
                    # genuinely cannot place.
                    for entry in tasks:
                        entry.pending_ambiguity = ""
                        entry.pending_ambiguity_options = []
                        entry.ambiguity_kind = ""
                    clarity = "clear"
                else:
                    held = _hold_only_what_the_question_is_about(tasks, text, question)
                    if not held and tasks and all(
                        _question_is_about_what_the_task_already_says(entry) for entry in tasks
                    ):
                        # Every task already says which of the options it means, so there is
                        # nothing left to put to the user. This is what stops a message that
                        # settles its own subject from being questioned about it.
                        clarity = "clear"
                    else:
                        clarification = Clarification(
                            question=text or _default_clarification_text(tasks, options),
                            options=options,
                            reason=str(spec.get("reason") or ""),
                            scope="request" if not held else "task",
                            original_question=question,
                            task_ids=[t.task_id for t in held],
                            pending_tasks=held,
                        )

        understanding = Understanding(
            kind=kind,  # type: ignore[arg-type]
            clarity=clarity,  # type: ignore[arg-type]
            resolved_question=str(payload.get("resolved_question") or question).strip(),
            context_notes=str(payload.get("context_notes") or "").strip(),
            semantic_mappings=[
                {"term": str(m.get("term", "")), "meaning": str(m.get("meaning", ""))}
                for m in (payload.get("semantic_mappings") or [])
                if isinstance(m, dict)
            ][:8],
            tasks=tasks,
            clarification=clarification,
            unsupported_reason=unsupported_reason,
        )

    # A clause that could not be resolved is held on its own, so the clauses around it are
    # still answered and the user is asked one specific thing rather than sent back to the
    # start of the request.
    task_clarifications = _task_clarifications(understanding.tasks, question)
    for held in task_clarifications:
        _hold_clarification(session, held)

    trace.append(
        {
            "node": "understand",
            "event": "decision",
            "detail": understanding.clarity,
            "data": {
                "resolved_question": understanding.resolved_question,
                "context_notes": understanding.context_notes,
                "tasks": [t.question or t.raw for t in understanding.tasks],
                "semantic_mappings": understanding.semantic_mappings,
            },
        }
    )

    return {
        "understanding": understanding,
        "tasks": understanding.tasks,
        "current_index": 0,
        "phase": "understood",
        "usage": usage,
        "clarifications": task_clarifications,
        "trace": trace,
    }
