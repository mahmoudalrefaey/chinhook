"""Web interface for asking a connected database questions in plain language.

The Streamlit page. Deployed from the Dockerfile; see README.md for how to run and configure it.

Chat bubbles are written as raw HTML rather than through st.chat_message or a keyed
container. Streamlit wraps every markdown element in its own internal layout box, and one of
those boxes was measuring itself shorter than the text it holds. Because everything in that
chain has overflow left as visible, the short measurement never actually clipped the text,
it just meant a background colour painted on that box stopped short of covering it, which is
what read as a bubble cut off partway through. Painting the background on a div this file
writes directly, sitting inside that box rather than being that box, sidesteps the problem:
the div sizes itself to its own content regardless of what the ancestor around it measured.
"""

from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).parent
ICON = ROOT / "static" / "icon.svg"

st.set_page_config(
    page_title="Chinhook · Chat with your database",
    page_icon=str(ICON) if ICON.exists() else None,
    layout="wide",
    initial_sidebar_state="auto",
)

# Questions that make sense for any database, since nothing here knows what the connected
# one holds until it has been read.
EXAMPLE_QUESTIONS = [
    "What tables are in this database?",
    "Which table has the most rows?",
    "What can I ask about this data?",
    "Summarise what this database is about",
]


def load_styles():
    css = (ROOT / "chinhook" / "ui" / "styles.css").read_text(encoding="utf-8")
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)


load_styles()


try:
    # ui.render itself imports chat_engine, and everything chat_engine pulls in behind it
    # (config, the agent workflow, the database module) at the top of its own file, so its
    # import has to be inside this same try along with the others below. It used to sit at
    # the top of this file instead, well before this point, which meant any failure in that
    # whole chain had already happened and crashed the page with a raw traceback by the time
    # this except clause could have caught anything.
    from chinhook.ui.render import (
        as_frame, bubble, escape_markdown, escape_text, pipeline_html, text_to_html, thinking,
    )
    from chinhook import chat_engine
    from chinhook import config
    from chinhook import connection
    from chinhook import rate_limit
    from chinhook import runtime
    from chinhook import settings_file
    from chinhook.db.indexing import get_index_status
except Exception as exc:  # noqa: BLE001
    # escape_text is one of the names this same try was attempting to import, so it cannot
    # be trusted to exist if the failure happened before that import completed. The
    # standard library's own escaping has no such dependency.
    import html as _html

    st.markdown(
        '<div class="notice"><strong>Something is wrong with this deployment.</strong><br>'
        f"{_html.escape(f'{type(exc).__name__}: {exc}')}<br><br>"
        "The app could not start. Check the deployment's environment and logs.</div>",
        unsafe_allow_html=True,
    )
    st.stop()


# ---------- access ----------

def _require_passphrase() -> bool:
    """Gate the whole page behind one shared passphrase, when one is configured.

    Meant for a small evaluation audience on a link that is otherwise public, not a real
    login system: everyone who has the passphrase shares one identity, and there is no
    per-user account behind it. Left disabled, the default, when APP_PASSPHRASE is not set,
    so a deployment that already controls access another way is not forced through a screen
    it does not need.
    """
    import secrets

    if not config.APP_PASSPHRASE:
        return True
    if st.session_state.get("authenticated"):
        return True

    st.markdown('<div class="page-title">Sign in</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="page-subtitle">This deployment is protected. Enter the passphrase to '
        "continue.</div>",
        unsafe_allow_html=True,
    )
    entered = st.text_input("Passphrase", type="password", label_visibility="collapsed")
    if st.button("Enter", type="primary"):
        # compare_digest rather than ==, so how long the comparison takes does not itself
        # leak how many of the passphrase's characters were guessed correctly. Encoded to
        # bytes first: compare_digest refuses two str arguments outright unless both are
        # pure ASCII, and a passphrase is not guaranteed to be one, in any language.
        if secrets.compare_digest(entered.encode("utf-8"), config.APP_PASSPHRASE.encode("utf-8")):
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("That passphrase is not correct.")
    return False


if not _require_passphrase():
    st.stop()


# ---------- state ----------

for name, default in [("messages", []), ("pending", None), ("session", None), ("answered", {}),
                      ("rate_bucket", rate_limit.new_session_bucket()),
                      # Each attempt to connect costs a real database login and a model call,
                      # so it is limited on its own, separately from questions.
                      ("setup_bucket", rate_limit.TokenBucket(5, 5)),
                      # Opening a configuration file runs scrypt, slow and memory-hungry on
                      # purpose, so trying passphrases is limited on its own too.
                      ("import_bucket", rate_limit.TokenBucket(5, 5)),
                      ("runtime", None), ("index_ready", False)]:
    if name not in st.session_state:
        st.session_state[name] = default


