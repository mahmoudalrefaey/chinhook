# Runs the web interface together with Ollama in one container, since config.py points at
# Ollama on localhost and does not read that address from the environment. Everything else
# (Postgres, Qdrant) is expected to be reached over the network as separate services.

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates zstd \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://ollama.com/install.sh | sh
RUN curl -fsSL https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /app
COPY . .

RUN uv sync --frozen

COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

EXPOSE 8501
CMD ["/app/start.sh"]
