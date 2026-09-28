"""Task and result verification.

This is deliberately separate from SQL validation. Validation answers "is this valid and
safe SQL" and already exists in scripts.db_module.validate_sql, which this module calls
rather than reimplements. Verification answers a different question: "does what came back
actually satisfy the task the user asked for".

Every check here is deterministic. They read the task, the generated SQL and the returned
rows, and they never call a model. A check returns pass, fail or unknown, and only when all
of them are unknown does the node fall back to a semantic check with the model.
"""

from __future__ import annotations

import re
from typing import Any, Optional

import sqlglot

# A "top 5" in the user's own words. Used as a cross-check on what the understanding node
# reported, not as the only source, so a misread "top 5" cannot talk the verifier out of a
# cardinality check.
_LIMIT_WORDS = r"(?:top|first|best|most)\s+(?:\d+|ten|five|twenty)?"
_AGGREGATE_INTENTS = {"count", "sum", "average", "aggregate"}
_SINGLE_INTENTS = {"max", "min"}


def parse_limit_from_question(question: str) -> Optional[int]:
    """A row limit stated in plain language, e.g. 'top 5' -> 5."""
    if not question:
        return None
    words = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
        "eight": 8, "nine": 9, "ten": 10, "twenty": 20, "fifty": 50,
    }
    match = re.search(
        r"\b(?:top|first|best|most)\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten|twenty|fifty)\b",
        question.lower(),
    )
    if match:
        token = match.group(1)
        return int(token) if token.isdigit() else words[token]
    match = re.search(r"\blimit\s+(?:to\s+)?(\d+)\b", question.lower())
    return int(match.group(1)) if match else None


def question_expects_count(question: str) -> bool:
    """True when the question itself asks for a number rather than a list.

    Read from the user's own words rather than from the intent the understanding node
    reported, so a misread intent cannot talk the verifier out of checking the shape of a
    result that was supposed to be a single number.
    """
    if not question:
        return False
    return bool(
        re.search(
            r"\b(how many|number of|count of|total number|how much)\b", question.lower()
        )
    )


def statement_limit(sql: Optional[str]) -> Optional[int]:
    """The LIMIT in the generated SQL, read from the parsed statement rather than by regex."""
    if not sql:
        return None
    try:
        expression = sqlglot.parse_one(sql, read="postgres")
    except Exception:
        return None
    limit = expression.args.get("limit")
    if limit is None:
        return None
    if isinstance(limit, int):
        return limit
    try:
        return int(limit.expression.name)
    except Exception:
        return None


def tables_in(expression) -> list[str]:
    """Table names an already-parsed query reads.

    A schema-qualified table is reported with its schema, because a name that says which
    schema it is in says something a bare name does not. Split out from referenced_tables so
    a caller that has already parsed the query, such as the task graph's own validity check,
    reads the same tree it just built rather than paying for a second parse of the same SQL.
    """
    names = []
    for table in expression.find_all(sqlglot.exp.Table):
        name = f"{table.db}.{table.name}" if table.db else table.name
        names.append(name)
    return names


def referenced_tables(sql: Optional[str]) -> list[str]:
    """Table names the query reads, parsing it first. See tables_in for the parsed form."""
    if not sql:
        return []
    try:
        expression = sqlglot.parse_one(sql, read="postgres")
    except Exception:
        return []
    return tables_in(expression)


def has_group_by(sql: Optional[str]) -> bool:
    if not sql:
        return False
    try:
        expression = sqlglot.parse_one(sql, read="postgres")
    except Exception:
        return False
    return expression.args.get("group") is not None


def _check(name: str, verdict: str, detail: str) -> dict[str, Any]:
    return {"check": name, "verdict": verdict, "detail": detail}