def _forget_connection():
    """Back to the setup screen, with nothing of the previous connection or chat kept."""
    st.session_state.runtime = None
    st.session_state.index_ready = False
    st.session_state.messages = []
    st.session_state.session = None
    st.session_state.answered = {}
    st.session_state.pending = None


# ---------- setup ----------
# The database and model a visitor brings live in st.session_state.runtime and nowhere else:
# not in a file, not in a database, not in the URL. Closing the tab forgets them.

_DIALECTS = {"PostgreSQL": runtime.POSTGRES, "MySQL": runtime.MYSQL}

_READ_ONLY_SQL = {
    runtime.POSTGRES: (
        "CREATE ROLE chat_reader WITH LOGIN PASSWORD 'choose-a-password';\n"
        "GRANT CONNECT ON DATABASE your_database TO chat_reader;\n"
        "GRANT USAGE ON SCHEMA public TO chat_reader;\n"
        "GRANT SELECT ON ALL TABLES IN SCHEMA public TO chat_reader;\n"
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO chat_reader;"
    ),
    runtime.MYSQL: (
        "CREATE USER 'chat_reader'@'%' IDENTIFIED BY 'choose-a-password';\n"
        "GRANT SELECT, SHOW VIEW ON your_database.* TO 'chat_reader'@'%';"
    ),
}


def _banner():
    """The banner, as a plain static file rather than through st.image.

    st.image keeps the picture in the memory of the process that drew the page and serves it
    from there, so on a host that spreads requests over several processes the request for
    the picture can reach one that never had it: that is how the banner went missing on
    Vercel. A file under static/ is served the same by any process, and the browser caches it.
    """
    st.markdown(
        '<img class="banner" src="app/static/banner.png" alt="Chinhook">', unsafe_allow_html=True
    )


def _on_provider_change():
    st.session_state.setup_base_url = connection.PROVIDERS.get(st.session_state.setup_provider, "")


def render_setup():
    _banner()
    # The whole form is one element of the page, so the indexing page that follows replaces
    # it in one piece. Element by element, everything below that page's last element would
    # stay on screen, faded, until indexing finished: Streamlit only clears what a run did
    # not redraw once the run ends, and the indexing run lasts as long as indexing does.
    with st.container():
        _setup_form()


def _setup_form():
    st.markdown('<div class="page-title">Chat with your database</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="page-subtitle">Connect a PostgreSQL or MySQL database and an '
        "OpenAI-compatible model, then ask questions in plain language. What you enter here is "
        "kept only for this browser session and never stored, and every query runs "
        "read-only.</div>",
        unsafe_allow_html=True,
    )
    st.markdown('<div class="rule"></div>', unsafe_allow_html=True)
    _import_section()

    # ---- database
    st.markdown('<div class="panel-label">1 · Your database</div>', unsafe_allow_html=True)
    kind = st.radio("Database", list(_DIALECTS), horizontal=True, key="setup_dialect")
    dialect = _DIALECTS[kind]
    mode = st.radio(
        "Connection details", ["Connection URL", "Separate fields"], horizontal=True, key="setup_mode"
    )
    default_port = "5432" if dialect == runtime.POSTGRES else "3306"
    scheme = "postgresql" if dialect == runtime.POSTGRES else "mysql"
    url = host = port = database = username = password = ""
    if mode == "Connection URL":
        url = st.text_input(
            "Connection URL",
            type="password",
            key="setup_url",
            placeholder=f"{scheme}://user:password@host:{default_port}/database",
            help="The URL your hosting provider gives you. It has to be reachable from the internet.",
        )
    else:
        left, right = st.columns([3, 1])
        host = left.text_input("Host", key="setup_host", placeholder="db.example.com")
        port = right.text_input("Port", key="setup_port", placeholder=default_port)
        database = st.text_input("Database name", key="setup_database")
        left, right = st.columns(2)
        username = left.text_input("User", key="setup_user")
        password = right.text_input("Password", type="password", key="setup_password")

    left, right = st.columns(2)
    schema = ""
    if dialect == runtime.POSTGRES:
        # The default goes into session state rather than value=, so a loaded file can set
        # this field without Streamlit warning that it was given two values.
        if "setup_schema" not in st.session_state:
            st.session_state.setup_schema = "public"
        schema = left.text_input(
            "Schema", key="setup_schema",
            help="Questions are answered from the tables and views in this schema.",
        )
    ssl_mode = right.selectbox(
        "SSL", runtime.SSL_MODES, key="setup_ssl",
        help="prefer: encrypt when the server supports it. require: always encrypt. "
        "disable: never encrypt.",
    )
    with st.expander("Connecting with a read-only login (recommended)"):
        st.markdown(
            "Every connection this app opens is read-only at the database itself, whatever the "
            "login is allowed to do. A login that can only read is still the safest thing to "
            "give it:",
        )
        st.code(_READ_ONLY_SQL[dialect], language="sql")

    # ---- model
    st.markdown('<div class="panel-label">2 · Your model</div>', unsafe_allow_html=True)
    if "setup_base_url" not in st.session_state:
        st.session_state.setup_base_url = connection.PROVIDERS["OpenAI"]
    st.selectbox(
        "Provider", list(connection.PROVIDERS), key="setup_provider", on_change=_on_provider_change
    )
    base_url = st.text_input(
        "Base URL", key="setup_base_url",
        help="Any OpenAI-compatible chat completions endpoint.",
    )
    api_key = st.text_input("API key", type="password", key="setup_api_key")
    model = st.text_input(
        "Model", key="setup_model", placeholder="e.g. gpt-4.1-mini",
        help="The model has to support tool (function) calling: every query is written through one.",
    )
    with st.expander("Advanced"):
        fast_model = st.text_input(
            "Fast model (optional)", key="setup_fast_model",
            help="A cheaper model for the one-line description of each table written while "
            "indexing. Leave empty to use the model above for everything.",
        )

    if not st.button("Connect", type="primary"):
        return

    allowed, wait = st.session_state.setup_bucket.take()
    if not allowed:
        st.error(f"Too many attempts in a short time. Try again in about {wait:.0f} seconds.")
        return
    try:
        if mode == "Connection URL":
            db = connection.database_settings_from_url(url, schema=schema, ssl_mode=ssl_mode)
            if db.dialect != dialect:
                found = "PostgreSQL" if db.dialect == runtime.POSTGRES else "MySQL"
                raise connection.ConnectionSetupError(
                    f"That is a {found} URL, but {kind} is selected above."
                )
        else:
            db = connection.database_settings_from_fields(
                dialect, host, port, database, username, password, schema=schema, ssl_mode=ssl_mode
            )
        llm_settings = connection.llm_settings(base_url, api_key, model, fast_model)
    except connection.ConnectionSetupError as exc:
        st.error(escape_markdown(exc))
        return
    _check_and_connect(runtime.Runtime(db=db, llm=llm_settings))


