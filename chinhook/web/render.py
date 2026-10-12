"""What a reply looks like on its way to the browser: sanitised HTML and plain JSON values.

Answers arrive as Markdown and are converted by Python-Markdown rather than by a converter
written here. That is the point of using the library: tables, ordered and unordered lists,
emphasis, headings, block quotes, inline code and fenced blocks all come out the way they
were written, and a construct nobody thought to special-case still renders instead of
showing up as raw punctuation.

The converted HTML is the only markup the browser ever inserts as markup. Every other value
here (table names, SQL, rows, error messages) is sent as plain JSON and placed on the page as
text by chinhook/web/static/app.js, so none of it can turn into markup however it is written.
"""

import math
from datetime import date, datetime, time
from decimal import Decimal

import markdown as markdown_lib
import nh3

from chinhook import chat_engine

# The extensions that cover what answers actually use. "extra" is a bundle containing
# fenced code, tables, sane lists, footnotes and a few others.
_MARKDOWN_EXTENSIONS = ["extra", "sane_lists", "admonition"]

# What is actually allowed to reach the page as live markup, checked by parsing the HTML
# properly rather than by pattern-matching it as text. A previous version of this file tried
# to strip a short list of dangerous tags with two regular expressions, and a tag written
# inside another tag reassembled into a live one the moment the outer tag was removed, among
# more than a dozen other ways found to get past it: a javascript: link written as raw HTML
# or as Markdown's own link syntax, an attribute written with an unusual separator, a <base>
# tag able to redirect every relative link on the page, and a handful more. None of that is
# possible against a real parser working from an allowlist, because there is no tag or
# attribute here for anything unwanted to hide inside.
_ALLOWED_TAGS = {
    "p", "br", "hr", "strong", "em", "code", "pre", "blockquote",
    "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "thead", "tbody", "tr", "th", "td", "a",
}
_ALLOWED_ATTRIBUTES = {"a": {"href", "title"}}
_ALLOWED_URL_SCHEMES = {"http", "https", "mailto"}

# How many result rows go to the browser with an answer. The answer itself was written from
# the rows the query returned; this only bounds what the results table shows and downloads.
MAX_ROWS_SENT = 1000


def _sanitize(html: str) -> str:
    return nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes=_ALLOWED_URL_SCHEMES,
        link_rel="noopener noreferrer nofollow",
    )


def markdown_to_html(text: str) -> str:
    """Markdown to HTML, sanitised against an allowlist afterwards.

    A fresh Markdown converter is built for every call rather than reused from one shared at
    module level. markdown.Markdown is stateful and was never meant to be used from more than
    one place at a time, and the server answers every session on its own thread inside one
    process: a shared converter meant one visitor's answer, mid-conversion, could receive
    another visitor's text and hand back their rendered reply instead of its own. That was
    reproduced directly, not merely suspected, and it is why this is a few milliseconds slower
    per call rather than something reused.
    """
    converted = markdown_lib.Markdown(
        extensions=_MARKDOWN_EXTENSIONS, output_format="html"
    ).convert(text or "")
    return _sanitize(converted)


# ---------- result rows ----------

def _dedupe_columns(columns: list[str]) -> list[str]:
    """Column labels made unique, keeping the first occurrence of each name as it was.

    SELECT a.name, b.name FROM ... is entirely ordinary SQL and produces two columns that
    share a name; the results table and the CSV download both need to tell them apart.
    """
    seen: dict[str, int] = {}
    unique = []
    for name in columns:
        name = str(name)
        count = seen.get(name, 0)
        seen[name] = count + 1
        unique.append(name if count == 0 else f"{name} ({count + 1})")
    return unique


def _cell(value):
    """One value from a row, as JSON can carry it, shown the way the database returned it.

    A Decimal stays text so 138.60 is not shown as 138.6, and a date is written the ISO way.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    return str(value)


def _number(value):
    """value as a number when it genuinely is one, else None.

    A text value counts only when it reads back exactly as written, so a postal code such as
    "01234" stays text rather than being charted as 1234.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        text = value.strip()
        try:
            number = float(text)
        except ValueError:
            return None
        if not math.isfinite(number):
            return None
        digits = text.lstrip("-")
        if len(digits) > 1 and digits[0] == "0" and digits[1] != ".":
            return None
        return number
    return None


def _chart(columns: list[str], rows: list) -> dict | None:
    """A bar chart of the result, when its shape makes one meaningful.

    That is: between 2 and 30 rows, with a text column to label the bars and a numeric
    column to size them. The first of each is used.
    """
    if not 2 <= len(rows) <= 30:
        return None
    numeric = [
        position for position in range(len(columns))
        if all(_number(row[position]) is not None for row in rows)
    ]
    labels = [position for position in range(len(columns)) if position not in numeric]
    if not numeric or not labels:
        return None
    label, value = labels[0], numeric[0]
    return {
        "label": columns[label],
        "value": columns[value],
        # Each point is [label, value to size the bar by, value as the database wrote it].
        "points": [[str(_cell(row[label])), _number(row[value]), str(_cell(row[value]))] for row in rows],
    }


def _rows(result) -> dict | None:
    columns = result.get("columns")
    rows = result.get("rows")
    if not columns or not rows:
        return None
    columns = _dedupe_columns(list(columns))
    rows = [list(row) for row in rows]
    return {
        "columns": columns,
        "rows": [[_cell(value) for value in row] for row in rows[:MAX_ROWS_SENT]],
        "total": len(rows),
        "chart": _chart(columns, rows),
    }


# ---------- messages ----------

_TASK_FIELDS = (
    "task_id", "intent", "question", "status", "verification", "row_count", "truncated",
    "schema_tables", "schema_from_cache", "semantic", "sql", "failure_reason",
)


def _details(result) -> dict | None:
    """How an answer was produced: the query, the rows, the steps. None for a reply that ran
    no query at all, such as a greeting or a clarifying question."""
    tasks = result.get("tasks") or []
    if not (result.get("sql") or result.get("schema") or tasks):
        return None
    tables = sorted({
        name for task in tasks for name in (task.get("schema_tables") or [])
    })
    return {
        "sql": result.get("sql") or "",
        "tables": tables,
        "schema": result.get("schema") or "",
        "result": _rows(result),
        "tasks": [
            {name: _cell(task.get(name)) if name != "schema_tables" else list(task.get(name) or [])
             for name in _TASK_FIELDS}
            for task in tasks
        ],
        "usage": result.get("usage") or {},
    }


def message_view(index: int, message: dict, answered: set[int]) -> dict:
    """One message of the conversation, in the shape the page draws it from."""
    if message["role"] == "user":
        return {"index": index, "role": "user", "text": message["content"]}

    result = message.get("result") or {}
    view = {
        "index": index,
        "role": "assistant",
        "model": result.get("model") or "",
        "timings": {
            key: round(value, 2) for key, value in (result.get("timings") or {}).items()
        },
    }
    if message.get("failed"):
        view["failed"] = True
        view["error"] = str(result.get("error") or "")
        return view

    view["html"] = markdown_to_html(message["content"])
    view["text"] = message["content"]
    view["details"] = _details(result)
    clarification = result.get("clarification") or {}
    options = [str(option) for option in clarification.get("options") or []]
    if options and index not in answered:
        # "both" among the options means the question is genuinely multi-select.
        multi = len(options) > 2 or any(
            option.strip().lower() in ("both", "all", "either") for option in options
        )
        view["clarification"] = {"options": options, "multi": multi}
    return view


def pipeline() -> list[dict]:
    """The stages an answer goes through, for the progress strip."""
    return [dict(node) for node in chat_engine.PIPELINE]
