"""scripts/db/sql.py:validate_sql against every known bypass and a set of legitimate queries.

Every payload here was a real bypass of the check this replaced, verified against a running
database before the fix and confirmed blocked after it. This is what stops any of them
coming back unnoticed.
"""

import pytest

from scripts.db.sql import validate_sql


DISALLOWED = [
    ("CTE with DELETE", 'WITH x AS (DELETE FROM artist WHERE artist_id=-999 RETURNING *) SELECT * FROM x'),
    ("CTE with UPDATE", "WITH x AS (UPDATE artist SET name='z' WHERE artist_id=-999 RETURNING *) SELECT * FROM x"),
    ("CTE with INSERT", "WITH x AS (INSERT INTO artist(name) VALUES('z') RETURNING *) SELECT * FROM x"),
    ("SELECT INTO", 'SELECT * INTO newtbl FROM artist'),
    ("pg_read_file", "SELECT pg_read_file('/etc/passwd')"),
    ("pg_read_binary_file", "SELECT pg_read_binary_file('/etc/passwd')"),
    ("pg_ls_dir", "SELECT pg_ls_dir('/')"),
    ("lo_import", "SELECT lo_import('/etc/passwd')"),
    ("dblink", "SELECT * FROM dblink('host=evil','select 1') AS t(x int)"),
    ("pg_sleep", "SELECT pg_sleep(30)"),
    ("setval", "SELECT setval('artist_artist_id_seq', 1)"),
    ("nextval", "SELECT nextval('artist_artist_id_seq')"),
    ("stacked statements", 'SELECT 1; DROP TABLE artist'),
    ("bare delete", 'DELETE FROM artist'),
    ("bare drop", 'DROP TABLE artist'),
    ("empty string", ''),
    ("garbage", 'SELEC 1'),
]

ALLOWED = [
    ("plain select", 'SELECT * FROM artist LIMIT 5'),
    ("join, group by, order by, limit",
     'SELECT g.name, COUNT(*) FROM track t JOIN genre g ON g.genre_id=t.genre_id '
     'GROUP BY g.name ORDER BY 2 DESC LIMIT 5'),
    ("for update", 'SELECT * FROM artist FOR UPDATE'),
    ("subquery", 'SELECT COUNT(*) FROM (SELECT DISTINCT customer_id FROM invoice) s'),
    ("column merely named like a function",
     'SELECT count AS pg_sleep FROM (SELECT 1 AS count) s'),
]


@pytest.mark.parametrize("name,sql", DISALLOWED, ids=[n for n, _ in DISALLOWED])
def test_disallowed_query_is_rejected(name, sql):
    ok, reason = validate_sql(sql)
    assert ok is False, f"{name} should have been rejected but was allowed"
    assert reason


@pytest.mark.parametrize("name,sql", ALLOWED, ids=[n for n, _ in ALLOWED])
def test_legitimate_query_is_allowed(name, sql):
    ok, reason = validate_sql(sql)
    assert ok is True, f"{name} should have been allowed but was rejected: {reason}"