def _check_and_connect(candidate: runtime.Runtime) -> None:
    """Log in to the database and try the model for real, and connect when both work.

    What is shown here comes from the connection details, which may have come from a file
    someone else wrote, so it is escaped: a database name cannot turn into Markdown.
    """
    with st.status("Checking the connection", expanded=True) as status:
        try:
            st.write(f"Connecting to {escape_markdown(candidate.db.describe())}…")
            count = connection.check_database(candidate)
            st.write(f"Connected: {count} tables and views to ask about.")
            st.write(f"Checking {escape_markdown(candidate.llm.model)}…")
            connection.check_llm(candidate.llm)
            st.write("The model answers and calls tools.")
        except connection.ConnectionSetupError as exc:
            # expanded=True explicitly: left out, the update clears it and the box collapses,
            # hiding the one line that says what to change.
            status.update(label="Could not connect", state="error", expanded=True)
            st.error(escape_markdown(exc))
            return
        status.update(label="Connected", state="complete")

    _forget_connection()
    st.session_state.runtime = candidate
    st.rerun()


# ---------- configuration files ----------
# A visitor can save what they connected with to a file (the export dialog on the chat page)
# and load it on the setup screen next time. The file goes straight from this server to their
# browser and back; see chinhook/settings_file.py for what is in it and how it is encrypted.

# Typed only to open one file, and cleared as soon as that attempt is over.
_IMPORT_SECRETS = ("import_passphrase", "import_password", "import_api_key")


def _import_section():
    """A file exported earlier, as a way past filling in the whole form below."""
    with st.expander("Import a configuration file", icon=":material/upload_file:"):
        st.markdown(
            '<div class="timing">Saved your settings from the chat page before? Load the file '
            "here instead of filling in the form. It is read in this session only, and not "
            "kept.</div>",
            unsafe_allow_html=True,
        )
        uploaded = st.file_uploader(
            "Configuration file", type=["json"], key="import_file", max_upload_size=1,
            label_visibility="collapsed",
        )
        if uploaded is None:
            return
        try:
            info = settings_file.inspect(uploaded.getvalue())
        except settings_file.SettingsFileError as exc:
            st.error(escape_markdown(exc))
            return

        # A form, not loose fields: a password field typed into and then left with Tab (past
        # its own show-password button) never reaches the server on its own, and a form sends
        # every field when it is submitted, however the focus moved between them. It also
        # empties them again once it has (clear_on_submit), on the page as well as here.
        with st.form("import_form", border=False, clear_on_submit=True):
            if info.encrypted:
                st.markdown(
                    f'<div class="timing">This file says it is for {escape_text(info.label)}. '
                    "Its password and API key are encrypted.</div>",
                    unsafe_allow_html=True,
                )
                st.text_input(
                    "Passphrase", type="password", key="import_passphrase",
                    help="The passphrase chosen when this file was exported.",
                )
            else:
                st.markdown(
                    f'<div class="timing">For {escape_text(info.label)}. The file holds no '
                    "password or API key, so enter them here.</div>",
                    unsafe_allow_html=True,
                )
                left, right = st.columns(2)
                left.text_input("Database password", type="password", key="import_password")
                right.text_input("API key", type="password", key="import_api_key")

            left, right = st.columns(2)
            left.form_submit_button(
                "Load and connect", type="primary", width="stretch",
                on_click=_load_settings_file, args=(True,),
            )
            right.form_submit_button(
                "Load into the form", width="stretch", on_click=_load_settings_file, args=(False,),
            )
        notice = st.session_state.pop("import_notice", None)
        if notice is not None:
            kind, message = notice
            (st.error if kind == "error" else st.success)(escape_markdown(message))
        loaded = st.session_state.pop("import_connect", None)
        if loaded is not None:
            _connect_loaded(loaded)


