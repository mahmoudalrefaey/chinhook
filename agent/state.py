"""Typed state for the agentic workflow.

Three levels of structure:

* TokenUsage  - the only new metrics the application reports, aggregated per user request.
* TaskState   - one per independent analytical task, so two tasks can never overwrite each
                other's SQL, columns or rows.
* ChatSession - the conversation, and the only place state is kept between turns. One
                session belongs to one chat and dies with it. There is no module level
                session anywhere in this package, which is what keeps one chat's context
                out of another's.

GraphState is the dict LangGraph threads through the nodes. It holds references to those
objects rather than raw parallel lists of strings, so a node cannot silently drop a task's
result by writing to a shared key.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, Literal, Optional, TypedDict

# The most tasks a single message is ever decomposed into. LangGraph's own recursion limit
# has to be set once, before the graph runs, at a point where the real number of tasks a
# question will turn into is not known yet, so that limit is sized against this ceiling
# instead. Enforcing the same ceiling in node_understand is what makes that sizing actually
# safe rather than merely generous: a limit sized for at most this many tasks stops
# protecting anything the moment something is allowed to produce more of them.
MAX_TASKS_PER_REQUEST = 8

Clarity = Literal["clear", "ambiguous", "insufficient_context", "unsupported"]
TaskStatus = Literal[
    "pending",
    "schema_retrieved",
    "sql_ready",
    "executed",
    "verified",
    "failed",
]
VerificationStatus = Literal["pass", "fail", "unknown"]

# Why a task did not produce an answer. The user is told in words rather than in jargon, and
# the difference matters: a question about something this database does not hold is not the
# same as a question whose query was rejected, and neither is the same as one whose result
# did not survive verification. Ambiguity and missing context stop a request before any task
# is even built, so they are not a task's own failure_kind; see Clarity and Understanding
# instead.
FailureKind = Literal[
    "schema_retrieval",
    "sql_generation",
    "sql_validation",
    "sql_execution",
    "result_verification",
    "unsupported",
]


# ---------- metrics ----------

@dataclass
class TokenUsage:
    """Token usage for one user request, summed over every LLM call it caused.

    Counts come from the provider's own usage object on each response. Nothing is
    estimated, and a request that made three calls reports three calls.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    llm_calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, other: "TokenUsage") -> "TokenUsage":
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.llm_calls += other.llm_calls
        return self

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "llm_calls": self.llm_calls,
        }


# ---------- clarification ----------

@dataclass
class Clarification:
    """A question put to the user because the request cannot be answered as understood.

    Always about the whole request, never about one task among several: asking about the one
    part of a message that is unclear while quietly running the rest used to leave the user
    reading a part-answer they had not asked for yet, next to a question about a different
    part of the same message. Holding the entire request until it is answered is simpler and
    is what a person doing this by hand would do. original_question is the message the user
    actually asked, so a reply resumes that request rather than being read as a fresh one.
    """

    question: str
    options: list[str] = field(default_factory=list)
    reason: str = ""
    resolved: bool = False
    asked: bool = False
    original_question: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "options": list(self.options),
            "reason": self.reason,
            "original_question": self.original_question,
        }


# ---------- schema cache ----------

@dataclass
class SchemaCache:
    """Table definitions already retrieved during this chat, keyed by table name.

    Conversation scoped on purpose. A new chat starts with an empty cache, so a question in
    a new chat can never be answered from a table definition that was retrieved because of
    something a different user asked earlier in another chat.
    """

    tables: dict[str, str] = field(default_factory=dict)

    def add(self, schema_text: str) -> list[str]:
        """Store every table definition in schema_text and return the names stored."""
        stored = []
        for name, definition in split_schema(schema_text).items():
            self.tables[name] = definition
            stored.append(name)
        return stored

    def lookup(self, names: list[str]) -> Optional[str]:
        """Return the definitions for names if every one of them is already cached."""
        if not names:
            return None
        if not all(name in self.tables for name in names):
            return None
        return "\n".join(self.tables[name] for name in names)


def split_schema(schema_text: str) -> dict[str, str]:
    """Split the newline-joined table definitions db_module returns into name -> definition.

    The definitions are produced by get_table_defs() and always start with a quoted table
    name, so the split is a read of an existing format rather than a new format invented
    here. The indented "Represents:" line belongs to the table above it.
    """
    tables: dict[str, str] = {}
    current: Optional[str] = None
    lines: list[str] = []
    for raw in (schema_text or "").splitlines():
        if raw.startswith('"') and "(" in raw.split("\n")[0]:
            head = raw.split("(", 1)[0].strip()
            name = head.strip('"')
            if current is not None:
                tables[current] = "\n".join(lines).strip()
            current = name
            lines = [raw]
        elif current is not None and raw.strip():
            lines.append(raw)
    if current is not None:
        tables[current] = "\n".join(lines).strip()
    return tables


# ---------- tasks ----------

