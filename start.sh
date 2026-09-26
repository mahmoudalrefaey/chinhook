#!/bin/sh
# Starts Ollama first since the app connects to it at import time, waits until it actually
# answers, pulls the embedding model if it is not already present, then starts the app.
set -e

ollama serve &

echo "Waiting for Ollama..."
until curl -s http://127.0.0.1:11434/api/version > /dev/null 2>&1; do
    sleep 1
done
echo "Ollama is up."

if ! ollama list | grep -q nomic-embed-text; then
    echo "Pulling nomic-embed-text..."
    ollama pull nomic-embed-text
fi

exec uv run --with streamlit streamlit run app.py \
    --server.port "${PORT:-8501}" \
    --server.address 0.0.0.0 \
    --server.headless true
