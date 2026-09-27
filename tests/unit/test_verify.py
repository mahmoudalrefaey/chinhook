"""The deterministic result checks in agent/verify.py, with no model call involved."""

from agent import verify as v
from agent.state import TaskState


def test_parse_limit_from_question():
    assert v.parse_limit_from_question("What are the top 5 selling tracks?") == 5
    assert v.parse_limit_from_question("top ten artists") == 10
    assert v.parse_limit_from_question("how many customers") is None
    assert v.parse_limit_from_question("list all albums") is None
    assert v.parse_limit_from_question("limit to 3 rows") == 3


def test_question_expects_count():
    assert v.question_expects_count("How many customers are from the USA?") is True
    assert v.question_expects_count("total number of invoices") is True
    assert v.question_expects_count("What are the top 5 tracks?") is False
    assert v.question_expects_count("show me the artists") is False


def test_statement_limit_and_referenced_tables_and_group_by():
    sql = (
        'SELECT t."name", SUM(il."quantity") FROM "invoice_line" il '
        'JOIN "track" t ON t."track_id"=il."track_id" '
        'GROUP BY t."name" ORDER BY 2 DESC LIMIT 5'
    )
    assert v.statement_limit(sql) == 5
    assert set(v.referenced_tables(sql)) == {"invoice_line", "track"}
    assert v.has_group_by(sql) is True
    assert v.has_group_by('SELECT COUNT(*) FROM "customer"') is False


def _task(**kwargs):
    defaults = dict(task_id="t1", raw="", question="")
    defaults.update(kwargs)
    return TaskState(**defaults)


def test_grouped_aggregate_with_many_rows_passes():
    # This is the bug: "how many tracks per genre" is correctly several rows, and used to
    # fail verification every time because the check demanded exactly one row for any count.
    task = _task(question="how many tracks are in each genre", intent="count")
    task.sql = 'SELECT g.name, COUNT(*) FROM track t JOIN genre g ON g.genre_id=t.genre_id GROUP BY g.name'
    verdict, checks, _ = v.verify_task(task, {"row_count": 25, "error": None})
    assert verdict == "pass"
    shape = next(c for c in checks if c["check"] == "aggregate_shape")
    assert shape["verdict"] == "pass"


def test_grouped_aggregate_with_zero_rows_still_fails():
    task = _task(question="how many tracks are in each genre", intent="count")
    task.sql = 'SELECT g.name, COUNT(*) FROM track t JOIN genre g ON g.genre_id=t.genre_id GROUP BY g.name'
    verdict, _, _ = v.verify_task(task, {"row_count": 0, "error": None})
    assert verdict == "fail"


def test_plain_aggregate_still_requires_exactly_one_row():
    task = _task(question="how many customers are there", intent="count")
    task.sql = 'SELECT COUNT(*) FROM customer'
    ok_verdict, _, _ = v.verify_task(task, {"row_count": 1, "error": None})
    assert ok_verdict == "pass"
    bad_verdict, _, _ = v.verify_task(task, {"row_count": 5, "error": None})
    assert bad_verdict == "fail"


def test_execution_error_fails_immediately():
    task = _task(question="anything", intent="other")
    task.sql = "SELECT 1"
    verdict, checks, _ = v.verify_task(task, {"error": "relation does not exist"})
    assert verdict == "fail"
    assert checks[0]["check"] == "execution"
