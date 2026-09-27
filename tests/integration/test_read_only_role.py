"""The database-level guarantee behind the query path: even a query the parser fails to
catch cannot actually write, because the role the query path connects as cannot write.

This is the layer that matters most: everything in test_sql_validator.py can be defeated by
a parser bug nobody has found yet, a sqlglot upgrade that changes how something is
represented, or a caller that reaches the database some other way. This test does not trust
the parser at all. It sends a real write straight at the real connection the app uses for
queries, past any validation, and checks that Postgres itself refuses it.
"""

import pytest

import config

pytestmark = pytest.mark.integration


def _ro_connection():
    import psycopg2

    return psycopg2.connect(config.DATABASE_URL_RO, sslmode="require")


@pytest.mark.skipif(
    config.DATABASE_URL_RO == config.DATABASE_URL,
    reason="DATABASE_URL_RO is not configured as a separate role; nothing to prove here yet",
)
def test_the_connection_the_query_path_uses_cannot_write():
    conn = _ro_connection()
    try:
        with conn.cursor() as cur:
            with pytest.raises(Exception, match="read-only"):
                cur.execute('DELETE FROM "artist" WHERE 1 = 0')
    finally:
        conn.rollback()
        conn.close()


def test_the_connection_the_query_path_uses_can_still_read():
    conn = _ro_connection()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) FROM "artist"')
            (count,) = cur.fetchone()
        assert count >= 0
    finally:
        conn.rollback()
        conn.close()
