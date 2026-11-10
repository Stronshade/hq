#!/usr/bin/env bash
# Pull chat + embed models into the compose Ollama volume, then pin num_ctx via Modelfile.
set -euo pipefail

OLLAMA_CONTAINER="${OLLAMA_CONTAINER:-hq-ollama-1}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "Waiting for Ollama in ${OLLAMA_CONTAINER}..."
for _ in $(seq 1 60); do
  if docker exec "${OLLAMA_CONTAINER}" ollama list >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

docker exec "${OLLAMA_CONTAINER}" ollama pull nomic-embed-text
docker exec "${OLLAMA_CONTAINER}" ollama pull qwen3:14b

echo "Creating derived tag qwen3-14b-16k (num_ctx=16384)..."
docker cp "${ROOT}/ollama/Modelfile.qwen3-14b-16k" "${OLLAMA_CONTAINER}:/tmp/Modelfile.qwen3-14b-16k"
docker exec "${OLLAMA_CONTAINER}" ollama create qwen3-14b-16k -f /tmp/Modelfile.qwen3-14b-16k

docker exec "${OLLAMA_CONTAINER}" ollama list
echo "Done."
