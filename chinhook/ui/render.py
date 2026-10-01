"""Rendering for the interface: markup produced from a result, with no Streamlit in it.

The functions here take plain values and return HTML or a dataframe. They are kept apart
from the page so that what a reply looks like is one readable piece, and so that the page
itself is only about wiring: what to show, and in what order.

Answers arrive as Markdown and are converted by Python-Markdown rather than by a converter
written here. That is the point of using the library: tables, ordered and unordered lists,
emphasis, headings, block quotes, inline code and fenced blocks all come out the way they
were written, and a construct nobody thought to special-case still renders instead of
showing up as raw punctuation.

The output is HTML placed inside the chat bubble, which is why the page writes it itself
rather than handing the Markdown to Streamlit: see the note in app.py about why the bubble
has to be a div the page writes.
"""

import markdown as markdown_lib
import nh3
import pandas as pd

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


def _sanitize(html: str) -> str:
    return nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes=_ALLOWED_URL_SCHEMES,
        link_rel="noopener noreferrer nofollow",
    )


# ---------- answers ----------

def markdown_to_html(text: str) -> str:
    """Markdown to HTML, sanitised against an allowlist afterwards.

    A fresh Markdown converter is built for every call rather than reused from one shared at
    module level. markdown.Markdown is stateful and was never meant to be used from more than
    one place at a time, and Streamlit runs every visitor's session in its own thread inside
    one process: a shared converter meant one visitor's answer, mid-conversion, could receive
    another visitor's text and hand back their rendered reply instead of its own. That was
    reproduced directly, not merely suspected, and it is why this is a few milliseconds slower
    per call rather than something reused.
    """
    converted = markdown_lib.Markdown(
        extensions=_MARKDOWN_EXTENSIONS, output_format="html"
    ).convert(text or "")
    return _sanitize(converted)


def text_to_html(text):
    """A reply, rendered. Kept under the name the page imports it by."""
    return markdown_to_html(text)


def escape_text(text) -> str:
    """Plain text made safe to place inside HTML the page already controls the shape of.

    Used for values that are not Markdown and were never meant to carry any markup at all,
    such as a task id or a status word: nh3.clean with no tags allowed at all strips markup
    rather than encoding it, which is exactly what escaping plain text needs.
    """
    return nh3.clean(str(text if text is not None else ""), tags=set())


def _dedupe_columns(columns: list[str]) -> list[str]:
    """Column labels made unique, keeping the first occurrence of each name as it was.

    SELECT a.name, b.name FROM ... is entirely ordinary SQL and produces two columns that
    share a name. pandas allows that in the rows it is given, but indexing a DataFrame by a
    column name that occurs more than once hands back another DataFrame instead of the single
    Series every caller here expects, which is what made the whole page fail, and keep
    failing on every rerun once the broken result was already in the chat history.
    """
    seen: dict[str, int] = {}
    unique = []
    for name in columns:
        count = seen.get(name, 0)
        seen[name] = count + 1
        unique.append(name if count == 0 else f"{name} ({count + 1})")
    return unique


def as_frame(result):
    """Result rows as a dataframe, with anything genuinely numeric typed as numeric.

    A column is only retyped when converting it to a number and back to text reproduces
    exactly what was there. A postal code of "01234" becomes the number 1234, which does not
    turn back into "01234", so it is left as text rather than silently losing its leading
    zero; an ordinary count or total round-trips cleanly and is typed as a number as before.
    """
    if not result.get("rows") or not result.get("columns"):
        return None
    columns = _dedupe_columns(list(result["columns"]))
    frame = pd.DataFrame(result["rows"], columns=columns)
    for column in frame.columns:
        series = frame[column]
        converted = pd.to_numeric(series, errors="coerce")
        if not converted.notna().all():
            continue
        roundtrip = converted.astype(str)
        original = series.astype(str).str.strip()
        if (roundtrip == original).all():
            frame[column] = converted
    return frame


# ---------- page furniture ----------

def bubble(role, text_html, key_suffix):
    css_class = "bubble-user" if role == "user" else "bubble-bot"
    return f'<div class="{css_class}" id="bubble-{key_suffix}">{text_html}</div>'


def pipeline_html(active=None, completed=(), timings=None):
    """The row of stages, with each one marked pending, running or finished."""
    timings = timings or {}
    pieces = []
    for position, node in enumerate(chat_engine.PIPELINE):
        key = node["key"]
        if key == active:
            state = "active"
        elif key in completed:
            state = "done"
        else:
            state = "idle"

        seconds = timings.get(key)
        stamp = f'<div class="node-time">{seconds:.1f}s</div>' if seconds else ""
        if key == "question" and "question" in completed and not seconds:
            stamp = '<div class="node-time">in</div>'

        if position:
            pieces.append(f'<div class="wire {"done" if state != "idle" else ""}"></div>')

        pieces.append(
            f'<div class="node {state}">'
            f'<div class="node-dot">{position + 1}</div>'
            f'<div class="node-title">{node["title"]}</div>'
            f'<div class="node-detail">{node["detail"]}</div>'
            f"{stamp}</div>"
        )
    return f'<div class="pipeline">{"".join(pieces)}</div>'


def thinking(label):
    return (
        '<div class="thinking">'
        '<span class="dots"><span></span><span></span><span></span></span>'
        f'<span class="thinking-label">{label}</span>'
        "</div>"
    )
