"""Chinhook: ask a PostgreSQL or MySQL database questions in plain language.

    runtime        the database and model one browser session is connected to
    config         the deployment's own settings, read from the environment
    connection     turning what was typed on the setup screen into settings, and checking them
    chat_engine    the one entry point the web app calls: answer, index, schema
    rate_limit     per-session and global question limits
    agent/         the LangGraph workflow that turns a question into checked SQL and an answer
    db/            database access, the vector index, and read-only SQL execution
    ui/            rendering helpers and the page's styles

The Streamlit page itself is app.py at the repository root, next to static/, which is where
Streamlit serves static files from.
"""
