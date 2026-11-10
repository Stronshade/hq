# Pull chat + embed models into the compose Ollama volume, then pin num_ctx via Modelfile.
param(
  [string]$OllamaContainer = "hq-ollama-1"
)

$Root = Split-Path -Parent $PSScriptRoot
Write-Host "Waiting for Ollama in $OllamaContainer..."
for ($i = 0; $i -lt 60; $i++) {
  docker exec $OllamaContainer ollama list 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) { break }
  Start-Sleep -Seconds 2
}

docker exec $OllamaContainer ollama pull nomic-embed-text
docker exec $OllamaContainer ollama pull qwen3:14b

Write-Host "Creating derived tag qwen3-14b-16k (num_ctx=16384)..."
docker cp "$Root\ollama\Modelfile.qwen3-14b-16k" "${OllamaContainer}:/tmp/Modelfile.qwen3-14b-16k"
docker exec $OllamaContainer ollama create qwen3-14b-16k -f /tmp/Modelfile.qwen3-14b-16k
docker exec $OllamaContainer ollama list
Write-Host "Done."
