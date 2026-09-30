"""Which real values are worth indexing on their own, and what they are.

A question rarely names a table or a column; it names a value ("Americans", "rock", "AC/DC")
and expects the words mapped onto whatever the data actually holds. Indexing every distinct
value of every text column would defeat the point on a database of any real size, so only
columns that behave like a lookup, a category or a country are indexed at all: repeated
often enough across the table's rows that the same value is genuinely shared, not merely a
column that happens to have few rows under it. A name or an address is not a category no
matter how small the table is; a genre or a country is one even on a table with only a
handful of rows, because the same handful of values keeps coming back.
"""

from scripts.db.clients import dialect as current_dialect
from scripts.db.clients import schema as current_schema
from scripts.db.introspection import restart
from scripts.db.pii import PII_COLUMN

_MAX_DISTINCT = 500
_SAMPLE_LIMIT = _MAX_DISTINCT + 1
# A column qualifies when its distinct count is small outright, or small relative to how
# many rows repeat each value. Without the second test, a column that is nearly one distinct
# value per row, such as a title or an address, reads as "low cardinality" purely because the
# table itself is small, and ends up indexed as though it were a fixed set of categories.
_MIN_ABSOLUTE_CARDINALITY = 50
_MAX_UNIQUE_RATIO = 0.5


def _text_columns(columns: list[tuple[str, str]]) -> list[str]:
    """Text columns worth considering for value indexing, contact-detail columns excluded.

    A column that looks like an email, a phone number or a person's name is never a category
    no matter how few distinct values it happens to have: a small table can make an entire
    column of real people's contact details look "low cardinality" by table size alone, which
    is exactly the case this rules out before the cardinality check ever runs.
    """
    text_types = current_dialect().text_types
    return [
        column
        for column, dtype in columns
        if dtype.lower() in text_types and not PII_COLUMN.search(column)
    ]


def low_cardinality_values(
    conn, table_name: str, columns: list[tuple[str, str]], row_estimate: int
) -> dict[str, list[str]]:
    """Distinct, non-null values of each text column that behaves like a fixed set of categories.

    Each column is checked with a single query capped by LIMIT, so the cost of ruling a
    high-cardinality column out is bounded no matter how large the table is: the scan stops
    the moment it has collected one more distinct value than the cap allows, rather than
    counting every one of them first. A column whose scan fails or runs out of time is
    skipped, not the whole table: it only means that column's values are not searchable.
    """
    dialect = current_dialect()
    table = dialect.qualified(current_schema(), table_name)
    result: dict[str, list[str]] = {}
    for column in _text_columns(columns):
        col = dialect.quote(column)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT DISTINCT {col} FROM {table} WHERE {col} IS NOT NULL "
                    f"LIMIT {_SAMPLE_LIMIT}"
                )
                values = [row[0] for row in cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            print(f"Skipping values of {table_name}.{column}: {type(exc).__name__}: {exc}")
            restart(conn)
            continue
        count = len(values)
        if count == 0 or count > _MAX_DISTINCT:
            continue
        repeats_enough = count <= _MIN_ABSOLUTE_CARDINALITY or (
            row_estimate > 0 and count <= row_estimate * _MAX_UNIQUE_RATIO
        )
        if repeats_enough:
            result[column] = sorted(str(v) for v in values)
    return result
