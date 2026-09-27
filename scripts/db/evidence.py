"""Automatic evidence generation.

# A table named "invoice_line" doesn't say anywhere that it represents a completed sale
# rather than, say, a price list or a playlist membership. Column names and types alone
# were not enough signal for the model to tell those apart, which is what let "top selling
# tracks" get answered by sorting on price instead of actual units sold.
#
# Rather than have a person write that distinction down once (which goes stale the moment
# the data changes, and has to be redone by hand for a different database entirely), a
# short description of what each table actually represents is generated automatically from
# its structure and a few of its real rows, at indexing time. It is regenerated automatically
# whenever a table's fingerprint changes, so it never goes stale and needs no code changes
# if the whole database were swapped for something else.
"""

import config


def get_sample_rows(conn, table_name: str, limit: int = 3) -> list[tuple]:
    """A few real rows from a table, used to ground the automatically generated evidence."""
    with conn.cursor() as cur:
        cur.execute(f'SELECT * FROM "{table_name}" LIMIT %s', (limit,))
        return cur.fetchall()


def generate_evidence(table_def: str, sample_rows: list[tuple]) -> str:
    """Infer what a table represents in the real world, from its structure and real data.

    Uses the nano deployment since this is a short, well-defined summarisation task, not
    something that needs the larger model's reasoning.
    """
    sample_text = "\n".join(str(row) for row in sample_rows) or "(table is currently empty)"
    prompt = (
        "You are documenting a database table for another AI that will write SQL queries "
        "against it. Given the table's structure and a few real rows, write ONE short "
        "sentence stating what this table actually represents in the real world. "
        "Specifically say whether it represents a completed transaction or event with "
        "measurable facts (a sale, a booking, a play), a reference or lookup table, or a "
        "pure relationship between two other tables with no facts of its own. Be concrete, "
        "not generic, and base this only on the structure and values shown, not the name.\n\n"
        f"Table definition: {table_def}\n"
        f"Sample rows:\n{sample_text}"
    )
    try:
        client = config.create_azure_client("gpt-4.1-nano")
        deployment = config.get_model_config("gpt-4.1-nano")["deployment"]
        resp = client.chat.completions.create(
            model=deployment,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_completion_tokens=120,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        # A transient failure generating evidence for one table should not fail the whole
        # index. The table still gets indexed, just without the extra context this run.
        print(f"Evidence generation failed for this table, indexing it without it: {e}")
        return ""
