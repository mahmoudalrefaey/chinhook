# The web app. Qdrant is a separate service reached by QDRANT_URL (see README.md), and each
# visitor's database and model are reached by whatever they enter on the setup screen, so
# nothing about this image is specific to any of them.
#
# By default the image holds no embedding model: Qdrant Cloud Inference embeds the text, and
# the app fits in 512 MB. For a Qdrant without Cloud Inference, build it with the model inside:
#
#   docker build --build-arg EXTRAS=local-embeddings .     (and run with EMBED_PROVIDER=fastembed)

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
ARG EXTRAS=""
RUN uv sync --frozen --no-dev ${EXTRAS:+--extra "$EXTRAS"}

# With local-embeddings, the model runs in-process. Downloading it here, at build time, means a
# fresh container answers its first question without first fetching it from the internet.
# This is EMBED_MODEL's fastembed default; a different EMBED_MODEL set at run time still works,
# and that model is fetched on first use instead.
ARG LOCAL_EMBED_MODEL=BAAI/bge-small-en-v1.5
RUN if [ -n "$EXTRAS" ]; then \
        /app/.venv/bin/python -c "import os; from fastembed import TextEmbedding; TextEmbedding('${LOCAL_EMBED_MODEL}', cache_dir=os.environ['FASTEMBED_CACHE_PATH'])"; \
    fi

COPY . .

RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

# The default this binds to when $PORT is not set by the platform running this image. EXPOSE
# is image metadata only: a platform that injects $PORT routes to whatever port the process
# actually listens on, regardless of this line.
EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT:-8501}/healthz" || exit 1

# tini as PID 1 rather than the shell directly: it forwards signals and reaps anything left
# orphaned, neither of which a plain shell does.
ENTRYPOINT ["/usr/bin/tini", "--"]
# One process on purpose: each visitor's session lives in its memory (chinhook/web/sessions.py).
# --proxy-headers so the client address and scheme are taken from the platform's proxy.
CMD ["sh", "-c", "exec /app/.venv/bin/uvicorn chinhook.web.server:app --host 0.0.0.0 --port \"${PORT:-8501}\" --proxy-headers --forwarded-allow-ips='*'"]
