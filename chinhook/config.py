"""Settings that belong to whoever runs this deployment, read once from the environment.

Set them as the hosting platform's environment variables (on FastAPI Cloud: the
app's Environment Variables, or `fastapi cloud env set`). Nothing here describes a visitor's database or model: those are
entered on the setup screen and held per browser session (see chinhook/runtime.py).
What is left is the deployment's own business: where the vector index lives, which embedding
model fills it and where that runs, and the limits that keep one visitor from using up the
server for everyone else.
"""

import os


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


# ---------- Qdrant ----------
# Any Qdrant reachable by URL: Qdrant Cloud, a service next to the app on a private network, a
# managed instance elsewhere. Only these two values change when it moves. Required: there is
# no sensible default for where a deployment's index lives.
QDRANT_URL = os.getenv("QDRANT_URL", "").strip()
# The key Qdrant itself is configured with (its QDRANT__SERVICE__API_KEY).
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY") or None
# Every collection this app creates starts with this, so one Qdrant can be shared with other
# things without the retention cleanup ever touching a collection it did not create.
QDRANT_COLLECTION_PREFIX = os.getenv("QDRANT_COLLECTION_PREFIX", "chinhook")

# ---------- Embedding ----------
# Who turns text into vectors, so a visitor only has to bring a chat model:
#   "qdrant-cloud": Qdrant Cloud Inference, inside the cluster itself. The app ships text and
#                   Qdrant embeds it, so this server holds no model and needs little memory.
#                   Requires QDRANT_URL to be a Qdrant Cloud cluster.
#   "fastembed":    in-process, on this server's CPU, with any Qdrant. Needs the image built
#                   with the local-embeddings extra (see the Dockerfile) and about 1 GB of RAM.
EMBED_PROVIDER = os.getenv("EMBED_PROVIDER", "qdrant-cloud").strip().lower()
if EMBED_PROVIDER not in ("qdrant-cloud", "fastembed"):
    raise RuntimeError(f'EMBED_PROVIDER must be "qdrant-cloud" or "fastembed", not "{EMBED_PROVIDER}".')
# Changing it is safe at any time: the model is part of every index's name, so a different
# model builds new indexes rather than mixing incompatible vectors into old ones. The defaults
# are free to run: all-MiniLM-L6-v2 costs nothing on Qdrant Cloud, free clusters included.
EMBED_MODEL = os.getenv("EMBED_MODEL") or (
    "sentence-transformers/all-minilm-l6-v2" if EMBED_PROVIDER == "qdrant-cloud" else "BAAI/bge-small-en-v1.5"
)
# The model's vector size. Only needed for a Qdrant Cloud model the app does not already know
# (see chinhook/db/clients.py); fastembed reports its own.
EMBED_DIM = _int("EMBED_DIM", 0)
# Where fastembed caches the model files. The Docker image downloads the model here at build
# time, so a fresh container does not fetch it on its first question.
EMBED_CACHE_DIR = os.getenv("FASTEMBED_CACHE_PATH") or None

# ---------- Indexing ----------
# How long an index nobody has connected to is kept before it is deleted. An index holds table
# definitions, a one-line description of each table and a sample of low-cardinality values
# from the visitor's own data, so it is not kept forever. 0 keeps indexes forever.
INDEX_RETENTION_DAYS = _int("INDEX_RETENTION_DAYS", 7)
# A database with more tables than this is refused at setup rather than indexed: indexing
# costs one model call per table on the visitor's key and real CPU on this server.
MAX_INDEX_TABLES = _int("MAX_INDEX_TABLES", 200)
# How many databases can be indexing at once across every session on this server.
MAX_CONCURRENT_INDEX_JOBS = _int("MAX_CONCURRENT_INDEX_JOBS", 2)

# ---------- Access and rate limiting ----------
# An optional shared passphrase in front of the whole app, for a private deployment. Unset
# means no gate: every visitor brings their own database and model key anyway.
APP_PASSPHRASE = os.getenv("APP_PASSPHRASE") or None
RATE_LIMIT_PER_SESSION_PER_MINUTE = _int("RATE_LIMIT_PER_SESSION_PER_MINUTE", 10)
RATE_LIMIT_GLOBAL_PER_MINUTE = _int("RATE_LIMIT_GLOBAL_PER_MINUTE", 120)

# ---------- Sessions ----------
# A visitor's connection details live in this server's memory for as long as their session
# does, and nowhere else. A session nobody has used for this long is forgotten, secrets and
# chat included; closing the tab forgets it from the browser's side straight away.
SESSION_IDLE_MINUTES = _int("SESSION_IDLE_MINUTES", 60)
# How many sessions one server holds at once. Past this the longest-idle ones are dropped
# first, so memory stays bounded however many visitors arrive.
MAX_SESSIONS = _int("MAX_SESSIONS", 500)
