"""Web interface for asking the Chinook database questions in plain language.

Run it with the command in README_UI.md. The command line version in main.py still works
exactly as before, and nothing it depends on is changed by this file.

Chat bubbles are written as raw HTML rather than through st.chat_message or a keyed
container. Streamlit wraps every markdown element in its own internal layout box, and one of
those boxes was measuring itself shorter than the text it holds. Because everything in that
chain has overflow left as visible, the short measurement never actually clipped the text,
it just meant a background colour painted on that box stopped short of covering it, which is
what read as a bubble cut off partway through. Painting the background on a div this file
writes directly, sitting inside that box rather than being that box, sidesteps the problem:
the div sizes itself to its own content regardless of what the ancestor around it measured.
"""

import html
import time
from pathlib import Path

import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="Chinook Database Chat",
    layout="wide",
    initial_sidebar_state="auto",
)

ROOT = Path(__file__).parent
EXAMPLE_QUESTIONS = [
    "How many customers are from the USA?",
    "What are the top 5 selling tracks?",
    "Show me all albums by AC/DC",
    "Which country brought in the most revenue?",
]


def load_styles():
    css = (ROOT / "assets" / "styles.css").read_text(encoding="utf-8")
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)


load_styles()


# ---------- bubble rendering ----------
#
# Model answers use a small set of predictable markdown: numbered lists, occasional bullets,
# **bold**, and plain paragraphs. Rather than pull in a markdown dependency for that, the
# handful of patterns actually seen in testing are converted directly. Anything outside
# those patterns is escaped and shown as plain text rather than guessed at.

def text_to_html(text):
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

    for line in lines:
        stripped = line.strip()
        numbered = stripped[:2].rstrip(".").isdigit() if stripped[:1].isdigit() else False
        bulleted = stripped.startswith(("- ", "* "))

        if numbered or bulleted:
            tag = "ol" if numbered else "ul"
            if list_tag and list_tag != tag:
                flush()
            list_tag = tag
            content = stripped.split(".", 1)[1].strip() if numbered else stripped[2:]
            list_items.append(f"<li>{content}</li>")
            continue

        flush()
        if stripped:
            blocks.append(f"<p>{stripped}</p>")

    flush()
    joined = "".join(blocks) or f"<p>{escaped}</p>"
    return joined.replace("**", "<strong>", 1).replace("**", "</strong>", 1) \
        if joined.count("**") >= 2 else joined


def bubble(role, text_html, key_suffix):
    css_class = "bubble-user" if role == "user" else "bubble-bot"
    return f'<div class="{css_class}" id="bubble-{key_suffix}">{text_html}</div>'


def stream_bubble(placeholder, text, size=3, pause=0.012):
    """Type the answer into its own bubble div, chunk by chunk, in place."""
    shown = ""
    for start in range(0, len(text), size):
        shown += text[start:start + size]
        placeholder.markdown(bubble("assistant", text_to_html(shown), "live"),
                              unsafe_allow_html=True)
        time.sleep(pause)

try:
    import chat_engine
    import config
    from scripts.indexer import get_index_status, run_full_reindex
except Exception as exc:  # noqa: BLE001
    st.markdown(
        '<div class="notice"><strong>Cannot reach the backing services.</strong><br>'
        f"{type(exc).__name__}: {exc}<br><br>"
        "Check that the Postgres and Qdrant containers are running, that Ollama is up, "
        "and that the values in <code>.env</code> are filled in.</div>",
        unsafe_allow_html=True,
    )
    st.stop()


# ---------- pipeline diagram ----------

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


@st.cache_data(ttl=120, show_spinner=False)
def cached_status():
    status = get_index_status()
    return {
        "db": status["db_tables"],
        "qdrant": status["qdrant_tables"],
        "stale": status["needs_reindex"],
    }


@st.cache_data(ttl=300, show_spinner=False)
def cached_schema():
    return chat_engine.schema_overview()


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


