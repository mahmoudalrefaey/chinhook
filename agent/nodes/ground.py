"""Semantic grounding: settling what the user's words mean in this schema."""

import json
import re
from typing import Optional

from agent import llm, schema as schema_store
from agent.nodes.clarify import _hold_task_for_clarification
from agent.nodes.common import _current_task, _is_meta, _trace, _visible_schema
from agent.nodes.prompts import _GROUND_SYSTEM, _VALUE_CHOICE_SYSTEM
from agent.state import GraphState, TaskState, TokenUsage, split_schema


def _resolve_filter_column(task: TaskState, spec: dict) -> Optional[tuple[str, str, str]]:
    """Find the real (table, column, type) a filter is aiming at, from this task's schema.

    Only columns that exist in the schema this task was given, and only the ones whose name
    or owning table matches the hint, are considered.
    """
    hint = str(spec.get("column_hint") or "").strip().lower()
    entities = {e.lower() for e in (task.entities or [])}
    if not hint and not entities:
        return None
    for table, definition in split_schema(task.schema).items():
        if entities and table.lower() not in entities:
            continue
        if not entities and hint and hint not in table.lower():
            continue
        for column in _columns_of(definition):
            if not hint or hint == column[0].lower() or hint in column[0].lower():
                return table, column[0], column[1]
    return None


def _columns_of(definition: str) -> list[tuple[str, str]]:
    """Column name and type from a table definition produced by get_table_defs()."""
    head = definition.split("\n", 1)[0]
    if "(" not in head:
        return []
    inner = head[head.index("(") + 1: head.rindex(")")]
    columns: list[tuple[str, str]] = []
    for part in inner.split(", "):
        if "(" in part and part.endswith(")"):
            name, _, dtype = part[:-1].partition(" (")
            columns.append((name, dtype))
    return columns


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _is_valid_identifier(name: str) -> bool:
    """Whether a name is safe to interpolate as a bare double-quoted identifier.

    Checked against the name as given, before anything is stripped from it. Stripping a
    quote out of a bad name first and validating what is left used to let a name containing
    one silently turn into a different, real table or column instead of being refused.
    """
    return bool(_IDENTIFIER.fullmatch(name or ""))


def _escape_ilike_value(value: str) -> str:
    """A value made safe to sit inside a single-quoted ILIKE '%...%' pattern.

    Three characters need escaping, and the order matters: the backslash used as the
    pattern's own escape character first, so a literal backslash already in the value cannot
    change how the characters after it are read, then the two characters ILIKE treats as
    wildcards, and finally the quote that would otherwise end the SQL string literal itself.
    Without the first two, a value containing "%", "_" or a trailing backslash matched far
    more or far less than the user actually typed, or broke the query outright.

    This assumes the server's standard_conforming_strings setting is left at its default
    (on), which is what makes a doubled quote the correct way to escape a quote in a plain
    string literal. That default is shared by every other place in this codebase that builds
    a query this way.
    """
    value = value.replace("\\", "\\\\")
    value = value.replace("%", "\\%").replace("_", "\\_")
    return value.replace("'", "''")


def _probe_values(table: str, column: str, value: str) -> list[str]:
    """Stored values that look like the value the user typed.

    This is what stops the workflow from assuming a word is stored the way it was said. It
    runs through the same validated, read-only, time-limited execution path as any other
    query, and returns an empty list rather than raising.
    """
    from scripts.db_module import run_sql_query

    if not _is_valid_identifier(table) or not _is_valid_identifier(column):
        return []
    literal = _escape_ilike_value(value)
    query = (
        f'SELECT DISTINCT "{column}" AS v FROM "{table}" '
        f"WHERE \"{column}\" ILIKE '%{literal}%' ESCAPE '\\' LIMIT 200"
    )
    result = run_sql_query(query)
    if not isinstance(result, dict) or "error" in result:
        return []
    return [str(row[0]) for row in result.get("rows", []) if row and row[0] is not None]


_TEXT_TYPES = {"text", "character varying", "character", "citext"}


