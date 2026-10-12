"""Chinhook: ask a PostgreSQL or MySQL database questions in plain language.

    runtime        the database and model one browser session is connected to
    config         the deployment's own settings, read from the environment
    connection     turning what was typed on the setup screen into settings, and checking them
    settings_file  saving a session's settings to a file and loading them back, encrypted
    chat_engine    the one entry point the web app calls: answer, index, schema
    rate_limit     per-session and global question limits
    agent/         the LangGraph workflow that turns a question into checked SQL and an answer
    db/            database access, the vector index, and read-only SQL execution
    web/           the web server, its JSON API, sessions, and the page (HTML, CSS, JS)

Run it with: uvicorn chinhook.web.server:app. The brand images it serves at /static live in
static/ at the repository root.
"""
