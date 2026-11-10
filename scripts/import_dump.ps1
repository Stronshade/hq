# Import ap_inbox_candidate.sql into Compose Postgres (Windows-friendly).
# Usage from repo root:
#   powershell -ExecutionPolicy Bypass -File scripts/import_dump.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Dump = if ($env:DUMP_PATH) { $env:DUMP_PATH } else { Join-Path $Root "ap_inbox_candidate.sql" }
$Service = if ($env:COMPOSE_SERVICE) { $env:COMPOSE_SERVICE } else { "postgres" }
$DbName = if ($env:POSTGRES_DB) { $env:POSTGRES_DB } else { "ap_inbox" }
$DbUser = if ($env:POSTGRES_USER) { $env:POSTGRES_USER } else { "ap" }

if (-not (Test-Path $Dump)) {
  throw "Dump not found at $Dump"
}

Push-Location $Root
try {
  Write-Host "Waiting for Postgres ($Service) to be healthy..."
  $ready = $false
  for ($i = 1; $i -le 60; $i++) {
    docker compose exec -T $Service pg_isready -U $DbUser -d $DbName 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    Start-Sleep -Seconds 2
  }
  if (-not $ready) { throw "Postgres did not become ready in time" }

  $sanitized = Join-Path $env:TEMP "ap_inbox_candidate.sanitized.sql"
  Write-Host "Sanitizing \restrict / \unrestrict -> $sanitized"
  Get-Content -Path $Dump -ReadCount 1000 |
    ForEach-Object {
      $_ | Where-Object { $_ -notmatch '^\\restrict ' -and $_ -notmatch '^\\unrestrict ' }
    } |
    Set-Content -Path $sanitized -Encoding utf8

  Write-Host "Importing sanitized dump..."
  Get-Content -Path $sanitized -Raw |
    docker compose exec -T $Service psql -v ON_ERROR_STOP=1 -U $DbUser -d $DbName
  if ($LASTEXITCODE -ne 0) { throw "psql import failed with exit $LASTEXITCODE" }

  Write-Host "Verifying frontapp.frontapp_messages count..."
  docker compose exec -T $Service psql -U $DbUser -d $DbName -c `
    "SELECT count(*) AS message_count FROM frontapp.frontapp_messages;"

  Write-Host "Import complete."
}
finally {
  Pop-Location
}
