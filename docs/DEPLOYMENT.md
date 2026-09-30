# Deploying Chinhook

A deployment is two services:

| Service | What it is | Public? |
|---|---|---|
| **app** | This repository's `Dockerfile`: the Streamlit web app, the agent, and the embedding model running in-process | Yes |
| **Qdrant** | The vector index, `qdrant/qdrant` | No, and never needs to be |

Nothing else is needed on the server side. Visitors bring their own database and their own
model API key on the setup screen, and neither is ever stored.

The app is a long-running Python server that keeps a WebSocket open to every browser, so it
needs a host that runs containers: Railway, Render, Fly.io, a VM, anything with Docker.
Serverless and static platforms (Vercel, Netlify, Cloudflare Pages) cannot run it.

## On Railway

### 1. Qdrant

1. In a Railway project, **New → Docker Image**, image `qdrant/qdrant:v1.19.1`.
2. **Settings → Volumes**: add a volume mounted at `/qdrant/storage`, so indexes survive a
   redeploy.
3. **Variables**:
   - `QDRANT__SERVICE__API_KEY` — a long random string (for example the output of
     `openssl rand -hex 32`). Qdrant refuses every request without it.
4. Do **not** generate a public domain for it. The app reaches it over Railway's private
   network.

If the app later cannot reach Qdrant over the private network, also set
`QDRANT__SERVICE__HOST=::` on the Qdrant service: some Railway environments' private network
is IPv6-only, and Qdrant listens on IPv4 by default.

### 2. The app

1. **New → GitHub Repo**, this repository. Railway finds the `Dockerfile` and builds it.
2. **Variables** (Railway fills in `${{...}}` references from the Qdrant service; use the
   Qdrant service's actual name if it is not `Qdrant`):

   | Variable | Value |
   |---|---|
   | `QDRANT_URL` | `http://${{Qdrant.RAILWAY_PRIVATE_DOMAIN}}:6333` |
   | `QDRANT_API_KEY` | `${{Qdrant.QDRANT__SERVICE__API_KEY}}` |
   | `INDEX_RETENTION_DAYS` | `7` (or your choice; `0` keeps indexes forever) |
   | `MAX_INDEX_TABLES` | `200` |
   | `MAX_CONCURRENT_INDEX_JOBS` | `2` |
   | `RATE_LIMIT_PER_SESSION_PER_MINUTE` | `10` |
   | `RATE_LIMIT_GLOBAL_PER_MINUTE` | `120` |
   | `APP_PASSPHRASE` | optional: a shared passphrase in front of the whole app |

   Leave `ALLOW_PRIVATE_HOSTS` unset. It must never be `true` on a public deployment: it would
   let any visitor point the app at Railway's private network, Qdrant included.
3. **Settings → Networking**: generate a public domain (or attach your own).
4. **Settings → Deploy**: healthcheck path `/_stcore/health`.
5. Give it at least **1 GB of memory**: the embedding model, Streamlit and a few concurrent
   indexing jobs together use most of that.

Railway sets `PORT` itself and the app listens on it. Keep the service at **one replica**:
each browser's session lives in the memory of the process it connected to.

### 3. Check it

Open the public URL, connect a database and a model, and ask a question. In the Qdrant
service's logs you should see collections named `chinhook_<16 hex characters>_tables` and
`_values` appear when a database is first connected.

## Moving Qdrant somewhere else

The app only knows Qdrant as `QDRANT_URL` and `QDRANT_API_KEY`, so moving it is a change to
those two variables and nothing else:

- **Qdrant Cloud**: create a cluster, then set `QDRANT_URL` to the cluster's URL (for example
  `https://xxxxxxxx.eu-central-1-0.aws.cloud.qdrant.io:6333`) and `QDRANT_API_KEY` to its key.
- **Any Docker host**: run `qdrant/qdrant` with a volume and `QDRANT__SERVICE__API_KEY`, and
  point `QDRANT_URL` at it, preferably over a private network rather than the internet.

Existing indexes do not need to be moved: when a database connects and its index is missing,
it is rebuilt. Moving the data instead is possible with Qdrant's own snapshots.

## Running the app somewhere else

Anywhere that runs the `Dockerfile` works the same way: set the variables above, expose the
port the platform gives in `$PORT` (8501 if it gives none), and point the platform's health
check at `/_stcore/health`. On Render, use a Web Service from the repository; on Fly.io,
`fly launch` picks up the `Dockerfile`.

## Operator checklist

- [ ] `ALLOW_PRIVATE_HOSTS` is unset or `false`.
- [ ] Qdrant has `QDRANT__SERVICE__API_KEY` set and no public domain.
- [ ] The app's `QDRANT_API_KEY` matches it.
- [ ] Rate limits and `MAX_INDEX_TABLES` suit how much load the server can take.
- [ ] `INDEX_RETENTION_DAYS` matches what the app tells visitors about how long their index
      is kept.
- [ ] One replica of the app.
