# The web app. Qdrant is a separate service reached by QDRANT_URL (see README.md), and each
# visitor's database and model are reached by whatever they enter on the setup screen, so
# nothing about this image is specific to any of them.

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"
ENV PYTHONUNBUFFERED=1 \
    FASTEMBED_CACHE_PATH=/app/.cache/fastembed \
    HF_HUB_DISABLE_TELEMETRY=1

WORKDIR /app

# Dependencies before source, so an ordinary code change does not invalidate the layer that
# installs everything: only a change to these two files does.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# The embedding model runs in-process. Downloading it here, at build time, means a fresh
# container answers its first question without first fetching the model from the internet.
# Setting a different EMBED_MODEL at run time still works; that model is fetched on first use.
ARG EMBED_MODEL=BAAI/bge-small-en-v1.5
ENV EMBED_MODEL=${EMBED_MODEL}
RUN /app/.venv/bin/python -c "import os; from fastembed import TextEmbedding; TextEmbedding(os.environ['EMBED_MODEL'], cache_dir=os.environ['FASTEMBED_CACHE_PATH'])"

COPY . .

RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

# The default this binds to when $PORT is not set by the platform running this image. EXPOSE
# is image metadata only: Railway (and any platform that injects $PORT) routes to whatever
# port the process actually listens on regardless of this line.
EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT:-8501}/_stcore/health" || exit 1

# tini as PID 1 rather than the shell directly: it forwards signals and reaps anything left
# orphaned, neither of which a plain shell does.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "exec /app/.venv/bin/streamlit run app.py --server.port \"${PORT:-8501}\" --server.address 0.0.0.0 --server.headless true"]
