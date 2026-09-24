import os
from dotenv import load_dotenv

load_dotenv()

# ---------- Model Configurations ----------
MODEL_CONFIGS = {
    "gpt-4.1-nano": {
        "deployment": os.getenv("DEPLOYMENT1_NAME"),
        "endpoint": os.getenv("AZURE_OPENAI_ENDPOINT1"),
        "model_name": "gpt-4.1-nano",
    },
    "gpt-4.1-mini": {
        "deployment": os.getenv("DEPLOYMENT2_NAME"),
        "endpoint": os.getenv("AZURE_OPENAI_ENDPOINT2"),
        "model_name": "gpt-4.1-mini",
    },
}

DEFAULT_MODEL = "gpt-4.1-mini"

# ---------- Database ----------
DATABASE_URL = os.getenv("DATABASE_URL")

# ---------- Qdrant ----------
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "schema_tables")

# ---------- Embedding ----------
EMBED_MODEL = "nomic-embed-text"
EMBED_DIM = 768
OLLAMA_HOST = "http://127.0.0.1:11434"

# ---------- Indexing ----------
INDEX_CHECK_INTERVAL = int(os.getenv("INDEX_CHECK_INTERVAL", "300"))
AUTO_INDEX_ON_STARTUP = os.getenv("AUTO_INDEX_ON_STARTUP", "true").lower() == "true"

# ---------- Azure OpenAI (shared) ----------
AZURE_OPENAI_KEY = os.getenv("AZURE_OPENAI_KEY")
AZURE_API_VERSION = "2024-10-21"


def get_model_config(model_name: str) -> dict:
    """Get configuration for a specific model."""
    return MODEL_CONFIGS.get(model_name, MODEL_CONFIGS[DEFAULT_MODEL])


def create_azure_client(model_name: str):
    """Create Azure OpenAI client for the given model."""
    from openai import AzureOpenAI

    cfg = get_model_config(model_name)
    return AzureOpenAI(
        api_key=AZURE_OPENAI_KEY,
        api_version=AZURE_API_VERSION,
        azure_endpoint=cfg["endpoint"],
    )


def validate_config() -> tuple[bool, list[str]]:
    """Validate all required configuration is present."""
    missing = []
    if not AZURE_OPENAI_KEY:
        missing.append("AZURE_OPENAI_KEY")
    if not DATABASE_URL:
        missing.append("DATABASE_URL")
    if not MODEL_CONFIGS[DEFAULT_MODEL]["deployment"]:
        missing.append("DEPLOYMENT1_NAME")
    if not MODEL_CONFIGS[DEFAULT_MODEL]["endpoint"]:
        missing.append("AZURE_OPENAI_ENDPOINT1")
    return len(missing) == 0, missing