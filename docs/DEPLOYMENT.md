# Deploying Chinhook

A deployment is two services:

| Service | What it is | Public? |
|---|---|---|
| **app** | This repository's `Dockerfile`: the Streamlit web app, the agent, and the embedding model running in-process | Yes |
| **Qdrant** | The vector index, `qdrant/qdrant` | No, and never needs to be |

Nothing else is needed on the server side. Visitors bring their own database and their own
model API key on the setup screen, and neither is ever stored.

The app is a long-running Python server that keeps a WebSocket open to every browser, so it
needs a host that runs it as one always-on container: Railway, Render, Fly.io, a VM, anything
with Docker. See [why not Vercel](#why-not-vercel-or-other-function-platforms) for what goes
wrong on a platform that runs it as a function instead.

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

## Why not Vercel or other function platforms

Vercel can run this `Dockerfile` as a container function, and it was tried. Its own logs show
why it does not work for this app:

- **The live connection is cut every 5 minutes.** A function invocation ends at the plan's
  time limit, and the browser's WebSocket ends with it (300 seconds on Hobby). Each cut
  freezes the tab while it reconnects.
- **One browser's requests reach several copies of the app.** A single page load started two
  copies on different machines. Streamlit keeps each visitor's session, including their
  database and model settings and their chat, in the memory of one process. A reconnect that
  reaches another copy starts over at the setup screen, and an image drawn by one copy
  returns 404 from another.

Any platform that runs each request as a function behaves the same way. An always-on
container keeps one process for every session; dropped connections come back to the same
session for five minutes (`server.disconnectedSessionTTL` in `.streamlit/config.toml`).

## Keeping a Vercel address

To keep an existing `*.vercel.app` address (or a domain already on Vercel) pointing at the
app, the repository's root [`vercel.json`](../vercel.json) already does it. It sets the
framework to "Other" with no install or build step, which overrides whatever preset the Vercel
project has (such as "Python"), serves the small fallback page in `deploy/vercel/`, and
redirects every path to the app. Without the override, Vercel finds `app.py` and fails trying
to build it as a Python function ("Found app.py but it does not export a top-level app...");
renaming `app.py` does not help, it only changes the error to "No python entrypoint found".

1. In `vercel.json` (both redirect rules) and `deploy/vercel/index.html`, replace
   `YOUR-APP.up.railway.app` with the app's own address. There are two rules because Vercel
   compiles `/:path*` into a pattern that needs at least one path segment: without the
   separate `/` rule, the bare address would show the fallback page instead of redirecting.
2. Leave the Vercel project's Root Directory at the repository root, so it reads that file.
3. Redeploy. `curl -I https://<your-project>.vercel.app` should answer `307` with a
   `location` header pointing at the app. Environment variables on the Vercel project are no
   longer used and can be removed.

## Operator checklist

- [ ] `ALLOW_PRIVATE_HOSTS` is unset or `false`.
- [ ] Qdrant has `QDRANT__SERVICE__API_KEY` set and no public domain.
- [ ] The app's `QDRANT_API_KEY` matches it.
- [ ] Rate limits and `MAX_INDEX_TABLES` suit how much load the server can take.
- [ ] `INDEX_RETENTION_DAYS` matches what the app tells visitors about how long their index
      is kept.
- [ ] One replica of the app.
