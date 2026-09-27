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

import re

import markdown as markdown_lib
import pandas as pd

import chat_engine

# The extensions that cover what answers actually use. "extra" is a bundle containing
# fenced code, tables, sane lists, footnotes and a few others.
_MARKDOWN = markdown_lib.Markdown(
    extensions=["extra", "sane_lists", "admonition"],
    output_format="html",
)

# Raw HTML in a model's output would be passed straight through by the converter, so the
# handful of tags that can execute or fetch are removed afterwards. This is a guard, not a
# general purpose sanitiser, and it exists because the reply is rendered as live HTML.
_DANGEROUS_TAGS = re.compile(
    r"<\s*/?\s*(script|style|iframe|object|embed|form|link|meta|svg)\b[^>]*>.*?"
    r"(?:<\s*/\s*\1\s*>)?",
    re.IGNORECASE | re.DOTALL,
)
_EVENT_HANDLERS = re.compile(r"\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)


# ---------- answers ----------

def markdown_to_html(text: str) -> str:
    """Markdown to HTML, with anything executable in the source removed first.

    Escaping the raw tags before conversion rather than after, so a tag that survived would
    be shown as text instead of being interpreted as markup.
    """
    source = _DANGEROUS_TAGS.sub("", text or "")
    source = _EVENT_HANDLERS.sub("", source)
    _MARKDOWN.reset()
    return _MARKDOWN.convert(source)


def text_to_html(text):
    """A reply, rendered. Kept under the name the page imports it by."""
    return markdown_to_html(text)


def as_frame(result):
    """Result rows as a dataframe, with anything numeric actually typed as numeric."""
    if not result.get("rows") or not result.get("columns"):
        return None
    frame = pd.DataFrame(result["rows"], columns=result["columns"])
    for column in frame.columns:
        converted = pd.to_numeric(frame[column], errors="coerce")
        if converted.notna().all():
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