def _load_settings_file(connect: bool) -> None:
    """Read the uploaded file into the setup form and, with connect, connect with it too.

    The import form's submit callback, so it runs before the next run draws anything and may
    still set every setup_* key the setup form's fields read from. The passphrase, and any
    password or key typed in for the file, are cleared here whatever happens, so none of them
    outlives the attempt.
    """
    typed = {name: st.session_state.get(name, "") for name in _IMPORT_SECRETS}
    for name in _IMPORT_SECRETS:
        if name in st.session_state:
            st.session_state[name] = ""
    uploaded = st.session_state.get("import_file")
    if uploaded is None:
        return
    allowed, wait = st.session_state.import_bucket.take()
    if not allowed:
        st.session_state.import_notice = (
            "error", f"Too many attempts in a short time. Try again in about {wait:.0f} seconds."
        )
        return
    try:
        loaded = settings_file.load_settings(uploaded.getvalue(), typed["import_passphrase"])
    except settings_file.SettingsFileError as exc:
        st.session_state.import_notice = ("error", str(exc))
        return
    loaded = loaded.with_secrets(typed["import_password"], typed["import_api_key"])
    _fill_setup_form(loaded)
    if connect:
        st.session_state.import_connect = loaded
    else:
        st.session_state.import_notice = (
            "success", f"Loaded {loaded.describe()}. Check the form below, then press Connect.",
        )


def _fill_setup_form(loaded: settings_file.LoadedSettings) -> None:
    """Put settings read from a file into the setup form, as if they had been typed in."""
    st.session_state.setup_dialect = next(
        name for name, value in _DIALECTS.items() if value == loaded.dialect
    )
    st.session_state.setup_mode = "Connection URL"
    st.session_state.setup_url = loaded.url
    if loaded.dialect == runtime.POSTGRES:
        st.session_state.setup_schema = loaded.schema
    st.session_state.setup_ssl = loaded.ssl_mode
    st.session_state.setup_provider = loaded.provider
    st.session_state.setup_base_url = loaded.base_url
    st.session_state.setup_model = loaded.model
    st.session_state.setup_fast_model = loaded.fast_model
    if loaded.api_key:
        st.session_state.setup_api_key = loaded.api_key


def _connect_loaded(loaded: settings_file.LoadedSettings) -> None:
    """Connect with settings loaded from a file, just as the form's Connect button would."""
    allowed, wait = st.session_state.setup_bucket.take()
    if not allowed:
        st.error(f"Too many attempts in a short time. Try again in about {wait:.0f} seconds.")
        return
    try:
        db = connection.database_settings_from_url(
            loaded.url, schema=loaded.schema, ssl_mode=loaded.ssl_mode
        )
        llm_settings = connection.llm_settings(
            loaded.base_url, loaded.api_key, loaded.model, loaded.fast_model
        )
    except connection.ConnectionSetupError as exc:
        st.error(escape_markdown(exc))
        return
    _check_and_connect(runtime.Runtime(db=db, llm=llm_settings))


def render_prepare():
    """Bring the connected database's index up to date, then go on to the chat.

    The first connection to a database builds its index; a later one reuses it and only
    re-indexes tables whose shape changed since, so reconnecting is usually instant.
    """
    _banner()
    rt = st.session_state.runtime
    st.markdown('<div class="page-title">Preparing your database</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="page-subtitle">Each table is read, described in one sentence by your '
        "model, and indexed so a question can find the tables it needs. This happens the "
        "first time a database is connected; reconnecting later reuses it.</div>",
        unsafe_allow_html=True,
    )
    bar = st.progress(0.0, text="Checking the index")

    def progress(done, total, message):
        bar.progress(min(1.0, done / max(total, 1)), text=message)

    try:
        with runtime.use(rt):
            chat_engine.prepare_index(progress)
    except Exception as exc:  # noqa: BLE001
        bar.empty()
        st.markdown(
            '<div class="notice"><strong>The database could not be indexed.</strong><br>'
            f"{escape_text(chat_engine.describe_index_error(exc, rt.db))}</div>",
            unsafe_allow_html=True,
        )
        with st.expander("Technical detail"):
            st.code(f"{type(exc).__name__}: {exc}", language="text")
        left, right = st.columns(2)
        if left.button("Try again", type="primary"):
            st.rerun()
        if right.button("Change connection"):
            _forget_connection()
            st.rerun()
        return
    st.session_state.index_ready = True
    st.rerun()