def _candidate_columns(task: TaskState, hint: str) -> list[tuple[str, str, str]]:
    """Text columns in this task's schema worth probing for a value, best first.

    Ranked by what the task itself mentions: a table named in the question or in the task's
    entities outranks a column that merely holds a similar string. Without this, a value
    that happens to appear in two places, such as a performer name that is also a composer
    credit, can be grounded in the less likely one and the query is then quietly wrong.
    """
    haystack = " ".join(
        [task.question or task.raw, " ".join(task.entities or [])]
        + [str(spec.get("column_hint") or "") for spec in (task.filters or [])]
    ).lower()
    hint_words = [word for word in re.findall(r"[a-z0-9]+", (hint or "").lower()) if len(word) > 2]

    scored: list[tuple[int, int, str, str, str]] = []
    for table, definition in split_schema(task.schema).items():
        for column, dtype in _columns_of(definition):
            if dtype not in _TEXT_TYPES:
                continue
            score = 0
            if table.lower() in haystack:
                score += 3
            for word in hint_words:
                if column.lower() == word:
                    score += 2
                elif word in column.lower():
                    score += 1
            scored.append((-score, len(scored), table, column, dtype))
    scored.sort()
    return [(table, column, dtype) for _score, _order, table, column, dtype in scored]


def node_ground(state: GraphState) -> dict:
    task = _current_task(state)
    if task is None or _is_meta(task):
        return {"phase": "grounded"}

    usage = state.get("usage") or TokenUsage()
    trace = _trace(state, "ground", "start", detail=task.task_id)
    notes: list[str] = []
    unresolved: list[str] = []
    stored_values: list[str] = []
    nearby_pairs: list[tuple[str, str]] = []

    for spec in task.filters:
        value = str(spec.get("value") or "").strip()
        if not value:
            continue
        hint = str(spec.get("column_hint") or "").strip()
        resolved = _resolve_filter_column(task, spec)
        found: Optional[tuple[str, str, str, str]] = None
        if resolved is not None:
            table, column, dtype = resolved
            if dtype in _TEXT_TYPES:
                matches = _probe_values(table, column, value)
                if matches:
                    found = (table, column, matches[0], ", ".join(f"'{m}'" for m in matches[:6]))
        if found is None:
            # The column the reading named is not where the value lives. Look for the value
            # itself under the other text columns of this task's schema, rather than
            # concluding the value cannot exist.
            for table, column, _dtype in _candidate_columns(task, hint)[:6]:
                matches = _probe_values(table, column, value)
                if matches:
                    found = (table, column, matches[0],
                             ", ".join(f"'{m}'" for m in matches[:6]))
                    break
        if found is not None:
            table, column, stored, shown = found
            notes.append(f'"{value}" is stored in "{table}"."{column}" as {shown}.')
            spec["value"] = stored
            spec["column_hint"] = column
            spec["table_hint"] = table
        else:
            unresolved.append(value)
            # What these columns really holds, so a mapping is a choice between values that
            # exist rather than a guess about a word. Read from the database, not invented.
            nearby = _nearby_values(task, hint)
            nearby_pairs.extend(nearby)
            stored_values.extend(item[1] for item in nearby)
            if nearby and task.attempts == 0 and task.failure_reason is None:
                # A lookup first, because the values are given and the mapping is either one
                # of them or nothing. A lookup that declines is not the end of it: grounding
                # below sees the same values with the schema and the measure around them, and
                # only a value that is still unmapped after that is worth asking about.
                chosen, spent = _choose_stored_value(value, nearby, state["model"])
                usage.add(spent)
                if chosen:
                    spec["value"] = chosen
                    notes.append(f'"{value}" is stored as {chosen!r}.')
                    unresolved.remove(value)

    if task.pending_ambiguity:
        # A value that no column in this task's schema accounts for. Nothing about the
        # measure or the entity is in doubt, only which value was meant, and the user is the
        # only one who can say. The question is raised here rather than asked by the model so
        # that it carries the values the database really holds.
        return _hold_task_for_clarification(
            state, task, task.pending_ambiguity, [], usage, trace
        )

    if unresolved:
        # Grounding ran and still could not place a word the user used. Rather than put a
        # value in the filter that the data does not hold, and hand back a count of zero
        # that looks like an answer, the values that do exist are offered.
        if stored_values:
            column = _column_label(unresolved[0], task) or (
                nearby_pairs[0][0] if nearby_pairs else "This column"
            )
            values = [value for label, value in nearby_pairs if label == column] or stored_values
        else:
            column, values = "This column", stored_values
        task.pending_ambiguity = (
            f'Nothing in this database matches "{unresolved[0]}". '
            f"{column} holds: "
            f"{', '.join(repr(v) for v in values[:10]) or 'no values'}. Which did you mean?"
        )
        # The values are offered as options, so the user picks one rather than having to
        # type a value the interface cannot check for them.
        return _hold_task_for_clarification(
            state, task, task.pending_ambiguity, values[:10], usage, trace
        )

    if task.needs_grounding and not notes:
        history = state["session"].context_summary(limit=2)
        material = [f"Task: {task.question or task.raw}"]
        if history:
            material.append(f"Earlier in this conversation:\n{history}")
        material.extend([
            f"User's intent as understood: {task.intent}, metrics: {task.metrics or ['not stated']}",
            f"Filters the user gave: {json.dumps(task.filters, ensure_ascii=False)}",
            f"Words that did not match stored values: {unresolved or 'none'}",
        ])
        if stored_values:
            material.append(
                "Values these columns really hold, from the database itself:\n"
                + "\n".join(f"- {value}" for value in stored_values[:60])
            )
        material.append(f"Schema for this task:\n{_visible_schema(task)}")
        payload, spent = llm.chat_json(
            llm.grounding_model(state["model"]),
            _GROUND_SYSTEM,
            "\n".join(material),
            max_completion_tokens=700,
        )
        usage.add(spent)
        payload = payload or {}
        mapped_terms = _apply_value_mapping(task, payload, unresolved, stored_values)
        notes.extend(mapped_terms)
        ambiguity = str(payload.get("ambiguity") or "").strip()
        if ambiguity and str(payload.get("ambiguity_kind") or "").lower() == "value" and not unresolved:
            # Every value on this task has been placed on a value the data really holds, so
            # there is nothing left for the user to decide about the values. Whatever else
            # the model found here, a second look at a value it can look up is not a question
            # for the user.
            ambiguity = ""
            notes.append("The values the user used were placed on the values the data holds.")
        metric = str(payload.get("metric") or "").strip()
        if metric:
            notes.append(f"Measure to compute: {metric}.")
        ambiguity = str(payload.get("ambiguity") or "").strip()
        if ambiguity:
            # The subject of this one task is still not settled. That task stops here, with
            # the question attached to it, and the other tasks carry on. Blocking the whole
            # message would throw away work the user can already be shown.
            return _hold_task_for_clarification(
                state, task, ambiguity,
                [str(o) for o in (payload.get("options") or []) if str(o).strip()],
                usage, trace,
            )

    task.semantic = " ".join(notes)
    task.status = "grounded"
    if not task.schema:
        task.schema = schema_store.all_schema_text()
    trace = trace + [
        {
            "node": "ground",
            "event": "grounded",
            "task": task.task_id,
            "detail": task.semantic or "no mapping was needed",
        }
    ]
    return {"phase": "grounded", "usage": usage, "trace": trace}