def render_chart(frame):
    """Draw a bar chart when the shape of the result makes one meaningful."""
    if frame is None or not 2 <= len(frame) <= 30:
        return
    numeric = [c for c in frame.columns if pd.api.types.is_numeric_dtype(frame[c])]
    labels = [c for c in frame.columns if c not in numeric]
    if not numeric or not labels:
        return
    st.markdown('<div class="panel-label">Shape of the result</div>', unsafe_allow_html=True)
    st.bar_chart(frame.set_index(labels[0])[numeric[0]], color="#B11813", height=260)


def render_detail(result):
    with st.expander("How this answer was produced"):
        st.markdown(
            pipeline_html(
                completed=[n["key"] for n in chat_engine.PIPELINE],
                timings=result.get("timings", {}),
            ),
            unsafe_allow_html=True,
        )

        if result.get("schema"):
            st.markdown('<div class="panel-label">Tables retrieved</div>', unsafe_allow_html=True)
            st.code(result["schema"], language="text")

        if result.get("sql"):
            st.markdown('<div class="panel-label">Query written</div>', unsafe_allow_html=True)
            st.code(result["sql"], language="sql")

        frame = as_frame(result)
        if frame is not None:
            st.markdown('<div class="panel-label">Rows returned</div>', unsafe_allow_html=True)
            st.dataframe(frame, width="stretch", hide_index=True)
            render_chart(frame)

        timings = result.get("timings", {})
        if timings:
            st.markdown(
                f'<div class="timing">Model {result.get("model")} &middot; '
                f'total {timings.get("total", 0):.1f}s</div>',
                unsafe_allow_html=True,
            )


def render_failure(result):
    if result.get("needs_restart"):
        body = (
            "<strong>The database session needs restarting.</strong><br>"
            "An earlier query failed and left the shared connection in an aborted "
            "transaction, so every question after it fails too. Stop the app and start it "
            "again to clear it."
        )
    else:
        body = (
            "<strong>That question could not be answered.</strong><br>"
            f"{result.get('error')}"
        )
    st.markdown(f'<div class="notice">{body}</div>', unsafe_allow_html=True)


# ---------- state ----------

for name, default in [("messages", []), ("pending", None), ("comparison", None)]:
    if name not in st.session_state:
        st.session_state[name] = default


# ---------- sidebar ----------

with st.sidebar:
    st.markdown('<div class="side-brand">Chinook <span>Chat</span></div>', unsafe_allow_html=True)

    st.markdown('<div class="side-heading">Model</div>', unsafe_allow_html=True)
    models = chat_engine.available_models()
    model = st.selectbox(
        "Model",
        models,
        index=models.index(config.DEFAULT_MODEL) if config.DEFAULT_MODEL in models else 0,
        label_visibility="collapsed",
    )

    st.markdown('<div class="side-heading">Index</div>', unsafe_allow_html=True)
    try:
        status = cached_status()
        pill = (
            '<span class="pill pill-warn">stale</span>'
            if status["stale"]
            else '<span class="pill pill-ok">ready</span>'
        )
        st.markdown(
            f'<div class="stat"><span class="stat-label">Database tables</span>'
            f'<span class="stat-value">{status["db"]}</span></div>'
            f'<div class="stat"><span class="stat-label">Indexed tables</span>'
            f'<span class="stat-value">{status["qdrant"]}</span></div>'
            f'<div class="stat"><span class="stat-label">State</span>{pill}</div>',
            unsafe_allow_html=True,
        )
    except Exception as exc:  # noqa: BLE001
        st.markdown(
            '<div class="stat"><span class="stat-label">Status</span>'
            '<span class="stat-value">unavailable</span></div>'
            f'<div class="timing">{type(exc).__name__}</div>',
            unsafe_allow_html=True,
        )

    if st.button("Rebuild index", type="primary"):
        with st.spinner("Rebuilding"):
            try:
                count = run_full_reindex()
                cached_status.clear()
                st.success(f"Indexed {count} tables")
            except Exception as exc:  # noqa: BLE001
                st.error(f"{type(exc).__name__}: {exc}")

    if st.session_state.messages:
        st.markdown('<div class="side-heading">Conversation</div>', unsafe_allow_html=True)
        asked = sum(1 for m in st.session_state.messages if m["role"] == "user")
        spent = sum(
            m["result"].get("timings", {}).get("total", 0)
            for m in st.session_state.messages
            if m.get("result")
        )
        st.markdown(
            f'<div class="stat"><span class="stat-label">Questions</span>'
            f'<span class="stat-value">{asked}</span></div>'
            f'<div class="stat"><span class="stat-label">Time in answers</span>'
            f'<span class="stat-value">{spent:.0f}s</span></div>',
            unsafe_allow_html=True,
        )
        if st.button("Clear messages"):
            st.session_state.messages = []
            st.rerun()


