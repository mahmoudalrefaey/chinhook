# The web interface only. Qdrant and Postgres are separate services (see docker-compose.yml),
# reached over the network by URL, the same way a hosted deployment reaches them; nothing
# about this image is specific to either of them. Embeddings come from an Azure OpenAI
# deployment, so there is no local embedding model or service to bring up here either.

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies before source, so an ordinary code change does not invalidate the layer that
# installs everything: only a change to these two files does.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

COPY . .

RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

# The default this binds to when $PORT is not set by the platform running this image. EXPOSE
# is image metadata only: Railway (and any platform that injects $PORT) routes to whatever
# port the process actually listens on regardless of this line, and Docker cannot make a
# build-time declaration track a value only known when the container is started, so this
# stays accurate for the common case rather than attempting something Docker has no way to
# express.
EXPOSE 8501

# tini as PID 1 rather than the shell directly: it forwards signals and reaps anything left
# orphaned, neither of which a plain shell does, and neither of which "docker stop" or a
# crash-restart can be relied on to get right without it.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "exec /app/.venv/bin/streamlit run app.py --server.port \"${PORT:-8501}\" --server.address 0.0.0.0 --server.headless true"]
