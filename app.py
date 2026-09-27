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

from ui.render import (
    as_frame,
    bubble,
    pipeline_html,
    text_to_html,
    thinking,
)

ROOT = Path(__file__).parent
ICON = ROOT / "assets" / "icon.svg"

st.set_page_config(
    page_title="Chinook Database Chat",
    page_icon=str(ICON) if ICON.exists() else None,
    layout="wide",
    initial_sidebar_state="auto",
)

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
            tables = sorted({
                name
                for task in result.get("tasks") or []
                for name in (task.get("schema_tables") or [])
            })
            st.markdown(
                '<div class="panel-label">Tables retrieved: '
                + html.escape(", ".join(tables) or "none")
                + "</div>",
                unsafe_allow_html=True,
            )
            # The schema of every table that was searched is the longest thing in here and
            # the least often wanted, so it stays available but out of the way until asked
            # for. The table names are still shown outside, which is the part anyone reads.
            with st.expander("Show the schema that was searched"):
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

        # Token counts, and the per-task breakdown when a question turned out to be more
        # than one question. Both sit inside the panel that was already there.
        usage = result.get("usage")
        if usage:
            st.markdown(
                f'<div class="timing">{usage.get("input_tokens", 0)} input tokens &middot; '
                f'{usage.get("output_tokens", 0)} output tokens &middot; '
                f'{usage.get("total_tokens", 0)} total &middot; '
                f'{usage.get("llm_calls", 0)} model call(s)</div>',
                unsafe_allow_html=True,
            )

        render_trace(result)


def render_clarification_controls(index, result):
    """The options of an open question, as controls the user can pick from.

    A question with a finite set of answers is faster to answer with a click than by typing,
    and a question that says "or" between things that are not mutually exclusive has to be
    able to take more than one, so the two cases get different controls.

    The choice is put into the same place a typed answer goes, so the request behind the
    question is resumed rather than the choice being read as a new question of its own.
    """
    clarification = result.get("clarification")
    if not clarification or not clarification.get("options"):
        return
    # Answered already: the question is no longer open, so there is nothing to choose.
    if st.session_state.answered.get(index):
        return

    options = list(clarification["options"])
    prefix = f"clarify_{index}"
    # "both" among the options means the question is genuinely multi-select.
    multi = len(options) > 2 or any(
        option.strip().lower() in ("both", "all", "either") for option in options
    )

    with st.form(prefix + "_form"):
        chosen = []
        if multi:
            st.markdown(
                '<div class="panel-label">Pick one or more</div>', unsafe_allow_html=True
            )
            for position, option in enumerate(options):
                if st.checkbox(option, key=f"{prefix}_{position}"):
                    chosen.append(option)
        else:
            st.markdown(
                '<div class="panel-label">Pick one</div>', unsafe_allow_html=True
            )
            chosen = [
                st.radio(
                    "Pick one",
                    options,
                    key=prefix + "_radio",
                    label_visibility="collapsed",
                )
            ]
        submit = st.form_submit_button("Send", key=f"{prefix}_submit")

    if submit:
        selection = ", ".join(option for option in chosen if option)
        st.session_state.answered[index] = True
        if not selection:
            return
        st.session_state.pending = selection
        st.rerun()


def render_trace(result):
    """The tasks this question became, and how each one went.

    Structured decisions only: what was asked, which tables were used, the SQL, the row
    count, whether verification passed and why something failed. No model reasoning, and
    nothing the answer itself does not already say.
    """
    tasks = result.get("tasks") or []
    if not tasks:
        return
    with st.expander("Tasks in this question"):
        for task in tasks:
            verdict = task.get("verification")
            badge = {"pass": "ok", "fail": "warn", "unknown": ""}.get(verdict, "")
            pill = f'<span class="pill pill-{badge}">{verdict}</span>' if badge else ""
            tables = ", ".join(task.get("schema_tables") or []) or "no tables"
            reuse = ", reused from earlier in this chat" if task.get("schema_from_cache") else ""
            st.markdown(
                f'<div class="stat"><span class="stat-label">{task.get("task_id")} &middot; '
                f'{task.get("intent")}</span>'
                f'<span class="stat-value">{task.get("row_count", 0)} rows</span></div>'
                f'<div class="timing">{html.escape(task.get("question", ""))} &middot; '
                f'{task.get("status")} {pill}</div>'
                f'<div class="timing">Tables: {html.escape(tables)}{reuse}</div>',
                unsafe_allow_html=True,
            )
            if task.get("semantic"):
                st.markdown(
                    f'<div class="timing">Grounding: {html.escape(task["semantic"])}</div>',
                    unsafe_allow_html=True,
                )
            if task.get("sql"):
                st.code(task["sql"], language="sql")
            if task.get("failure_reason"):
                st.markdown(
                    f'<div class="timing">Failed: {html.escape(task["failure_reason"])}</div>',
                    unsafe_allow_html=True,
                )


def render_failure(result):
    """Say what went wrong in words a person can act on.

    The technical detail is not thrown away, it is put away: an unexpected error message is
    the kind of thing that is useful to whoever is looking after this and meaningless to
    whoever asked the question, so it is folded away rather than shown or lost.
    """
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
            "Something went wrong before there was a result to show. Asking again usually "
            "works; if it keeps happening, the details are below."
        )
    st.markdown(f'<div class="notice">{body}</div>', unsafe_allow_html=True)

    detail = result.get("error")
    if detail:
        with st.expander("Technical detail"):
            st.code(str(detail), language="text")


# ---------- state ----------

for name, default in [("messages", []), ("pending", None), ("comparison", None),
                      ("session", None), ("answered", {})]:
    if name not in st.session_state:
        st.session_state[name] = default


def chat_session():
    """The conversation this chat is having.

    Held in session_state next to the messages, so clearing the messages also drops the
    conversation the agent was reasoning over, and a new chat starts from nothing. It is
    never a module level or otherwise shared value, so one visitor's chat cannot reach
    another's.
    """
    if st.session_state.session is None:
        st.session_state.session = chat_engine.new_chat()
    return st.session_state.session


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
            st.session_state.session = None
            st.session_state.answered = {}
            st.rerun()


# ---------- header ----------

banner = ROOT / "assets/banner.png"
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
                    # A question with options to choose from is also offered as controls,
                    # so it can be answered with a click instead of by typing.
                    render_clarification_controls(index, message["result"])

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
        result = chat_engine.answer(question, model, on_stage=advance, session=chat_session())
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