# ---------- header ----------

banner = ROOT / "banner.png"
if banner.exists():
    st.image(str(banner), width="stretch")

chat_tab, compare_tab, schema_tab = st.tabs(["Chat", "Compare models", "Schema"])


# ---------- chat ----------

with chat_tab:
    st.markdown('<div class="page-title">Ask the database a question</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="page-subtitle">Questions are turned into SQL, run against the Chinook '
        "database, and answered in plain language. Every answer carries the tables that were "
        "searched and the query that ran.</div>",
        unsafe_allow_html=True,
    )
    st.markdown('<div class="rule"></div>', unsafe_allow_html=True)

    if not st.session_state.messages and not st.session_state.pending:
        st.markdown(pipeline_html(), unsafe_allow_html=True)
        st.markdown('<div class="panel-label">Try one of these</div>', unsafe_allow_html=True)
        left, right = st.columns(2)
        for position, example in enumerate(EXAMPLE_QUESTIONS):
            target = left if position % 2 == 0 else right
            if target.button(example, key=f"example_{position}"):
                st.session_state.pending = example
                st.rerun()

    for index, message in enumerate(st.session_state.messages):
        if message["role"] == "user":
            st.markdown(
                bubble("user", text_to_html(message["content"]), index),
                unsafe_allow_html=True,
            )
        else:
            if message.get("failed"):
                render_failure(message["result"])
            else:
                st.markdown(
                    bubble("assistant", text_to_html(message["content"]), index),
                    unsafe_allow_html=True,
                )
                if message.get("result"):
                    render_detail(message["result"])

    question = st.chat_input("Ask about customers, tracks, albums, invoices and more")

    if st.session_state.pending:
        question = st.session_state.pending
        st.session_state.pending = None

    if question:
        index = len(st.session_state.messages)
        st.session_state.messages.append({"role": "user", "content": question})
        st.markdown(bubble("user", text_to_html(question), index), unsafe_allow_html=True)

        diagram = st.empty()
        slot = st.empty()
        answer_slot = st.empty()
        finished = ["question"]

        def advance(label):
            key = chat_engine.STAGE_TO_KEY.get(label)
            diagram.markdown(
                pipeline_html(active=key, completed=list(finished)),
                unsafe_allow_html=True,
            )
            slot.markdown(thinking(label), unsafe_allow_html=True)
            if key:
                finished.append(key)

        diagram.markdown(pipeline_html(completed=finished), unsafe_allow_html=True)
        result = chat_engine.answer(question, model, on_stage=advance)
        diagram.empty()
        slot.empty()

        if result["ok"] and result.get("answer"):
            stream_bubble(answer_slot, result["answer"])
            st.session_state.messages.append({
                "role": "assistant",
                "content": result["answer"],
                "result": result,
            })
        else:
            answer_slot.empty()
            render_failure(result)
            st.session_state.messages.append({
                "role": "assistant",
                "content": "",
                "failed": True,
                "result": result,
            })

        # Redraw everything from stored state so the streamed message and the stored one do
        # not both survive, which would show the detail panel twice.
        st.rerun()


