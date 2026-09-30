"""Settings that belong to whoever runs this deployment, read once from the environment.

Nothing here describes a user's database or model: those are entered on the setup screen and
held per browser session (see runtime.py). What is left is the deployment's own business:
where the vector index lives, which embedding model runs in-process, and the limits that
keep one visitor from using up the server for everyone else.
"""

import os

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


# ---------- Qdrant ----------
# Any Qdrant reachable by URL: a container next to the app, Qdrant Cloud, a managed instance
# elsewhere. Only these two values change when it moves.
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
# Unset for a local Qdrant running with no auth at all. Set it to the key Qdrant itself is
# configured with (QDRANT__SERVICE__API_KEY) anywhere it is reachable over a network.
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY") or None
# Every collection this app creates starts with this, so one Qdrant can be shared with other
# things without the retention cleanup ever touching a collection it did not create.
QDRANT_COLLECTION_PREFIX = os.getenv("QDRANT_COLLECTION_PREFIX", "chinhook")

# ---------- Embedding ----------
# Runs in-process through fastembed, on the CPU, so a user only has to bring a chat model.
# Changing it is safe at any time: the model is part of every index's name, so a different
# model builds new indexes rather than mixing incompatible vectors into old ones.
EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-small-en-v1.5")
# Where the model files are cached. The Docker image downloads the model here at build time,
# so a fresh container does not fetch it on its first question.
EMBED_CACHE_DIR = os.getenv("FASTEMBED_CACHE_PATH") or None

# ---------- Indexing ----------
# How long an index nobody has connected to is kept before it is deleted. An index holds table
# definitions, a one-line description of each table and a sample of low-cardinality values
# from the user's own data, so it is not kept forever.
INDEX_RETENTION_DAYS = _int("INDEX_RETENTION_DAYS", 7)
# A database with more tables than this is refused at setup rather than indexed: indexing
# costs one model call per table on the user's key and real CPU on this server.
MAX_INDEX_TABLES = _int("MAX_INDEX_TABLES", 200)
# How many databases can be indexing at once across every session on this server.
MAX_CONCURRENT_INDEX_JOBS = _int("MAX_CONCURRENT_INDEX_JOBS", 2)

# ---------- Network ----------
# A public deployment must not be usable to reach its own private network (the Qdrant next
# to it, a cloud metadata address) by entering one as a "database host" or a "model URL".
# Only turn this on for local development, where the database really is on localhost.
ALLOW_PRIVATE_HOSTS = _bool("ALLOW_PRIVATE_HOSTS", False)

# ---------- Access and rate limiting ----------
# An optional shared passphrase in front of the whole app, for a private deployment. Unset
# means no gate: every visitor brings their own database and model key anyway.
APP_PASSPHRASE = os.getenv("APP_PASSPHRASE") or None
RATE_LIMIT_PER_SESSION_PER_MINUTE = _int("RATE_LIMIT_PER_SESSION_PER_MINUTE", 10)
RATE_LIMIT_GLOBAL_PER_MINUTE = _int("RATE_LIMIT_GLOBAL_PER_MINUTE", 120)