def _choose_stored_value(
    term: str, values: list[tuple[str, str]], model: str
) -> tuple[Optional[str], TokenUsage]:
    """Which stored value, out of the ones that really exist, is the one the user meant.

    Asked as a closed question with no option to ask the user anything, because this is a
    lookup and not a decision. Letting the model raise a question here is what turned a plain
    request into two turns, and it is the one place where a question was never needed: the
    values are given, so the mapping is either one of them or nothing.
    """
    flat = [value for _column, value in values]
    if not flat:
        return None, TokenUsage(llm_calls=1)
    grouped: list[str] = [f'The user\'s word: "{term}"', "Values that are in the database:"]
    current = ""
    for column, value in values:
        if column != current:
            current = column
            grouped.append(f"Column {column}:")
        grouped.append(f"- {value}")
    payload, spent = llm.chat_json(
        llm.grounding_model(model),
        _VALUE_CHOICE_SYSTEM,
        "\n".join(grouped),
        max_completion_tokens=120,
    )
    choice = str((payload or {}).get("value") or "").strip()
    for value in flat:
        if choice and choice.lower() == value.lower():
            return value, spent
    return None, spent


def _apply_value_mapping(
    task: TaskState, payload: dict, unresolved: list[str], stored_values: list[str]
) -> list[str]:
    """Adopt a mapped value only when the database actually holds it.

    The model is asked to map a word onto a stored value and is shown what the columns
    really contain, but a word it invents is still possible, and a filter on a value that
    does not exist returns nothing at all rather than failing. Checking the mapping against
    the data is what stops a count coming back as zero.
    """
    notes: list[str] = []
    entries = payload.get("value_mappings")
    if not isinstance(entries, list):
        entries = payload.get("mappings") or []
    for mapping in entries:
        if not isinstance(mapping, dict):
            continue
        term = str(mapping.get("term") or "").strip()
        maps_to = str(mapping.get("maps_to") or mapping.get("value") or "").strip()
        if not term or not maps_to or maps_to.lower() == "none":
            continue
        note = f'"{term}" means {maps_to}.'
        if term in unresolved or any(term in value for value in unresolved):
            candidate = maps_to.split(".")[-1].strip().strip("'\"")
            spec = next(
                (s for s in task.filters if term in str(s.get("value") or "")), None
            )
            if spec is not None and not _value_exists(spec, candidate, stored_values):
                task.repair_hint = (
                    f"'{candidate}' is not a value in this database. Use one of the values "
                    "the columns really hold, or match without a filter on this column."
                )
                note += " That value was not found in the data, so it must not be used as a filter."
            elif spec is not None:
                spec["value"] = candidate
                note += f' The filter now matches the stored value "{candidate}".'
        notes.append(note)
    return notes


