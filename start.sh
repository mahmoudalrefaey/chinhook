#!/bin/sh
# Starts Ollama first since the app connects to it at import time, waits until it actually
# answers, confirms the embedding model is present, then starts the app.
set -e

ollama serve &

echo "Waiting for Ollama..."
attempt=0
until curl -s http://127.0.0.1:11434/api/version > /dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 60 ]; then
        # An unbounded wait here used to mean a dead or wedged Ollama hung the whole
        # container forever, with the only sign of it being this same line repeated in the
        # logs and the platform's own deploy timeout eventually giving up with no real
        # explanation. Sixty seconds is generous for a process that starts in well under one.
        echo "Ollama did not come up within 60 seconds. Exiting so the platform can restart the container." >&2
        exit 1
    fi
    sleep 1
done
echo "Ollama is up."

if ! ollama list | grep -q nomic-embed-text; then
    # The image already has this model baked in at build time; reaching here means something
    # unexpected, such as a volume that reset /root/.ollama. Pulled again as a fallback rather
    # than failing outright, but this should not be the common path.
    echo "nomic-embed-text is not present, pulling it now..."
    ollama pull nomic-embed-text
fi

# The project's own virtual environment, built by `uv sync --frozen` at image build time, run
# directly rather than through `uv run --with streamlit`. That flag layered an unpinned
# requirement on top of the project environment, resolved against the package index at
# container start rather than against uv.lock, so the Streamlit version actually running
# could silently drift from the one the image was built and tested with, and needed network
# access just to start.
exec /app/.venv/bin/streamlit run app.py \
    --server.port "${PORT:-8501}" \
    --server.address 0.0.0.0 \
    --server.headless true
