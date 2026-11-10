#!/usr/bin/env bash
# Import ap_inbox_candidate.sql into the Compose Postgres service.
# Usage (from repo root):
#   bash scripts/import_dump.sh
# Or via Compose:
#   docker compose exec -T postgres bash < scripts/import_dump.sh  (not preferred)
# Preferred host invocation waits for healthy postgres then pipes a sanitized dump.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DUMP="${DUMP_PATH:-$ROOT_DIR/ap_inbox_candidate.sql}"
COMPOSE_SERVICE="${COMPOSE_SERVICE:-postgres}"
DB_NAME="${POSTGRES_DB:-ap_inbox}"
DB_USER="${POSTGRES_USER:-ap}"

if [[ ! -f "$DUMP" ]]; then
  echo "ERROR: dump not found at $DUMP" >&2
  exit 1
fi

echo "Waiting for Postgres ($COMPOSE_SERVICE) to be healthy..."
for i in $(seq 1 60); do
  if docker compose exec -T "$COMPOSE_SERVICE" pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1; then
    break
  fi
  if [[ "$i" -eq 60 ]]; then
    echo "ERROR: Postgres did not become ready in time" >&2
    exit 1
  fi
  sleep 2
done

echo "Sanitizing \\restrict / \\unrestrict and importing $DUMP ..."
# pg_dump 15.19 emits \restrict/\unrestrict; older clients reject them.
# Strip those meta-commands, then import with ON_ERROR_STOP.
sed -e '/^\\restrict /d' -e '/^\\unrestrict /d' "$DUMP" \
  | docker compose exec -T "$COMPOSE_SERVICE" \
      psql -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME"

echo "Verifying frontapp.frontapp_messages count..."
docker compose exec -T "$COMPOSE_SERVICE" \
  psql -U "$DB_USER" -d "$DB_NAME" -c \
  "SELECT count(*) AS message_count FROM frontapp.frontapp_messages;"

echo "Import complete."
