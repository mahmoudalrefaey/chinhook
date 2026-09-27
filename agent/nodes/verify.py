"""Checking the result answers the task, which is not the same as having run."""

import json

from agent import llm, verify as verification
from agent.nodes.common import _current_task, _is_meta, _trace
from agent.nodes.prompts import _SEMANTIC_VERIFY_SYSTEM
from agent.state import GraphState, TokenUsage


def node_verify(state: GraphState) -> dict:
    task = _current_task(state)
    if task is None:
        return {"phase": "verified"}
    if _is_meta(task):
        task.status = "verified"
        task.verification = "pass"
        trace = _trace(state, "verify", "not needed", detail=f"{task.task_id}: no database access required")
        return {"phase": "verified", "trace": trace}
    if task.status == "failed":
        trace = _trace(
            state,
            "verify",
            "skipped",
            detail=f"{task.task_id}: the task never produced a result to verify",
            task=task.task_id,
        )
        return {"phase": "verify_failed", "trace": trace}

    verdict, checks, notes = verification.verify_task(task, {
        "error": None,
        "row_count": task.row_count,
    })
    usage = state.get("usage") or TokenUsage()
    trace = _trace(
        state,
        "verify",
        "deterministic",
        detail=f"{task.task_id}: {verdict}",
        task=task.task_id,
        checks=checks,
    )

    if verdict != "fail" and task.semantic_checks_used < 1:
        # Something the checks could not settle: the shape of the result, or whether a
        # filter matched what the user actually meant. That is the one question the checks
        # cannot answer, and it is worth one cheap call rather than a confident wrong
        # answer. A failed check is never second-guessed by the model.
        task.semantic_checks_used += 1
        payload, spent = llm.chat_json(
            llm.grounding_model(state["model"]),
            _SEMANTIC_VERIFY_SYSTEM,
            f"Task the user asked for: {task.question or task.raw}\n"
            f"Intent: {task.intent}, expected result shape: {task.expected_row_kind}"
            + (f", at most {task.expected_limit} row(s)" if task.expected_limit else "")
            + ("\nFilters that were meant to be applied: " + json.dumps(task.filters, ensure_ascii=False)
               if task.filters else "")
            + "\n"
            f"Query that ran: {task.sql}\n"
            f"Columns returned: {task.columns}\n"
            f"Rows returned ({task.row_count}): "
            f"{json.dumps([list(r) for r in task.rows[:10]], default=str, ensure_ascii=False)}\n"
            f"Checks that already passed: {json.dumps(checks, ensure_ascii=False)}",
            max_completion_tokens=300,
        )
        usage.add(spent)
        payload = payload or {}
        mismatch = str(payload.get("mismatch") or "").strip()
        # A verdict of fail is only believed when the model can name the mismatch. A vague
        # objection is not a reason to throw away a result that passed every other check.
        if str(payload.get("verdict", "")).lower() == "fail" and mismatch:
            verdict = "fail"
        else:
            verdict = "pass"
            if mismatch:
                notes = (notes + " " + f"The check noted: {mismatch}").strip()
        checks = checks + [{"check": "semantic", "verdict": verdict, "detail": mismatch}]
        trace = trace + [
            {
                "node": "verify",
                "event": "semantic",
                "task": task.task_id,
                "detail": f"{verdict}: {mismatch}",
            }
        ]

    task.verification = verdict
    task.verification_checks = checks
    task.verification_notes = notes
    reasons = [
        f"{check['check']}: {check['detail']}"
        for check in checks
        if check["verdict"] == "fail" and check["detail"]
    ]

    if verdict == "pass":
        task.status = "verified"
        task.failure_reason = None
        return {"phase": "verified", "trace": trace, "usage": usage}

    task.status = "failed"
    task.failure_kind = "result_verification"
    # The reason the repair prompt and the user-facing message both read. It names the check
    # that objected rather than only the verdict, so a retry knows what to change.
    task.failure_reason = (
        f"the result does not answer the task ({'; '.join(reasons)})"
        if reasons
        else "the result does not answer the task"
    )
    return {"phase": "verify_failed", "trace": trace, "usage": usage}
