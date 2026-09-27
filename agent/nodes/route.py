"""Deciding where a message goes, before anything is retrieved, written or run.

Three of the six routes never reach the database at all, so this is also where a turn that
would have cost a search, a query and two model calls for nothing is saved."""

from agent import router
from agent.nodes.common import _current_task, _is_meta, _trace, _usage
from agent.state import GraphState, TaskState, TokenUsage


def node_route(state: GraphState) -> dict:
    """Decide where this message goes before anything is retrieved, written or run.

    Three of the six routes never reach the database at all, so this is also where a turn
    that would have cost a search, a query and two model calls for nothing is saved.
    """
    session = state["session"]
    question = state["question"]
    # What the user typed, beside the message the workflow is working on. The router places
    # the message on the user's own words; the rewrite only explains what they referred to.
    raw = state.get("raw_question") or question
    model = state["model"]

    decision = router.classify(question, session, model, raw=raw)
    if decision.route == "greeting" and "?" in (raw or ""):
        # A question is not small talk. A rewrite can read as a statement even when the
        # message the user sent is plainly a question, and nothing asked is being read as
        # small talk because of how the rewrite was phrased.
        decision = router.RouteDecision(
            route="database",
            reason="the message the user sent is a question, so it is not small talk",
            usage=decision.usage,
        )
    usage = _usage(state, decision.usage)
    trace = _trace(
        state,
        "route",
        decision.route,
        detail=decision.reason or question[:80],
    )

    if decision.route == "greeting":
        return {
            "route": decision.route,
            "route_reason": decision.reason,
            "usage": usage,
            "trace": trace,
            "phase": "routed",
        }

    if decision.route in {"conversation", "meta", "database", "followup"} and (
        decision.reply is None or not decision.reply.resolved
    ):
        # The user moved on. An open question they are not answering any more is not left
        # waiting to swallow their next message.
        session.pending_clarification = None

    if decision.route == "clarification" and decision.reply is not None and decision.reply.resolved:
        resumed = _resume_after_clarification(state, decision.reply, trace)
        resumed["usage"] = usage
        return resumed
    if decision.route == "clarification":
        # An open question was not answered in a way that can be read. Nothing is
        # restarted: the same question goes back with a note about what was not understood,
        # so the user is asked about the missing part rather than the whole request again.
        pending = state["session"].pending_clarification
        detail = (
            f"reply could not be matched to the options of: {pending.question}"
            if pending is not None
            else "reply could not be matched"
        )
        trace = trace + [
            {
                "node": "clarify",
                "event": "not understood",
                "detail": detail,
                "reply": question,
            }
        ]
        if pending is not None:
            return {
                "route": decision.route,
                "route_reason": decision.reason,
                "clarification": pending,
                "clarification_answer": question,
                "clarification_rejected": True,
                "usage": usage,
                "trace": trace,
                "phase": "routed",
            }
        return {
            "route": "database",
            "route_reason": decision.reason,
            "usage": usage,
            "trace": trace,
            "phase": "routed",
        }

    return {
        "route": decision.route,
        "route_reason": decision.reason,
        "usage": usage,
        "trace": trace,
        "phase": "routed",
    }


def _resume_after_clarification(
    state: GraphState, reply: router.ClarificationReply, trace: list[dict]
) -> dict:
    """Put the answered question back and carry on from where it stopped.

    A reply to a clarification is not a new request. The request that caused the question is
    restored, the work already done for it is kept, and only the part that depended on the
    answer is run again. That is what stops a user being asked the same thing twice, and it
    is why a two part question does not start over when the answer to one part arrives.
    """
    session = state["session"]
    pending = session.pending_clarification
    if pending is None:  # nothing to resume; treat the message as an ordinary question
        return {"route": "database", "trace": trace, "phase": "routed"}

    # The user's own words are what is recorded and what the question they answered is
    # recorded against, whichever version of the message the workflow was working on.
    said = state.get("raw_question") or state["question"]

    # The reply is now understood, so the exchange is recorded and the open question is
    # closed. Everything after this point is the original request, not a new one.
    session.remember_clarification(pending.question, pending.options, said)
    session.pending_clarification = None

    trace = trace + [
        {
            "node": "clarify",
            "event": "resolved",
            "detail": f"answered with: {said}",
            "options": list(pending.options),
            "selection": list(reply.selection or []),
        }
    ]

    if reply.negative:
        return {
            "route": "database",
            "trace": trace + [{"node": "clarify", "event": "declined",
                               "detail": "the user did not accept the reading on offer"}],
            "clarification_answer": said,
            "phase": "routed",
        }

    original = pending.original_question or said
    if pending.scope == "task" and pending.pending_tasks:
        tasks = pending.pending_tasks
        affected = set(pending.task_ids)
        for task in tasks:
            if task.task_id in affected:
                task.reset_for_retry()
                task.clarification_answer = said
        index = next(
            (i for i, task in enumerate(tasks) if task.task_id in affected), 0
        )
        return {
            "route": "followup",
            "route_reason": f"resumed after an answer about {', '.join(sorted(affected))}",
            "original_question": original,
            "clarification_answer": said,
            "clarification_resumed": True,
            "tasks": tasks,
            "current_index": index,
            "clarification": None,
            "clarifications": [],
            "trace": trace,
            "phase": "routed",
        }

    # The whole request was on hold, so the original message is read again with the answer
    # in hand. The reply is never read on its own.
    return {
        "route": "followup",
        "route_reason": "re-reading the original request with the answer applied",
        "original_question": original,
        "clarification_answer": said,
        "clarification_resumed": True,
        "tasks": [],
        "current_index": 0,
        "clarification": None,
        "clarifications": [],
        "trace": trace,
        "phase": "routed",
    }
