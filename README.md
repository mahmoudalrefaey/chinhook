<div align="center">

# Chinhook Database Chat

![Our Banner](static/banner.png)

### Ask your PostgreSQL or MySQL database questions in plain language.

Connect a database and any OpenAI-compatible model, and ask. An agent built on **LangGraph** finds the tables a question needs through a **Qdrant** index of your schema, writes read-only SQL, checks and runs it, verifies the result, and streams back an answer that shows exactly which query produced it.

<br/>

![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=for-the-badge&logo=python&logoColor=white)
![LangGraph](https://img.shields.io/badge/Workflow-LangGraph-1C3C3C?style=for-the-badge)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-4169E1?style=for-the-badge&logo=postgresql&logoColor=white)
![MySQL](https://img.shields.io/badge/MySQL-4479A1?style=for-the-badge&logo=mysql&logoColor=white)
![OpenAI-compatible](https://img.shields.io/badge/LLM-OpenAI--compatible-412991?style=for-the-badge)
![Qdrant](https://img.shields.io/badge/Vector%20Store-Qdrant-DC244C?style=for-the-badge)
![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)

</div>

---

## Contents

- [At a glance](#at-a-glance)
- [Using it](#using-it)
- [What happens to your data](#what-happens-to-your-data)
- [How it works](#how-it-works)
- [Supported databases and models](#supported-databases-and-models)
- [Running it yourself](#running-it-yourself)
- [Configuration](#configuration)
- [Project structure](#project-structure)
- [Safety and reliability](#safety-and-reliability)
- [Limitations](#limitations)
- [Contributing and security](#contributing-and-security)

---

## At a glance

- **Bring your own database and model.** You enter a connection and an API key on the setup screen. Nothing is configured in code, and nothing you enter is stored.
- **Read-only, enforced by the database itself.** Every connection the app opens is a read-only session, even if the login you give it could write.
- **Built for real schemas.** Any PostgreSQL schema or MySQL database, views included, mixed-case names included. The schema is searched, not pasted whole into a prompt, so it works the same with eleven tables or two hundred.
- **Every answer shows its work.** The tables that were searched, the SQL that ran, the rows it returned, how each step went, and what it cost in tokens.

---

## Using it

1. **Open the app** (a hosted instance, or [your own](#running-it-yourself)).
2. **Connect your database.** PostgreSQL or MySQL, as the connection URL your provider gives you or as separate fields. For PostgreSQL, pick the schema to answer from (`public` by default). The database has to be reachable from the internet, since the app connects to it from its server. A login that can only read is recommended; the setup screen shows the SQL to create one.
3. **Choose your model.** Pick a provider (OpenAI, OpenRouter, Groq, Google Gemini, Anthropic, Mistral, Azure OpenAI) or enter any OpenAI-compatible base URL, then your API key and a model name. The model must support tool (function) calling. Under *Advanced*, a cheaper *fast model* can take over the small jobs.
4. **Connect.** The app logs in to your database for real, checks it can see your tables, and checks the model answers and calls tools. Each problem is reported as what to change.
5. **Wait for indexing, once.** The first time a database is connected, each table is read and described in one sentence by your model, then indexed. Reconnecting later reuses the index and only re-indexes tables that changed.
6. **Ask.** A question that could mean two things ("How many Americans?": customers or employees?) is asked back, once, with the options to pick from. Under each answer, **How this answer was produced** shows the pipeline, the tables, the SQL, the rows and the tasks the question became.

The sidebar shows what is connected. **Refresh index** picks up schema changes, re-indexing only the tables that changed. **Change connection** starts over. **Delete this database's index** removes everything the server holds about your database.

---

## What happens to your data

| What | Where it goes | How long |
|---|---|---|
| Your database URL, login and password, your model API key | The server's memory, for your browser session only | Until you close the tab or change connection. Never written to disk, a database or the URL |
| Your database | Read by read-only queries only. Nothing is ever written to it | — |
| The index: table and column names, a one-line description of each table, and common values of category-like columns (countries, statuses, genres) | The deployment's Qdrant | Deleted after `INDEX_RETENTION_DAYS` (7 by default) with nobody using it, or at once with **Delete this database's index** |
| Your questions, the relevant part of your schema, the rows a query returns, and at index time a few sample rows per table | Your model provider, through the API key you gave | As that provider keeps them |

Columns that look like contact details (email, phone, address, names) are masked in the sample rows sent to the model, and are never indexed as values.

---

## How it works

```mermaid
flowchart TD
    START([message]) --> route
    route -->|greeting| greeting[end]
    route -->|clarification reply| understand
    route -->|question| understand
    understand -->|ambiguous| clarify[clarify: ask once] --> END2([end])
    understand -->|unsupported| answer
    understand -->|tasks| fanout{{"Send: one subgraph per task, in parallel"}}

    subgraph TASK["Each task's own subgraph"]
        retrieve[retrieve] --> generate[generate SQL]
        generate --> check[check + run]
        check -->|invalid or failed| repair[repair]
        check -->|ok| verify[verify]
        verify -->|fail, attempts left| repair
        repair --> retrieve
        verify -->|pass or attempts exhausted| done([task result])
    end

    fanout --> TASK
    done --> answer[answer: stream the reply] --> END3([end])
```

An ordinary question costs three model calls: understand it once, write SQL once per task, write the answer once, streamed.

1. **Route.** Small talk and replies to an open clarification are recognised without a model call.
2. **Understand.** One call reads the message against the conversation and the retrieved slice of the schema, splits a compound question into independent tasks, and decides whether anything has to be asked first.
3. **Retrieve.** Hybrid search over the index: dense embeddings plus word overlap with table and column names, expanded with the tables on the foreign-key path between the ones that matched. A second index of real values matches "Americans" to `country = 'USA'`.
4. **Generate.** The model writes one `SELECT` through a tool call, given only the retrieved slice of the schema.
5. **Check and run.** Parsed with `sqlglot` in your database's dialect: one statement, `SELECT` only, real tables only, nothing on the forbidden list. It runs in a read-only transaction with a time limit and a row cap.
6. **Verify.** Deterministic checks: did it run, does the shape match what was asked ("top 5" returns at most five rows, a count returns one number), does it read the table the question was about. A failure is repaired with its reason fed back, a bounded number of times.
7. **Answer.** Written only from verified results and streamed as it is written. A large result's table is built from the verified rows, not retyped by the model.

### Architecture

```mermaid
flowchart TB
    subgraph BROWSER["Browser"]
        UI["Setup screen → chat"]
    end

    subgraph APP["App server · Dockerfile"]
        WEB["Streamlit · app.py"]
        RT["Session runtime\n(database + model, memory only)"]
        FLOW["LangGraph agent · agent/"]
        EMB["Embeddings\n(fastembed, in-process)"]
    end

    subgraph YOURS["Yours"]
        DB[("PostgreSQL / MySQL\nread-only session")]
        LLM["OpenAI-compatible model"]
    end

    QD[("Qdrant\none index per database")]

    UI <--> WEB --> RT --> FLOW
    FLOW --> DB
    FLOW --> LLM
    FLOW --> EMB
    FLOW --> QD
```

Each browser session holds its own connection settings. Every database gets its own connection pool and its own pair of Qdrant collections, named from a hash of the database, schema, user and embedding model, so no session can reach another's database, schema or index.

---

## Supported databases and models

**Databases.** PostgreSQL and MySQL, including their hosted forms: Supabase, Neon, Amazon RDS and Aurora, Google Cloud SQL, Azure Database, Railway, Render, PlanetScale and others. Tested on PostgreSQL 18 and MySQL 8.4. MariaDB should work but is less tested. The database must accept connections from the internet; `localhost` and private networks are refused on a public deployment.

**Models.** Any endpoint that speaks the OpenAI chat completions API and supports tool calling: OpenAI, Azure OpenAI (`https://<resource>.openai.azure.com/openai/v1`), OpenRouter, Groq, Google Gemini's and Anthropic's OpenAI-compatible endpoints, Mistral, Together, a self-hosted vLLM. Reasoning models that refuse `temperature`, and servers that only know `max_tokens`, are handled automatically.

---

## Running it yourself

### With Docker (recommended)

```bash
git clone https://github.com/mahmoudalrefaey/chinhook.git
cd chinhook
docker compose up --build
```

Open <http://localhost:8501> and connect a database and model on the setup screen. Qdrant runs in its own container; nothing else is needed.

To try it without a database of your own, add the sample one, PostgreSQL with the Chinook music-store dataset and a read-only login already created:

```bash
docker compose -f docker-compose.yml -f docker-compose.local-db.yml up --build
```

then connect with `postgresql://chinhook_ro:chinook_ro_local_dev@postgres:5432/chinook` and SSL set to `disable`.

The compose files set `ALLOW_PRIVATE_HOSTS=true` for the app, so it can reach a database on your machine (use `host.docker.internal` as the host) or in the compose network. Never set it on a public deployment.

### Without Docker

Needs Python 3.12, [uv](https://docs.astral.sh/uv/), and a Qdrant to talk to:

```bash
docker run -d -p 6333:6333 qdrant/qdrant:v1.19.1
cp .env.example .env              # then set ALLOW_PRIVATE_HOSTS=true to reach a local database
uv sync
uv run streamlit run app.py
```

The first run downloads the embedding model (about 70 MB).

### Deploying

See **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**: the app and Qdrant on Railway step by step, how to move Qdrant to Qdrant Cloud or any other host by changing two variables, and a checklist for a public deployment.

---

## Configuration

Everything below belongs to whoever runs the deployment and is read from the environment (or `.env`). Visitors' databases and models are never configured here.

| Variable | Default | Purpose |
|---|---|---|
| `QDRANT_URL` | `http://localhost:6333` | Where Qdrant is |
| `QDRANT_API_KEY` | — | Qdrant's API key; required whenever Qdrant is reachable over a network |
| `QDRANT_COLLECTION_PREFIX` | `chinhook` | Prefix of every collection the app creates |
| `EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | [fastembed](https://github.com/qdrant/fastembed) model used for the index; changing it rebuilds indexes as they are used |
| `INDEX_RETENTION_DAYS` | `7` | Days an unused index is kept; `0` keeps indexes forever |
| `MAX_INDEX_TABLES` | `200` | Databases with more tables and views are refused rather than indexed |
| `MAX_CONCURRENT_INDEX_JOBS` | `2` | Databases indexing at the same time |
| `ALLOW_PRIVATE_HOSTS` | `false` | Local development only: allow databases and models on localhost or private networks |
| `APP_PASSPHRASE` | — | Optional shared passphrase in front of the whole app |
| `RATE_LIMIT_PER_SESSION_PER_MINUTE` | `10` | Questions per browser session per minute |
| `RATE_LIMIT_GLOBAL_PER_MINUTE` | `120` | Questions across all sessions per minute |

---

## Project structure

```text
chinhook/
├── app.py                  # Streamlit app: setup, indexing progress, chat and schema tabs
├── chat_engine.py          # The one entry point the app calls: answer, index, schema
├── connection.py           # Setup: reading what was typed, allowed hosts, database and model checks
├── runtime.py              # The database and model one browser session is connected to
├── config.py               # The deployment's own settings, from the environment
├── rate_limit.py           # Per-session and global limits
├── agent/
│   ├── graph.py            # Parent graph: routing, parallel tasks, timing
│   ├── task_graph.py       # One task: retrieve, write SQL, check and run, verify, repair
│   ├── llm.py              # OpenAI-compatible model calls, streaming, token accounting
│   ├── router.py           # Routing without a model call
│   ├── schema.py           # Per-database catalog cache and schema retrieval
│   ├── verify.py           # Deterministic result checks
│   ├── state.py            # Typed workflow and session state
│   └── nodes/              # route, understand, clarify, greeting, answer, prompts
├── scripts/db/
│   ├── clients.py          # Per-database connection pools, Qdrant, embeddings
│   ├── dialects.py         # What differs between PostgreSQL and MySQL
│   ├── introspection.py    # Tables, views, columns, foreign keys, row estimates
│   ├── fingerprinting.py   # Whether the index still matches the database
│   ├── indexing.py         # Building and updating the index
│   ├── retention.py        # Expiring indexes nobody uses
│   ├── retrieval.py        # Hybrid table search and value search
│   ├── evidence.py         # One-line description of each table
│   ├── values.py           # Which column values are worth indexing
│   ├── pii.py              # Columns treated as contact details
│   └── sql.py              # SQL validation and read-only execution
├── ui/render.py            # Markdown rendering and sanitising
├── assets/                 # Styles, icon, banners
├── docker/postgres-initdb/ # The sample Chinook database and its read-only login
├── docs/DEPLOYMENT.md
├── Dockerfile
├── docker-compose.yml
├── docker-compose.local-db.yml
└── .env.example
```

---

## Safety and reliability

- **Read-only at the database.** PostgreSQL connections run every transaction as `BEGIN READ ONLY`. MySQL connections are `SET SESSION TRANSACTION READ ONLY` and start each borrow with `START TRANSACTION READ ONLY`. A write fails at the database even with a superuser login, even if every check above it were fooled.
- **SQL validation.** One statement, `SELECT` (or `UNION`/`INTERSECT`/`EXCEPT` of them) only, the whole tree walked for writes hidden in CTEs, and per-dialect forbidden functions (`pg_sleep`, `pg_read_file`, `dblink`, `SLEEP`, `LOAD_FILE`, `INTO OUTFILE` and more).
- **Only your schema.** A query may only read tables and views of the connected schema, plus the database's own catalog (`information_schema`, and `pg_catalog` on PostgreSQL) for questions about the database itself.
- **Bounded.** 10-second limit per question's query, 500 rows at most (flagged when cut short, never passed off as complete), a bounded number of repair attempts, limits on tables indexed and on concurrent indexing, and per-session rate limits.
- **No reaching the server's own network.** Database hosts and model URLs must resolve to public addresses. `localhost`, private ranges, cloud metadata addresses and names such as `qdrant.railway.internal` are refused, and the model client does not follow redirects.
- **Isolation between visitors.** Connection settings, chat history, schema caches, connection pools, model clients (keyed by API key) and indexes are all per session or per database, never shared.
- **Sanitised output.** Answers are Markdown rendered through an allowlist HTML sanitiser, so neither a model's reply nor a value in your data can inject script into the page.

---

## Limitations

- Retrieval is only as good as the index: a table with an unhelpful name and no distinguishing sample data is harder to find.
- Cross-task dependencies are limited to a shared filter carried from one task's question into another's; tasks do not share actual query results.
- Row counts shown in the Schema tab come from the database's statistics and are approximate.
- Sessions live in the memory of one app process: run one replica, and a restart asks everyone to connect again (indexes are kept).
- The optional login is one shared passphrase, not per-user accounts.

---

## Contributing and security

Issues and pull requests are welcome. Please report security problems privately, as described in [SECURITY.md](SECURITY.md).

Licensed under the [MIT License](LICENSE).

---

<div align="center">

**Built around a simple idea:** a database answer should be grounded in the data, checked before it is reported, and understandable to the person who asked.

</div>