@dataclass
class TaskState:
    """One independent analytical task and everything that happened to it.

    Every field a task needs is on the task. Two tasks in the same request never share a
    result slot, so a second task overwriting the first is not expressible.
    """

    task_id: str
    raw: str                              # the user's own words for this task
    question: str = ""                    # self-contained, resolved against the conversation
    intent: str = "other"
    entities: list[str] = field(default_factory=list)
    filters: list[dict[str, Any]] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    expected_limit: Optional[int] = None  # "top 5" -> 5
    expected_row_kind: str = "rows"       # rows | count | single

    semantic: str = ""                    # how the user's words map onto the schema
    needs_sql: bool = True
    clarification_answer: str = ""        # the user's reply, when this request was resumed

    schema: str = ""                      # schema context used for this task
    schema_tables: list[str] = field(default_factory=list)
    schema_from_cache: bool = False
    value_hints: list[dict[str, Any]] = field(default_factory=list)  # real values a filter might mean

    sql: Optional[str] = None
    sql_repaired: bool = False
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False               # the query matched more rows than were returned
    sql_limit: Optional[int] = None       # LIMIT as parsed out of the generated SQL

    status: TaskStatus = "pending"
    execution_status: str = "pending"     # ok | error | skipped
    error: Optional[str] = None
    validation_error: Optional[str] = None
    failure_kind: str = ""                # why it failed, when it did

    verification: str = "unknown"         # pass | fail | unknown
    verification_checks: list[dict[str, Any]] = field(default_factory=list)
    verification_notes: str = ""

    attempts: int = 0
    failure_reason: Optional[str] = None
    repair_hint: str = ""

    def as_trace(self) -> dict[str, Any]:
        """The per-task view the interface's trace panel shows."""
        return {
            "task_id": self.task_id,
            "question": self.question or self.raw,
            "intent": self.intent,
            "entities": self.entities,
            "metrics": self.metrics,
            "filters": self.filters,
            "expected_limit": self.expected_limit,
            "expected_row_kind": self.expected_row_kind,
            "semantic": self.semantic,
            "schema_tables": self.schema_tables,
            "schema_from_cache": self.schema_from_cache,
            "sql": self.sql,
            "sql_repaired": self.sql_repaired,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "status": self.status,
            "execution_status": self.execution_status,
            "verification": self.verification,
            "verification_checks": self.verification_checks,
            "attempts": self.attempts,
            "failure_kind": self.failure_kind,
            "failure_reason": self.failure_reason,
        }


# ---------- understanding ----------

@dataclass
class Understanding:
    """The structured reading of one user message, produced by the query understanding node."""

    clarity: Clarity = "clear"
    kind: Literal["request", "correction", "about_chat"] = "request"
    resolved_question: str = ""
    context_notes: str = ""           # how this message was resolved against the chat
    tasks: list[TaskState] = field(default_factory=list)
    clarification: Optional[Clarification] = None
    unsupported_reason: str = ""


# ---------- conversation ----------

@dataclass
class Turn:
    """A completed turn, kept as a compact summary rather than raw transcript.

    Follow-up questions are resolved against these, not against the whole transcript, so a
    long chat does not grow the prompt for every call.
    """

    user: str
    question: str
    intent: str = ""
    tasks: list[dict[str, Any]] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    answer: str = ""
    # True when this turn ended by asking the user something rather than answering: question
    # and tasks here are a guess at what the ambiguity might resolve to, not something the
    # user actually confirmed. A later turn's own follow-up logic needs to tell the two apart,
    # since a guess and a confirmed reading read identically as plain text otherwise.
    unresolved: bool = False


