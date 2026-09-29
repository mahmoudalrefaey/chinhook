"""The deterministic result checks in agent/verify.py, with no model call involved."""

from agent import verify as v
from agent.state import TaskState


def test_parse_limit_from_question():
    assert v.parse_limit_from_question("What are the top 5 selling tracks?") == 5
    assert v.parse_limit_from_question("top ten artists") == 10
    assert v.parse_limit_from_question("how many customers") is None
    assert v.parse_limit_from_question("list all albums") is None
    assert v.parse_limit_from_question("limit to 3 rows") == 3


def test_parse_limit_from_question_covers_every_number_word_the_regex_accepts():
    # The regex used to accept four/six/seven/eight/nine but the lookup dict did not have
    # them, so "top four artists" raised a KeyError and took the whole turn down with it.
    words = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
        "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    }
    for word, number in words.items():
        assert v.parse_limit_from_question(f"top {word} artists") == number


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


def test_referenced_tables_excludes_the_querys_own_cte_names():
    # A CTE's name is not a table the query reads from the database, however the rest of the
    # query goes on to reference it; sqlglot represents both the same way (an exp.Table
    # node), so this used to report the CTE's own name as an unknown table and reject an
    # otherwise valid query.
    sql = (
        'WITH "spend" AS (SELECT "customer_id", SUM("total") AS "t" FROM "invoice" '
        'GROUP BY "customer_id") '
        'SELECT "customer_id" FROM "spend" WHERE "t" > 10'
    )
    assert set(v.referenced_tables(sql)) == {"invoice"}


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


def test_a_self_imposed_limit_fails_when_the_user_never_asked_for_one():
    # The bug this closes: models writing this kind of query lean toward a small LIMIT far
    # more often than a person would ask for one, so "list every genre" silently came back
    # with 5 of the 25 real rows and nothing said so.
    task = _task(question="list every genre name", intent="list")
    task.sql = 'SELECT name FROM genre LIMIT 5'
    task.sql_limit = 5
    verdict, checks, _ = v.verify_task(task, {"row_count": 5, "error": None})
    assert verdict == "fail"
    cardinality = next(c for c in checks if c["check"] == "cardinality")
    assert cardinality["verdict"] == "fail"


def test_a_limit_is_fine_when_the_user_actually_asked_for_one():
    task = _task(question="top 5 selling tracks", intent="rank", expected_limit=5)
    task.sql = 'SELECT name FROM track ORDER BY sales DESC LIMIT 5'
    task.sql_limit = 5
    verdict, _, _ = v.verify_task(task, {"row_count": 5, "error": None})
    assert verdict == "pass"


def test_no_limit_at_all_is_fine_when_the_user_never_asked_for_one():
    task = _task(question="list every genre name", intent="list")
    task.sql = 'SELECT name FROM genre'
    task.sql_limit = None
    verdict, _, _ = v.verify_task(task, {"row_count": 25, "error": None})
    assert verdict != "fail"
