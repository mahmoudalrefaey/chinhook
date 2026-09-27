"""Rendering for the interface: markup produced from a result, with no Streamlit in it.

The functions here take plain values and return HTML or a dataframe. They are kept apart
from the page so that what a reply looks like is one readable piece, and so that the page
itself is only about wiring: what to show, and in what order.
"""

import html
import re

import pandas as pd

import chat_engine


# ---------- answers ----------

def split_table_row(line):
    """The cells of one markdown table row, or None when the line is not a table row."""
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return None
    return [cell.strip() for cell in stripped.strip("|").split("|")]


def is_table_separator(line):
    cells = split_table_row(line)
    if not cells or not all(cells):
        return False
    return all(re.fullmatch(r":?-{2,}:?", cell or "") for cell in cells)


def inline(text):
    """**bold** and `code` inside a line of text."""
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    return re.sub(r"`([^`]+?)`", r"<code>\1</code>", text)


def render_table(header, body):
    """One markdown table as an HTML table, padded so every row has the same columns."""
    columns = max([len(header)] + [len(row) for row in body] or [len(header)])
    out = ['<table class="answer-table"><thead><tr>']
    for position in range(columns):
        cell = header[position] if position < len(header) else ""
        out.append(f"<th>{inline(cell)}</th>")
    out.append("</tr></thead><tbody>")
    for row in body:
        out.append("<tr>")
        for position in range(columns):
            cell = row[position] if position < len(row) else ""
            out.append(f"<td>{inline(cell)}</td>")
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def text_to_html(text):
    """Markdown used in an answer, rendered as HTML.

    Handles the forms answers actually take: headings, tables, numbered and bulleted lists,
    **bold** and plain paragraphs. Blank lines are spacing rather than content, so runs of
    them are dropped instead of becoming empty paragraphs. Anything not recognised is
    escaped and shown as text rather than guessed at.
    """
    escaped = html.escape(text)
    lines = escaped.split("\n")
    blocks = []
    list_items = []
    list_tag = None

    def flush():
        nonlocal list_items, list_tag
        if list_items:
            blocks.append(f"<{list_tag}>" + "".join(list_items) + f"</{list_tag}>")
            list_items = []
            list_tag = None

    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        # A table is a header row, a separator row and then the body rows.
        if split_table_row(line) and index + 1 < len(lines) and is_table_separator(lines[index + 1]):
            flush()
            header = split_table_row(line)
            index += 2
            body = []
            while index < len(lines) and split_table_row(lines[index]):
                body.append(split_table_row(lines[index]))
                index += 1
            blocks.append(render_table(header, body))
            continue

        # This runs on a growing partial string while an answer is still typing itself out,
        # not just on finished text. partition never raises even when the separator it is
        # looking for has not been typed yet, which split(..., 1)[1] does the moment a line
        # is mid-stream nothing but a bare digit like "1" with no period after it yet.
        marker, sep, rest = stripped.partition(".")
        numbered = bool(sep) and marker.isdigit()
        bulleted = stripped.startswith(("- ", "* "))

        if numbered or bulleted:
            tag = "ol" if numbered else "ul"
            if list_tag and list_tag != tag:
                flush()
            list_tag = tag
            content = rest.strip() if numbered else stripped[2:]
            list_items.append(f"<li>{inline(content)}</li>")
            index += 1
            continue

        flush()
        if not stripped:
            index += 1
            continue

        heading = re.fullmatch(r"(#{1,6})\s+(.*)", stripped)
        if heading:
            level = min(6, len(heading.group(1)))
            blocks.append(f"<h{level}>{inline(heading.group(2))}</h{level}>")
        elif stripped.isupper() and len(stripped) > 3 and not stripped.startswith("|"):
            blocks.append(f"<p><strong>{inline(stripped)}</strong></p>")
        else:
            blocks.append(f"<p>{inline(stripped)}</p>")
        index += 1

    flush()
    return "".join(blocks) or f"<p>{escaped}</p>"


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
