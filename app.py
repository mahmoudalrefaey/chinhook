"""Web interface for asking the Chinook database questions in plain language.

Run it with the command in README_UI.md. The command line version in main.py still works
exactly as before, and nothing it depends on is changed by this file.
"""

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


# Importing the pipeline opens the database connection, so a failure here means the
# environment is not ready rather than the code being wrong. Say so plainly and stop.
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


def thinking(label):
    return (
        '<div class="thinking">'
        '<span class="dots"><span></span><span></span><span></span></span>'
        f'<span class="thinking-label">{label}</span>'
        "</div>"
    )


def typewriter(text, size=3, pause=0.012):
    """Yield the finished answer in small pieces so it writes itself onto the page."""
    for start in range(0, len(text), size):
        yield text[start:start + size]
        time.sleep(pause)


@st.cache_data(ttl=120, show_spinner=False)
def cached_status():
    status = get_index_status()
    return {
        "db": status["db_tables"],
        "qdrant": status["qdrant_tables"],
        "stale": status["needs_reindex"],
    }


def render_detail(result):
    with st.expander("How this answer was produced"):
        if result.get("schema"):
            st.markdown('<div class="panel-label">Tables retrieved</div>', unsafe_allow_html=True)
            st.code(result["schema"], language="text")

        if result.get("sql"):
            st.markdown('<div class="panel-label">Query written</div>', unsafe_allow_html=True)
            st.code(result["sql"], language="sql")

        if result.get("rows"):
            st.markdown('<div class="panel-label">Rows returned</div>', unsafe_allow_html=True)
            st.dataframe(
                pd.DataFrame(result["rows"], columns=result["columns"]),
                use_container_width=True,
                hide_index=True,
            )

        timings = result.get("timings", {})
        if timings:
            parts = [f"{name} {value:.1f}s" for name, value in timings.items()]
            st.markdown(
                f'<div class="timing">Model {result.get("model")} &middot; '
                f'{" &middot; ".join(parts)}</div>',
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

if "messages" not in st.session_state:
    st.session_state.messages = []
if "pending" not in st.session_state:
    st.session_state.pending = None


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
            f'<div class="stat"><span class="stat-label">Status</span>'
            f'<span class="stat-value">unavailable</span></div>'
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
        if st.button("Clear messages"):
            st.session_state.messages = []
            st.rerun()


# ---------- header ----------

banner = ROOT / "banner.png"
if banner.exists():
    st.image(str(banner), use_container_width=True)

st.markdown('<div class="page-title">Ask the database a question</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="page-subtitle">Questions are turned into SQL, run against the Chinook '
    "database, and answered in plain language. Open the panel under any answer to see the "
    "tables that were used and the query that ran.</div>",
    unsafe_allow_html=True,
)
st.markdown('<div class="rule"></div>', unsafe_allow_html=True)


# ---------- empty state ----------

if not st.session_state.messages and not st.session_state.pending:
    st.markdown('<div class="panel-label">Try one of these</div>', unsafe_allow_html=True)
    left, right = st.columns(2)
    for position, question in enumerate(EXAMPLE_QUESTIONS):
        target = left if position % 2 == 0 else right
        if target.button(question, key=f"example_{position}"):
            st.session_state.pending = question
            st.rerun()


# ---------- history ----------

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        if message.get("failed"):
            render_failure(message["result"])
        else:
            st.markdown(message["content"])
            if message.get("result"):
                render_detail(message["result"])


# ---------- new question ----------

question = st.chat_input("Ask about customers, tracks, albums, invoices and more")

if st.session_state.pending:
    question = st.session_state.pending
    st.session_state.pending = None

if question:
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        slot = st.empty()
        slot.markdown(thinking("Getting started"), unsafe_allow_html=True)

        result = chat_engine.answer(
            question,
            model,
            on_stage=lambda label: slot.markdown(thinking(label), unsafe_allow_html=True),
        )
        slot.empty()

        if result["ok"] and result.get("answer"):
            st.write_stream(typewriter(result["answer"]))
            st.session_state.messages.append({
                "role": "assistant",
                "content": result["answer"],
                "result": result,
            })
        else:
            render_failure(result)
            st.session_state.messages.append({
                "role": "assistant",
                "content": "",
                "failed": True,
                "result": result,
            })

    # The answer was just drawn directly onto the page while it streamed. Rerunning draws
    # the whole conversation from stored state instead, so every message is produced by the
    # same code path. Without this the streamed message and the stored one both survive and
    # the detail panel appears twice.
    st.rerun()