# Which pages exist depends on how far this session has got: the setup screen until a database
# and a model are connected, the preparing screen until the index is ready, then the chat and
# the schema. Every run declares them through st.navigation, so an address left over from an
# earlier session (say /schema) lands on the screen that applies now rather than on nothing.
if st.session_state.runtime is None:
    runtime.activate(None)
    st.navigation([st.Page(render_setup, title="Connect", default=True)], position="hidden").run()
    st.stop()

if not st.session_state.index_ready:
    runtime.activate(None)
    st.navigation([st.Page(render_prepare, title="Preparing", default=True)], position="hidden").run()
    st.stop()

runtime.activate(st.session_state.runtime)
TENANT = st.session_state.runtime.tenant_id


# Cached values are keyed by the connected database: st.cache_data is shared by every session
# on this server, and a cache keyed by nothing would hand one visitor's schema to another.
@st.cache_data(ttl=120, show_spinner=False)
def cached_status(tenant):
    status = get_index_status()
    return {
        "db": status["db_tables"],
        "qdrant": status["qdrant_tables"],
        "stale": status["needs_reindex"],
        "changed": status["changed_tables"],
    }


@st.cache_data(ttl=300, show_spinner=False)
def cached_schema(tenant):
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
        # Driven by which stages actually recorded a timing, not by the full list of stages
        # that exist. A greeting or a clarifying question never reaches retrieval, SQL or
        # execution, and showing every node as finished regardless said otherwise.
        st.markdown(
            pipeline_html(
                completed=list(result.get("timings", {}).keys()),
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
                + escape_text(", ".join(tables) or "none")
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
        if not selection:
            # Nothing was actually picked, most likely a mis-click on Send. The question
            # stays open rather than being marked answered, which used to remove the
            # controls for good the moment this happened with nothing selected.
            return
        st.session_state.answered[index] = True
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
            pill = (
                f'<span class="pill pill-{badge}">{escape_text(verdict)}</span>'
                if badge else ""
            )
            tables = ", ".join(task.get("schema_tables") or []) or "no tables"
            reuse = ", reused from earlier in this chat" if task.get("schema_from_cache") else ""
            st.markdown(
                f'<div class="stat"><span class="stat-label">{escape_text(task.get("task_id"))} &middot; '
                f'{escape_text(task.get("intent"))}</span>'
                f'<span class="stat-value">{task.get("row_count", 0)} rows'
                f'{"+" if task.get("truncated") else ""}</span></div>'
                f'<div class="timing">{escape_text(task.get("question", ""))} &middot; '
                f'{escape_text(task.get("status"))} {pill}</div>'
                f'<div class="timing">Tables: {escape_text(tables)}{reuse}</div>',
                unsafe_allow_html=True,
            )
            if task.get("semantic"):
                st.markdown(
                    f'<div class="timing">Grounding: {escape_text(task["semantic"])}</div>',
                    unsafe_allow_html=True,
                )
            if task.get("sql"):
                st.code(task["sql"], language="sql")
            if task.get("failure_reason"):
                st.markdown(
                    f'<div class="timing">Failed: {escape_text(task["failure_reason"])}</div>',
                    unsafe_allow_html=True,
                )


def render_failure(result):
    """Say what went wrong in words a person can act on.

    The technical detail is not thrown away, it is put away: an unexpected error message is
    the kind of thing that is useful to whoever is looking after this and meaningless to
    whoever asked the question, so it is folded away rather than shown or lost.
    """
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


def chat_session():
    """The conversation this chat is having.

    Held in session_state next to the messages, so clearing the messages also drops the
    conversation the agent was reasoning over, and a new chat starts from nothing. It is
    never a module level or otherwise shared value, and never written anywhere, so one
    visitor's chat cannot reach another's.
    """
    if st.session_state.session is None:
        st.session_state.session = chat_engine.new_chat()
    return st.session_state.session


# ---------- exporting the connection ----------

_EXPORT_SECRETS = ("export_passphrase", "export_passphrase_again")


def _forget_export_file():
    """Drop a file made earlier, once what it was made from has changed."""
    st.session_state.pop("export_file", None)


def _create_export_file():
    """Make the export dialog's file from what was chosen in it.

    The export form's submit callback, for the same reason as _load_settings_file: it clears
    the passphrase fields before they are drawn again, so the passphrase is gone once the file
    is made.
    """
    include = st.session_state.get("export_secrets", True)
    passphrase = st.session_state.get("export_passphrase", "")
    repeated = st.session_state.get("export_passphrase_again", "")
    for name in _EXPORT_SECRETS:
        if name in st.session_state:
            st.session_state[name] = ""
    rt = st.session_state.runtime
    try:
        if include:
            settings_file.check_new_passphrase(passphrase, repeated)
            data = settings_file.export_settings(rt, passphrase)
        else:
            data = settings_file.export_settings(rt)
    except settings_file.SettingsFileError as exc:
        st.session_state.export_file = ("error", str(exc))
        return
    st.session_state.export_file = ("file", settings_file.file_name(rt), data)


@st.dialog("Export configuration", icon=":material/download:")
def _export_dialog():
    """Save what this session is connected with, to import on the setup screen next time."""
    rt = st.session_state.runtime
    st.markdown(
        f'<div class="timing">Save the connection to {escape_text(rt.db.describe())} and '
        f"{escape_text(rt.llm.model)} to a file. Next time, import it on the setup screen "
        "instead of filling in the form again.</div>",
        unsafe_allow_html=True,
    )
    include = st.toggle(
        "Include the database password and API key", value=True, key="export_secrets",
        on_change=_forget_export_file,
    )
    # A form for the same reasons as the import section's: both passphrases reach the server
    # when it is submitted, even when someone tabs from one field to the next, and both
    # fields are empty again afterwards.
    with st.form("export_form", border=False, clear_on_submit=True):
        if include:
            st.text_input(
                "Passphrase", type="password", key="export_passphrase",
                help=f"At least {settings_file.MIN_PASSPHRASE_LENGTH} characters. You need it to "
                "import the file, and it cannot be recovered if it is forgotten.",
            )
            st.text_input("Repeat the passphrase", type="password", key="export_passphrase_again")
            st.markdown(
                '<div class="timing">The password and API key are encrypted with this '
                "passphrase (scrypt and AES-256-GCM). Without it, the file is of no use to anyone "
                "who finds it.</div>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                '<div class="timing">The file says where the database and the model are, but '
                "holds no password or API key: you enter those when you import it.</div>",
                unsafe_allow_html=True,
            )
        st.form_submit_button("Create file", type="primary", on_click=_create_export_file)

    result = st.session_state.get("export_file")
    if result is None:
        return
    if result[0] == "error":
        st.error(escape_markdown(result[1]))
        return
    _, name, data = result
    st.download_button(
        f"Download {name}", data=data, file_name=name, mime="application/json",
        icon=":material/download:", on_click="ignore",
    )
    st.markdown(
        '<div class="timing">Made for this download only: this server keeps no copy.</div>',
        unsafe_allow_html=True,
    )


# ---------- sidebar ----------

try:
    index_status = cached_status(TENANT)
except Exception:  # noqa: BLE001
    index_status = None

with st.sidebar:
    st.markdown('<div class="side-brand">Chin<span>hook</span></div>', unsafe_allow_html=True)
    connected = st.session_state.runtime

    st.markdown('<div class="side-heading">Database</div>', unsafe_allow_html=True)
    dialect_label = "PostgreSQL" if connected.db.dialect == runtime.POSTGRES else "MySQL"
    where = escape_text(connected.db.url.host or "")
    if connected.db.dialect == runtime.POSTGRES:
        where += f" &middot; schema {escape_text(connected.db.schema)}"
    st.markdown(
        f'<div class="stat"><span class="stat-label">{dialect_label}</span>'
        f'<span class="stat-value">{escape_text(connected.db.url.database or "")}</span></div>'
        f'<div class="timing">{where}</div>',
        unsafe_allow_html=True,
    )

    st.markdown('<div class="side-heading">Model</div>', unsafe_allow_html=True)
    model_text = escape_text(connected.llm.model)
    if connected.llm.fast_model:
        model_text += f" &middot; fast: {escape_text(connected.llm.fast_model)}"
    st.markdown(f'<div class="timing">{model_text}</div>', unsafe_allow_html=True)

    st.markdown('<div class="side-heading">Index</div>', unsafe_allow_html=True)
    if index_status is None:
        st.markdown(
            '<div class="stat"><span class="stat-label">State</span>'
            '<span class="stat-value">unavailable</span></div>',
            unsafe_allow_html=True,
        )
    else:
        pill = (
            '<span class="pill pill-warn">changed</span>'
            if index_status["stale"]
            else '<span class="pill pill-ok">ready</span>'
        )
        st.markdown(
            f'<div class="stat"><span class="stat-label">Tables and views</span>'
            f'<span class="stat-value">{index_status["db"]}</span></div>'
            f'<div class="stat"><span class="stat-label">Indexed</span>'
            f'<span class="stat-value">{index_status["qdrant"]}</span></div>'
            f'<div class="stat"><span class="stat-label">State</span>{pill}</div>',
            unsafe_allow_html=True,
        )
    if st.button("Refresh index", type="primary", help="Re-index only the tables that changed"):
        allowed, wait = st.session_state.setup_bucket.take()
        if not allowed:
            st.error(f"Try again in about {wait:.0f} seconds.")
        else:
            with st.spinner("Refreshing"):
                try:
                    count = chat_engine.prepare_index()
                    cached_status.clear()
                    cached_schema.clear()
                    st.success(f"Re-indexed {count} table(s)" if count else "Already up to date")
                except Exception as exc:  # noqa: BLE001
                    st.error(chat_engine.describe_index_error(exc))

    st.markdown('<div class="side-heading">Session</div>', unsafe_allow_html=True)
    if st.session_state.messages and st.button("New chat"):
        st.session_state.messages = []
        st.session_state.session = None
        st.session_state.answered = {}
        st.rerun()
    if st.button("Export configuration", help="Save this connection to a file, to import next time"):
        _forget_export_file()
        _export_dialog()
    if st.button("Change connection"):
        _forget_connection()
        st.rerun()
    with st.expander("Your data"):
        st.markdown(
            '<div class="timing">Your connection details are kept only for this browser '
            "session. The index of this database (table definitions, a one-line description "
            "of each table, and common values such as countries or categories) is kept on "
            f"this server and deleted after {config.INDEX_RETENTION_DAYS} days unused.</div>",
            unsafe_allow_html=True,
        )
        if st.button("Delete this database's index"):
            try:
                chat_engine.delete_index()
            except Exception as exc:  # noqa: BLE001
                st.error(chat_engine.describe_index_error(exc))
            else:
                cached_status.clear()
                cached_schema.clear()
                _forget_connection()
                st.rerun()

# ---------- chat ----------

def chat_page():
    """The conversation: history, the question being answered, and the input under it all.

    Laid out the way Streamlit lays out any chat. The input is called at the top level of the
    page, so Streamlit pins it to the bottom of the window itself and keeps the newest message
    in view as the page grows, and the whole page scrolls as one. Nothing above the
    conversation is pinned: the header is one line, and it scrolls away like everything else.
    """
    rt = st.session_state.runtime
    st.markdown(
        '<div class="chat-header"><span class="chat-brand">Chin<span>hook</span></span>'
        f'<span class="chat-meta">{escape_text(rt.db.describe())} &middot; '
        f"{escape_text(rt.llm.model)}</span></div>",
        unsafe_allow_html=True,
    )

    # Read before anything else on the page is drawn, so a question submitted this run is
    # already known to the welcome copy below, which it hides the same run it was asked.
    question = st.chat_input("Ask a question about your data")
    # Written to the same bar Streamlit pins to the bottom of the window, after the input, so it
    # sits directly under it and stays there however long the conversation gets.
    st.bottom.markdown(
        '<div class="chat-footer">Chinhook Supported &amp; Sponsored by GBG Global Brands Group. '
        "All rights reserved</div>",
        unsafe_allow_html=True,
    )
    if st.session_state.pending:
        question = st.session_state.pending
        st.session_state.pending = None

    # The welcome copy is only useful before there is a conversation; once there is one it
    # would only push the messages down. Clearing the conversation empties
    # st.session_state.messages, which is what brings it back rather than some separate flag
    # to keep in sync with that.
    #
    # A plain conditional st.markdown is not enough to make it disappear the moment a question
    # is submitted: chat_engine.answer() below blocks for several seconds, and Streamlit only
    # removes an element a conditional skipped once the whole script run finishes, not as the
    # run reaches the point that skipped it. For a run this long, "finishes" is seconds away,
    # so the welcome copy (and, further down, the example panel) stayed on screen, stale,
    # right through the run that was supposed to hide it - the "two pipelines at once" and
    # leftover subtitle text this was seen as. st.empty() is exactly the escape hatch already
    # in use for the diagram and "thinking" text below: writing to (or clearing) an empty()
    # placeholder sends its own update immediately, mid-script, rather than waiting for the
    # run to end, which is what actually makes it disappear on the same run it was submitted.
    welcome = st.empty()
    if not st.session_state.messages and not question:
        with welcome.container():
            st.markdown('<div class="page-title">Ask the database a question</div>', unsafe_allow_html=True)
            st.markdown(
                '<div class="page-subtitle">Questions are turned into SQL, run read-only against '
                f"{escape_text(st.session_state.runtime.db.describe())}, and answered in plain "
                "language. Every answer carries the tables that were searched and the query that "
                "ran.</div>",
                unsafe_allow_html=True,
            )
            st.markdown('<div class="rule"></div>', unsafe_allow_html=True)
    else:
        welcome.empty()

    hero = st.empty()
    if not st.session_state.messages and not question:
        with hero.container():
            st.markdown(f'<div class="hero-pipeline">{pipeline_html()}</div>', unsafe_allow_html=True)
            st.markdown('<div class="panel-label">Try one of these</div>', unsafe_allow_html=True)
            left, right = st.columns(2)
            for position, example in enumerate(EXAMPLE_QUESTIONS):
                target = left if position % 2 == 0 else right
                if target.button(example, key=f"example_{position}"):
                    st.session_state.pending = example
                    st.rerun()
    else:
        hero.empty()

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

    if question:
        allowed, notice = rate_limit.check(st.session_state.rate_bucket)
        if not allowed:
            # Checked before the question is added to history at all, so a refused
            # question is not shown as though it were asked and never answered.
            st.markdown(f'<div class="notice">{escape_text(notice)}</div>', unsafe_allow_html=True)
        else:
            index = len(st.session_state.messages)
            st.session_state.messages.append({"role": "user", "content": question})
            st.markdown(bubble("user", text_to_html(question), index), unsafe_allow_html=True)

            diagram = st.empty()
            slot = st.empty()
            answer_slot = st.empty()
            finished = ["question"]
            streamed = []

            def advance(label):
                key = chat_engine.STAGE_TO_KEY.get(label)
                diagram.markdown(
                    pipeline_html(active=key, completed=list(finished)),
                    unsafe_allow_html=True,
                )
                slot.markdown(thinking(label), unsafe_allow_html=True)
                if key:
                    finished.append(key)

            def on_token(chunk):
                # The reply has started arriving, so the "thinking" indicator for the
                # answer stage has nothing left to say that the words themselves don't.
                slot.empty()
                streamed.append(chunk)
                answer_slot.markdown(
                    bubble("assistant", text_to_html("".join(streamed)), "live"),
                    unsafe_allow_html=True,
                )

            diagram.markdown(pipeline_html(completed=finished), unsafe_allow_html=True)
            result = chat_engine.answer(
                question, on_stage=advance, on_token=on_token, session=chat_session()
            )
            diagram.empty()
            slot.empty()

            if result["ok"] and result.get("answer"):
                if not streamed:
                    # Nothing streamed: a greeting or a clarification is written outside
                    # the node that streams, so the whole reply arrives at once instead.
                    answer_slot.markdown(
                        bubble("assistant", text_to_html(result["answer"]), "live"),
                        unsafe_allow_html=True,
                    )
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

            # Redraw everything from stored state so the streamed message and the stored
            # one do not both survive, which would show the detail panel twice.
            st.rerun()


# ---------- schema ----------

def schema_page():
    """Every table and view in the connected schema, read from the database itself."""
    st.markdown('<div class="page-title">What is in the database</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="page-subtitle">Read from the database itself rather than written down '
        "anywhere. This is the same introspection the indexer uses, so it is exactly what the "
        "model can be shown. Row counts come from the database's own statistics and are "
        "approximate.</div>",
        unsafe_allow_html=True,
    )
    st.markdown('<div class="rule"></div>', unsafe_allow_html=True)

    try:
        tables = cached_schema(TENANT)
        totals = st.columns(3)
        figures = [
            ("Tables", f"{len(tables)}"),
            ("Columns", f"{sum(len(t['columns']) for t in tables)}"),
            ("Rows (approx.)", f"{sum(t['rows'] for t in tables):,}"),
        ]
        for column, (label, value) in zip(totals, figures, strict=True):
            column.markdown(
                f'<div class="figure"><div class="figure-value">{value}</div>'
                f'<div class="figure-label">{label}</div></div>',
                unsafe_allow_html=True,
            )

        st.markdown('<div class="panel-label">Tables</div>', unsafe_allow_html=True)
        for table in tables:
            kind = " · view" if table.get("kind") == "view" else ""
            with st.expander(f"{table['table']}{kind}  (about {table['rows']:,} rows)"):
                st.dataframe(
                    pd.DataFrame(table["columns"], columns=["Column", "Type"]),
                    width="stretch",
                    hide_index=True,
                )
    except Exception as exc:  # noqa: BLE001
        st.markdown(
            f'<div class="notice"><strong>Could not read the schema.</strong><br>'
            f"{escape_text(f'{type(exc).__name__}: {exc}')}</div>",
            unsafe_allow_html=True,
        )


# ---------- pages ----------
# The sidebar above is drawn on both pages; Streamlit puts this menu at the top of it.
st.navigation(
    [
        st.Page(chat_page, title="Chat", icon=":material/forum:", default=True),
        st.Page(schema_page, title="Schema", icon=":material/table_view:", url_path="schema"),
    ]
).run()
