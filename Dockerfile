# Runs the web interface together with Ollama in one container, since config.py points at
# Ollama on localhost by default and it is the one service address the app cannot be told to
# reach elsewhere without setting OLLAMA_HOST. Everything else (Postgres, Qdrant) is expected
# to be reached over the network as separate services.

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates zstd tini \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://ollama.com/install.sh | sh
RUN curl -fsSL https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies before source, so an ordinary code change does not invalidate the layer that
# installs everything: only a change to these two files does.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

COPY . .

# Baked into the image rather than pulled the first time the container starts, so a cold
# start is not also the moment the embedding model, close to 300 MB, is downloaded fresh.
# Ollama's own server has to be running for `ollama pull` to talk to, so it is started here,
# used, and left behind: only the files it wrote under /root/.ollama survive into this layer.
RUN ollama serve & \
    for i in $(seq 1 30); do \
        curl -fs http://127.0.0.1:11434/api/version > /dev/null 2>&1 && break; \
        sleep 1; \
    done && \
    ollama pull nomic-embed-text

COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

# The default start.sh binds to, when $PORT is not set by the platform running this image.
# EXPOSE is image metadata only: Railway (and any platform that injects $PORT) routes to
# whatever port the process actually listens on regardless of this line, and Docker cannot
# make a build-time declaration track a value only known when the container is started, so
# this stays accurate for the common case rather than attempting something Docker has no way
# to express.
EXPOSE 8501

# tini as PID 1 rather than the shell directly: it forwards signals to both of this
# container's processes and reaps anything left orphaned, neither of which a plain shell
# does, and neither of which "docker stop" or a crash-restart can be relied on to get right
# without it.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/start.sh"]