@dataclass
class ChatSession:
    """Everything remembered for the lifetime of a single chat.

    Created when a chat starts and dropped when it ends. Nothing in this class is global, and
    no other module holds a reference to a live session, so a new chat cannot see this one.
    """

    session_id: str = ""
    turns: list[Turn] = field(default_factory=list)
    schema_cache: SchemaCache = field(default_factory=SchemaCache)
    pending_clarification: Optional[Clarification] = None
    clarification_history: list[dict[str, str]] = field(default_factory=list)

    def record_turn(self, turn: Turn) -> None:
        self.turns.append(turn)

    def remember_clarification(self, question: str, options: list[str], reply: str) -> None:
        self.clarification_history.append(
            {"question": question, "options": ", ".join(options), "reply": reply}
        )
        if self.pending_clarification is not None:
            self.pending_clarification.resolved = True

    def context_summary(self, limit: int = 4) -> str:
        """Compact structured context for follow-up resolution, newest last.

        The most recent turns win the budget. Older turns stay available through
        clarification history, which is only consulted when a clarification is open.
        """
        if not self.turns and not self.clarification_history:
            return ""
        lines: list[str] = []
        for turn in self.turns[-limit:]:
            parts = [f"- user asked: {turn.user}", f"  it meant: {turn.question}"]
            if turn.intent:
                parts.append(f"  intent: {turn.intent}")
            for task in turn.tasks:
                detail = f"    task: {task.get('question', '')}"
                entities = task.get("entities") or []
                filters = task.get("filters") or []
                if entities:
                    detail += f" | entities: {', '.join(entities)}"
                if filters:
                    rendered = ", ".join(
                        f"{f.get('column_hint', '?')}={f.get('value', '?')}" for f in filters
                    )
                    detail += f" | filters: {rendered}"
                if task.get("answer_summary"):
                    detail += f" | result: {task['answer_summary']}"
                parts.append(detail)
            if turn.tables:
                parts.append(f"  tables already searched: {', '.join(turn.tables)}")
            if turn.answer:
                parts.append(f"  answered: {turn.answer[:200]}")
            lines.append("\n".join(parts))
        for entry in self.clarification_history[-limit:]:
            options = f" (options offered: {entry['options']})" if entry["options"] else ""
            lines.append(f"- clarification asked: {entry['question']}{options}")
            lines.append(f"  user answered: {entry['reply']}")
        return "\n".join(lines)

    def turn_summary(self, limit: int = 3) -> str:
        """A short exchange summary, for deciding where a message goes.

        Smaller than context_summary on purpose: the router only needs to know what kind of
        thing happened last, not the filters and row counts of every earlier task.
        """
        if not self.turns:
            return ""
        lines = []
        for turn in self.turns[-limit:]:
            lines.append(f"user: {turn.user}")
            lines.append(f"assistant: {turn.answer[:160]}")
        return "\n".join(lines)

    def open_clarification_text(self) -> str:
        if self.pending_clarification is None or self.pending_clarification.resolved:
            return ""
        options = ", ".join(self.pending_clarification.options)
        hint = f" Options offered: {options}." if options else ""
        return f"A clarification is still open: {self.pending_clarification.question}{hint}"


def merge_tasks(existing: Optional[list[TaskState]], update) -> list[TaskState]:
    """Combine parallel per-task branches back into one list, ordered and deduplicated by id.

    Each task runs in its own branch of the graph (see agent/task_graph.py), so the same key
    receives one update per branch in the same step. A plain list reducer such as
    operator.add would concatenate those updates instead of replacing a task by its id, which
    is what turns "three tasks each wrote their own result" into "three results, some of them
    stale copies of a task that was retried." Ordered by task_id so the result is always
    T1, T2, T3, ... regardless of which branch happened to finish first.
    """
    by_id = {t.task_id: t for t in (existing or [])}
    incoming = update if isinstance(update, list) else [update]
    for task in incoming:
        by_id[task.task_id] = task
    return [by_id[key] for key in sorted(by_id)]


def _concat(existing: Optional[list], update) -> list:
    """Collect one entry per task branch, in whatever order they finished.

    Used for both task_usages and task_phase_times: neither needs merging by id the way tasks
    does, since nothing ever revises an earlier task's contribution to either, only adds to
    them, and the order they are summed or maxed in afterwards does not matter.
    """
    incoming = update if isinstance(update, list) else [update]
    return [*(existing or []), *incoming]


# ---------- graph state ----------

class GraphState(TypedDict, total=False):
    """State LangGraph threads between nodes.

    trace and task_usages concatenate, and tasks merges by id, all for the same reason:
    several task branches can write to them in the same step (see agent/task_graph.py). Every
    other key is written by exactly one node per step, so a plain overwrite is what keeps a
    node from being able to append a second result under an existing key.
    """

    session: ChatSession
    model: str
    question: str
    raw_question: str
    trace: Annotated[list[dict[str, Any]], operator.add]
    usage: TokenUsage
    # One entry per task, added by that task's own branch of the graph. Kept separate from
    # usage, which is the running total for everything that is not a task (routing,
    # understanding, the final reply): the two are combined into one total only once, when
    # the turn's result is built, rather than both trying to accumulate into the same key
    # from steps that can run in parallel with each other.
    task_usages: Annotated[list[TokenUsage], _concat]
    # Seconds spent in each of retrieve/generate/execute, one dict per task, each timed
    # locally on that task's own thread (see agent/task_graph.py) rather than through the
    # shared Timing object every other stage uses. Folded into that object's own totals only
    # once every task has finished; see run_turn.
    task_phase_times: Annotated[list[dict[str, float]], _concat]
    max_attempts: int

    understanding: Understanding
    tasks: Annotated[list[TaskState], merge_tasks]

    route: str
    route_reason: str
    original_question: str
    clarification_answer: str
    clarification_resumed: bool

    clarification: Optional[Clarification]

    answer: Optional[str]
    answer_kind: str
    error: Optional[str]
    timing: Any
    # Called with each chunk of the reply as the answer node writes it, so a caller with
    # somewhere live to show it (the page, the terminal) is not stuck waiting for the whole
    # thing. None everywhere else, which is what a caller with nowhere to stream to wants.
    on_token: Optional[Callable[[str], None]]