def _value_exists(spec: dict, candidate: str, stored_values: list[str]) -> bool:
    """Whether a mapped value is one the data really holds."""
    if any(candidate.lower() == value.lower() for value in stored_values):
        return True
    table = str(spec.get("table_hint") or "")
    column = str(spec.get("column_hint") or "")
    if not table or not column or not candidate:
        return True                      # nowhere to check; leave the decision to the query
    return bool(_probe_values(table, column, candidate))


def _column_label(value: str, task: TaskState) -> str:
    """The column a value was being looked for in, as "Table.Column"."""
    for spec in task.filters or []:
        if value in str(spec.get("value") or ""):
            table, column = str(spec.get("table_hint") or ""), str(spec.get("column_hint") or "")
            if table and column:
                return f"{table}.{column}"
    return ""


def _nearby_values(task: TaskState, hint: str) -> list[tuple[str, str]]:
    """The values the columns a filter points at really hold, as (column, value) pairs.

    Read from the database, capped, and only from the text columns of this task's own schema.
    The column the filter names is read in full before the others are sampled at all: a short
    alphabetical cut of a long column can leave out the one value the user meant, which looks
    exactly like the value not existing.
    """
    values: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for position, (table, column, _dtype) in enumerate(_candidate_columns(task, hint)[:3]):
        limit = 100 if position == 0 else 12
        for value in _sample_values(table, column, limit=limit):
            key = (column, value.lower())
            if key not in seen:
                seen.add(key)
                values.append((f"{table}.{column}", value))
    return values


def _sample_values(table: str, column: str, limit: int = 12) -> list[str]:
    from scripts.db_module import run_sql_query

    if not _is_valid_identifier(table) or not _is_valid_identifier(column):
        return []
    result = run_sql_query(
        f'SELECT DISTINCT "{column}" FROM "{table}" '
        f'WHERE "{column}" IS NOT NULL ORDER BY "{column}" LIMIT {int(limit)}'
    )
    if not isinstance(result, dict) or "error" in result:
        return []
    return [str(row[0]) for row in result.get("rows", []) if row and row[0] is not None]