def verify_task(task, execution: dict[str, Any]) -> tuple[str, list[dict[str, Any]], str]:
    """Check a task's execution result against what the task asked for.

    Returns (verdict, checks, notes) where verdict is pass, fail or unknown. A fail here
    sends the task into repair, not into the answer.
    """
    checks: list[dict[str, Any]] = []
    notes: list[str] = []

    if task.status == "failed" and not task.sql:
        return "fail", [_check("task", "fail", task.failure_reason or "task never produced SQL")], ""

    if execution.get("error"):
        checks.append(_check("execution", "fail", str(execution["error"])))
        return "fail", checks, ""

    row_count = int(execution.get("row_count") or 0)
    checks.append(_check("execution", "pass", f"query ran, {row_count} row(s) returned"))

    # Cardinality. This is the check that separates "top 5" from "thirteen rows and a good
    # explanation of why", which is what the original pipeline could not catch.
    expected_limit = task.expected_limit
    if expected_limit is None:
        expected_limit = parse_limit_from_question(task.question or task.raw)
    if expected_limit is not None:
        if row_count > expected_limit:
            checks.append(
                _check(
                    "cardinality",
                    "fail",
                    f"task asked for at most {expected_limit} row(s) but {row_count} came back",
                )
            )
            return "fail", checks, ""
        if task.sql_limit is not None and task.sql_limit > expected_limit:
            checks.append(
                _check(
                    "cardinality",
                    "fail",
                    f"SQL limits to {task.sql_limit} but the task asked for {expected_limit}",
                )
            )
            return "fail", checks, ""
        checks.append(_check("cardinality", "pass", f"within the requested {expected_limit} row(s)"))
    elif task.sql_limit is not None and task.expected_row_kind == "rows":
        # The user never asked for a specific count, and the shape this task expects is
        # "every matching row", not a top-N ranking. A LIMIT here is not a cautious default,
        # it is silently dropping rows nobody asked to have dropped: models writing this kind
        # of query lean toward a small LIMIT far more often than a person would ask for one.
        checks.append(
            _check(
                "cardinality",
                "fail",
                f"the query limits to {task.sql_limit} rows but the user asked for every "
                "matching row, with no number mentioned",
            )
        )
        return "fail", checks, ""
    else:
        checks.append(_check("cardinality", "unknown", "no row count was specified by the user"))

    # Aggregate shape. A count or a sum without a GROUP BY has exactly one number in it. With
    # a GROUP BY, "how many tracks are in each genre" is correctly several rows, one per
    # genre, and the right count of them depends entirely on how much data there is, which
    # this check has no way to know in advance. Asserting exactly one row for both shapes
    # used to fail every grouped aggregate that was actually correct, on every attempt, until
    # the task gave up and reported a real answer as unanswerable.
    aggregate_intent = task.intent in _AGGREGATE_INTENTS or question_expects_count(
        task.question or task.raw
    )
    if aggregate_intent and has_group_by(task.sql):
        if row_count == 0:
            checks.append(
                _check("aggregate_shape", "fail", "grouped aggregate query returned no rows at all")
            )
            return "fail", checks, ""
        checks.append(
            _check("aggregate_shape", "pass", f"grouped aggregate returned {row_count} row(s)")
        )
    elif aggregate_intent:
        if row_count == 1:
            checks.append(_check("aggregate_shape", "pass", "single aggregate value returned"))
        elif row_count == 0:
            checks.append(
                _check("aggregate_shape", "fail", "aggregate query returned no row at all")
            )
            return "fail", checks, ""
        else:
            checks.append(
                _check(
                    "aggregate_shape",
                    "fail",
                    f"aggregate task returned {row_count} rows instead of one value",
                )
            )
            return "fail", checks, ""
    elif task.intent in _SINGLE_INTENTS and not has_group_by(task.sql):
        if row_count > 1:
            checks.append(
                _check(
                    "single_row",
                    "fail",
                    f"task asked for the single highest or lowest value but {row_count} rows came back",
                )
            )
            return "fail", checks, ""
        checks.append(_check("single_row", "pass", "one row returned"))
    else:
        checks.append(_check("aggregate_shape", "unknown", "not an aggregate task"))

    # Entity agreement. A count of customers answered with a count of employees is valid
    # SQL and the wrong answer, and it is caught here without a model in the loop.
    if task.entities:
        from agent.schema import catalog

        known = set(catalog())
        wanted = {e for e in task.entities if e in known}
        if wanted:
            used = {t for t in referenced_tables(task.sql) if t in known}
            if used and not (used & wanted):
                checks.append(
                    _check(
                        "entity",
                        "fail",
                        f"task is about {sorted(wanted)} but the query only reads {sorted(used)}",
                    )
                )
                return "fail", checks, ""
            checks.append(
                _check("entity", "pass", f"query reads {sorted(used & wanted) or sorted(used)}")
            )
        else:
            checks.append(_check("entity", "unknown", "entities are not table names in this database"))
    else:
        checks.append(_check("entity", "unknown", "task named no entity"))

    if row_count == 0 and task.intent in {"list", "rank", "lookup", "other"}:
        checks.append(
            _check("empty_result", "unknown", "no rows matched; this may be correct or a bad filter")
        )
        notes.append("The query ran and matched nothing. Check the filter values against real data.")

    # Asking for the most, the least or the highest of something, and getting nothing back,
    # means the query did not reach the rows rather than that the answer is none. Reported
    # as a failure so it is repaired or said to be unanswerable, instead of being passed off
    # as an empty result.
    if row_count == 0 and (
        task.intent in {"rank", "max", "min"} or task.expected_row_kind in {"single", "limited"}
    ):
        checks.append(
            _check("empty_ranking", "fail",
                   "the query asked for the highest, lowest or a ranked set and returned no rows")
        )
        return "fail", checks, " ".join(notes)

    if all(entry["verdict"] == "unknown" for entry in checks if entry["check"] != "execution"):
        return "unknown", checks, " ".join(notes)
    return "pass", checks, " ".join(notes)
