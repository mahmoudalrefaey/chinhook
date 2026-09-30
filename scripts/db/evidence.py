"""Automatic evidence generation.

A table named "invoice_line" doesn't say anywhere that it represents a completed sale rather
than, say, a price list or a playlist membership. Column names and types alone were not
enough signal for the model to tell those apart, which is what let "top selling tracks" get
answered by sorting on price instead of actual units sold.

Rather than have a person write that distinction down once (which goes stale the moment the
data changes, and has to be redone by hand for a different database entirely), a short
description of what each table actually represents is generated automatically from its
structure and a few of its real rows, at indexing time. It is regenerated automatically
whenever a table's fingerprint changes, so it never goes stale and needs no code changes if
the whole database were swapped for something else.
"""

import runtime
from scripts.db.clients import dialect as current_dialect
from scripts.db.clients import schema as current_schema
from scripts.db.introspection import restart
from scripts.db.pii import PII_COLUMN


def get_sample_rows(conn, table_name: str, limit: int = 3) -> tuple[list[str], list[tuple]]:
    """A few real rows from a table, with their column names, used to ground the evidence.

    A table that cannot be read (a view that errors, a table this login may not select from)
    gives no rows rather than failing the index: its description is then written from its
    structure alone.
    """
    table = current_dialect().qualified(current_schema(), table_name)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {table} LIMIT {int(limit)}")
            columns = [d[0] for d in cur.description]
            return columns, [tuple(row) for row in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001
        print(f"Could not sample {table_name}: {type(exc).__name__}: {exc}")
        restart(conn)
        return [], []


def _masked_sample_text(columns: list[str], rows: list[tuple]) -> str:
    """Sample rows as text, with anything that looks like a person's contact detail hidden.

    The model only needs to see the shape of a value to tell a lookup table from a
    transaction table, not the value itself, and a real customer's email or phone number has
    no business leaving the database just to answer that question.
    """
    pii_positions = {i for i, name in enumerate(columns) if PII_COLUMN.search(name)}
    if not rows:
        return "(table is currently empty)"
    lines = []
    for row in rows:
        masked = tuple(
            "[redacted]" if i in pii_positions and value is not None else value
            for i, value in enumerate(row)
        )
        lines.append(str(masked))
    return "\n".join(lines)


def generate_evidence(table_def: str, columns: list[str], sample_rows: list[tuple]) -> str:
    """Infer what a table represents in the real world, from its structure and real data.

    Uses the session's fast model when one was given, since this is a short, well-defined
    summarisation task, not something that needs the larger model's reasoning.
    """
    sample_text = _masked_sample_text(columns, sample_rows)
    prompt = (
        "You are documenting a database table for another AI that will write SQL queries "
        "against it. Given the table's structure and a few real rows, write ONE short "
        "sentence stating what this table actually represents in the real world. "
        "Specifically say whether it represents a completed transaction or event with "
        "measurable facts (a sale, a booking, a play), a reference or lookup table, or a "
        "pure relationship between two other tables with no facts of its own. Be concrete, "
        "not generic, and base this only on the structure and values shown, not the name. "
        "Some values below may be shown as [redacted]; describe the table without them.\n\n"
        f"Table definition: {table_def}\n"
        f"Sample rows:\n{sample_text}"
    )
    try:
        from agent import llm

        text, _usage = llm.chat_text(
            runtime.current().llm.small_model,
            "You describe database tables in one sentence.",
            prompt,
            max_completion_tokens=120,
        )
        return (text or "").strip()
    except Exception as e:
        # A transient failure generating evidence for one table should not fail the whole
        # index. The table still gets indexed, just without the extra context this run.
        print(f"Evidence generation failed for this table, indexing it without it: {e}")
        return ""