# ---------- model comparison ----------

with compare_tab:
    st.markdown('<div class="page-title">Put both models on the same question</div>',
                unsafe_allow_html=True)
    st.markdown(
        '<div class="page-subtitle">The same question goes to both deployments, one after '
        "the other, and the answers sit side by side with the SQL each one wrote and how "
        "long it took. They run in sequence because the database connection is shared.</div>",
        unsafe_allow_html=True,
    )
    st.markdown('<div class="rule"></div>', unsafe_allow_html=True)

    asked = st.text_input(
        "Question",
        value="What are the top 5 selling tracks?",
        label_visibility="collapsed",
    )

    if st.button("Run on both models", type="primary", key="run_compare"):
        progress = st.empty()
        outcomes = chat_engine.compare(
            asked,
            on_progress=lambda name: progress.markdown(
                thinking(f"Asking {name}"), unsafe_allow_html=True
            ),
        )
        progress.empty()
        st.session_state.comparison = {"question": asked, "results": outcomes}

    comparison = st.session_state.comparison
    if comparison:
        st.markdown(f'<div class="asked">{comparison["question"]}</div>', unsafe_allow_html=True)
        columns = st.columns(len(comparison["results"]))

        fastest = min(
            (r["timings"].get("total", 9e9) for r in comparison["results"].values()),
            default=0,
        )

        for column, (name, result) in zip(columns, comparison["results"].items()):
            with column:
                total = result.get("timings", {}).get("total", 0)
                badge = '<span class="pill pill-ok">fastest</span>' if total == fastest else ""
                st.markdown(
                    f'<div class="compare-head"><span class="compare-name">{name}</span>'
                    f"{badge}</div>",
                    unsafe_allow_html=True,
                )
                st.markdown(
                    f'<div class="stat"><span class="stat-label">Total</span>'
                    f'<span class="stat-value">{total:.1f}s</span></div>',
                    unsafe_allow_html=True,
                )

                if result["ok"] and result.get("answer"):
                    st.markdown(f'<div class="compare-answer">{result["answer"]}</div>',
                                unsafe_allow_html=True)
                    if result.get("sql"):
                        st.markdown('<div class="panel-label">SQL</div>', unsafe_allow_html=True)
                        st.code(result["sql"], language="sql")
                    frame = as_frame(result)
                    if frame is not None:
                        st.dataframe(frame, width="stretch", hide_index=True)
                else:
                    render_failure(result)


# ---------- schema ----------

with schema_tab:
    st.markdown('<div class="page-title">What is in the database</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="page-subtitle">Read from the database itself rather than written down '
        "anywhere. This is the same introspection the indexer uses, so it is exactly what the "
        "model can be shown.</div>",
        unsafe_allow_html=True,
    )
    st.markdown('<div class="rule"></div>', unsafe_allow_html=True)

    try:
        tables = cached_schema()
        totals = st.columns(3)
        figures = [
            ("Tables", f"{len(tables)}"),
            ("Columns", f"{sum(len(t['columns']) for t in tables)}"),
            ("Rows", f"{sum(t['rows'] for t in tables):,}"),
        ]
        for column, (label, value) in zip(totals, figures):
            column.markdown(
                f'<div class="figure"><div class="figure-value">{value}</div>'
                f'<div class="figure-label">{label}</div></div>',
                unsafe_allow_html=True,
            )

        st.markdown('<div class="panel-label">Tables</div>', unsafe_allow_html=True)
        for table in tables:
            with st.expander(f"{table['table']}  ({table['rows']:,} rows)"):
                st.dataframe(
                    pd.DataFrame(table["columns"], columns=["Column", "Type"]),
                    width="stretch",
                    hide_index=True,
                )
    except Exception as exc:  # noqa: BLE001
        st.markdown(
            f'<div class="notice"><strong>Could not read the schema.</strong><br>'
            f"{type(exc).__name__}: {exc}</div>",
            unsafe_allow_html=True,
        )
