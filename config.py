import os
from typing import Any

from dotenv import load_dotenv

load_dotenv()

# ---------- Model Configurations ----------
MODEL_CONFIGS = {
    "gpt-4.1-nano": {
        "deployment": os.getenv("DEPLOYMENT1_NAME"),
        "endpoint": os.getenv("AZURE_OPENAI_ENDPOINT1"),
        "model_name": os.getenv("MODEL1_NAME", "gpt-4.1-nano"),
    },
    "gpt-4.1-mini": {
        "deployment": os.getenv("DEPLOYMENT2_NAME"),
        "endpoint": os.getenv("AZURE_OPENAI_ENDPOINT2"),
        "model_name": os.getenv("MODEL2_NAME", "gpt-4.1-mini"),
    },
}

DEFAULT_MODEL = "gpt-4.1-mini"

# The cheaper deployment used for small structured jobs: routing, grounding, table evidence.
# Kept as its own name rather than assumed to be "gpt-4.1-nano" everywhere, since a caller
# needs one fixed answer regardless of which model the person chatting has selected.
GROUNDING_MODEL = "gpt-4.1-nano"

# ---------- Database ----------
DATABASE_URL = os.getenv("DATABASE_URL")

# A separate, least-privilege connection string for everything that only ever reads:
# queries, schema introspection, fingerprinting, value grounding. Falls back to DATABASE_URL
# so a deployment that has not yet created a read-only role still runs, just without the
# database-level guarantee. See docs/READ_ONLY_ROLE.md for how to create the role this is
# meant to point at.
DATABASE_URL_RO = os.getenv("DATABASE_URL_RO") or DATABASE_URL

# ---------- Qdrant ----------
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
# Unset for local dev, where Qdrant runs with no auth at all. Set this to the full
# read-write key when Qdrant itself has QDRANT__SERVICE__API_KEY configured, which the app
# and the indexer both need to keep working the moment that is turned on - a separate,
# read-only key is what a person browsing the dashboard directly should use instead, never
# this one.
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY") or None
# One collection per kind of thing being searched: a table's own definition, and a real
# value one of its columns holds. Kept apart because a question is answered by finding both
# at once through different searches, not by ranking them against each other in one list.
QDRANT_COLLECTION_TABLES = os.getenv("QDRANT_COLLECTION_TABLES", "schema_tables")
QDRANT_COLLECTION_VALUES = os.getenv("QDRANT_COLLECTION_VALUES", "schema_values")

# ---------- Embedding ----------
# An Azure OpenAI embedding deployment, usually its own resource rather than the chat
# deployments above, so it is configured on its own rather than assumed to share their key
# or endpoint.
AZURE_EMBEDDING_KEY = os.getenv("AZURE_EMBEDDING_KEY")
AZURE_EMBEDDING_ENDPOINT = os.getenv("AZURE_EMBEDDING_ENDPOINT")
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-small")
EMBED_DIM = int(os.getenv("EMBED_DIM", "1536"))

# ---------- Indexing ----------
# Read only by the terminal CLI, which is the one entry point that can index as part of its
# own startup. The web app never does: the index is built by the indexer service (or the
# sidebar's "Rebuild index" button), never as a side effect of a browser tab loading the page.
AUTO_INDEX_ON_STARTUP = os.getenv("AUTO_INDEX_ON_STARTUP", "true").lower() == "true"

# ---------- Auth and rate limiting ----------
# A single shared passphrase gate, meant for a small evaluation audience rather than a
# multi-user product. Leaving this unset disables the gate, which is only appropriate for a
# deployment nobody but its owner can reach.
APP_PASSPHRASE = os.getenv("APP_PASSPHRASE") or None
RATE_LIMIT_PER_SESSION_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_SESSION_PER_MINUTE", "10"))
RATE_LIMIT_GLOBAL_PER_MINUTE = int(os.getenv("RATE_LIMIT_GLOBAL_PER_MINUTE", "60"))

# ---------- Azure OpenAI (shared) ----------
AZURE_OPENAI_KEY = os.getenv("AZURE_OPENAI_KEY")
AZURE_API_VERSION = "2024-10-21"


def get_model_config(model_name: str) -> dict:
    """Configuration for a specific model, or the default if the name is not recognised."""
    return MODEL_CONFIGS.get(model_name, MODEL_CONFIGS[DEFAULT_MODEL])


_azure_clients: dict[str, Any] = {}


def create_azure_client(model_name: str):
    """The Azure OpenAI client for the given model, one per deployment for the life of the process.

    A client holds its own connection pool, so building a fresh one on every call meant every
    model call paid for a new TLS handshake it did not need; deployments are fixed for the
    life of the process, so nothing about reusing one across calls can go stale.
    """
    from openai import AzureOpenAI

    cfg = get_model_config(model_name)
    client = _azure_clients.get(cfg["endpoint"])
    if client is None:
        client = AzureOpenAI(
            api_key=AZURE_OPENAI_KEY,
            api_version=AZURE_API_VERSION,
            azure_endpoint=cfg["endpoint"],
        )
        _azure_clients[cfg["endpoint"]] = client
    return client


def create_embedding_client():
    """The Azure OpenAI client for the embedding deployment, built once and reused.

    A separate cache slot from create_azure_client's, since this deployment usually lives on
    its own resource with its own key rather than sharing either with the chat deployments.
    """
    from openai import AzureOpenAI

    client = _azure_clients.get(AZURE_EMBEDDING_ENDPOINT)
    if client is None:
        client = AzureOpenAI(
            api_key=AZURE_EMBEDDING_KEY,
            api_version=AZURE_API_VERSION,
            azure_endpoint=AZURE_EMBEDDING_ENDPOINT,
        )
        _azure_clients[AZURE_EMBEDDING_ENDPOINT] = client
    return client


def validate_config() -> tuple[bool, list[str]]:
    """Check that every deployment the app actually uses is configured.

    Both models are checked, not only the default one: the cheaper deployment is used for
    routing and grounding on every question regardless of which model is selected for the
    visible answer, so a gap there breaks the app just as surely as a gap in the default.
    """
    missing = []
    if not AZURE_OPENAI_KEY:
        missing.append("AZURE_OPENAI_KEY")
    if not DATABASE_URL:
        missing.append("DATABASE_URL")
    if not AZURE_EMBEDDING_KEY:
        missing.append("AZURE_EMBEDDING_KEY")
    if not AZURE_EMBEDDING_ENDPOINT:
        missing.append("AZURE_EMBEDDING_ENDPOINT")

    env_names = {
        "gpt-4.1-nano": ("DEPLOYMENT1_NAME", "AZURE_OPENAI_ENDPOINT1"),
        "gpt-4.1-mini": ("DEPLOYMENT2_NAME", "AZURE_OPENAI_ENDPOINT2"),
    }
    for model_name, (deployment_var, endpoint_var) in env_names.items():
        cfg = MODEL_CONFIGS[model_name]
        if not cfg["deployment"]:
            missing.append(deployment_var)
        if not cfg["endpoint"]:
            missing.append(endpoint_var)

    return len(missing) == 0, missing
