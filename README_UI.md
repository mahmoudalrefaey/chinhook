# Web interface

A browser interface for asking the Chinook database questions in plain language. The
command line version in `main.py` is untouched and still works the same way.

## What you need running first

Four things have to be up before the interface will start. It will tell you which one is
missing if any of them are not.

**Postgres with the Chinook data.** Any Postgres instance works as long as the Chinook
tables are loaded and `DATABASE_URL` points at it. Note that `scripts/db_module.py` connects
with `sslmode="require"`, so the server has to support SSL. A stock local Postgres does not
by default.

**Qdrant.** Holds the embedded table definitions used to pick which tables are relevant to a
question. The default `QDRANT_URL` is `http://localhost:6333`.

**Ollama**, with the `nomic-embed-text` model pulled:

```
ollama pull nomic-embed-text
```

Ollama has to run on the same machine, because `config.py` points at `127.0.0.1:11434` and
that value is not read from the environment.

**Azure OpenAI**, with two deployments. Fill in `AZURE_OPENAI_KEY`, `DEPLOYMENT1_NAME`,
`AZURE_OPENAI_ENDPOINT1`, `DEPLOYMENT2_NAME` and `AZURE_OPENAI_ENDPOINT2` in `.env`. Copy
`.env.example` if you do not have one yet. The deployment names are the names you gave the
deployments in Azure AI Foundry, which are not always the same as the model names.

## Running it

```
uv run --with streamlit streamlit run app.py
```

It opens on `http://localhost:8501`.

Streamlit is pulled in for the run only. It is deliberately not added to `pyproject.toml`,
so the project dependencies stay exactly as they were.

If the styling ever looks wrong after a Streamlit release, pin the version that worked:

```
uv run --with streamlit==<version> streamlit run app.py
```

The interface restyles Streamlit's own markup, and that markup occasionally changes between
releases.

## Using it

Four example questions appear before you ask anything. Click one or type your own.

Every answer has a panel underneath it called "How this answer was produced". Opening it
shows the tables that were retrieved for that question, the SQL the model wrote, the rows
that came back, and how long each step took. This is the quickest way to check that an
answer came from the database rather than from the model's imagination.

The sidebar switches between the two Azure deployments, shows how many tables are in the
database against how many are indexed, and rebuilds the index.

## Things worth knowing

**Answers vary between runs.** The model call does not set a temperature, matching the
command line behaviour, so asking the same question twice can produce different SQL and
occasionally a different answer.

**One failed query ends the session.** The database connection is shared and module level.
If the model writes SQL that errors, the connection is left in an aborted transaction and
every question after that fails too. The interface will tell you when this has happened.
Restarting the app clears it.

**Rebuilding the index adds duplicates.** Each rebuild inserts a fresh copy of every table
rather than replacing the existing entries, so the collection grows each time. Duplicates
crowd out useful tables during retrieval and answers get worse. If that happens, delete the
collection and index once:

```
curl -X DELETE http://localhost:6333/collections/schema_tables
uv run python -m scripts.indexer --full
```

## Files

| File | Purpose |
|---|---|
| `app.py` | The interface itself |
| `chat_engine.py` | Runs a question through retrieval, the model, and the database |
| `assets/styles.css` | All of the styling and animation |
| `.streamlit/config.toml` | Theme colours for the widgets CSS cannot reach |
