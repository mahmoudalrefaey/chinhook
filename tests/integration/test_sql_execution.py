"""scripts/db/sql.py:run_sql_query against a real connection: row capping and the flag that
says a result was capped.

Needs a real Postgres with the Chinook data loaded, not Azure, so this is plain integration
rather than llm.
"""

import pytest

from scripts.db.sql import MAX_ROWS, run_sql_query

pytestmark = pytest.mark.integration


def test_a_result_under_the_cap_is_not_marked_truncated():
    result = run_sql_query('SELECT * FROM "genre"')
    assert "error" not in result
    assert len(result["rows"]) < MAX_ROWS
    assert "truncated" not in result


def test_a_result_over_the_cap_is_capped_and_marked_truncated():
    # track has well over MAX_ROWS rows in the seeded Chinook data, so this is a real cap
    # hitting, not a contrived one.
    result = run_sql_query('SELECT * FROM "track"')
    assert "error" not in result
    assert len(result["rows"]) == MAX_ROWS
    assert result["truncated"] is True
