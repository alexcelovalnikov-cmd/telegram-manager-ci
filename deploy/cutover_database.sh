#!/bin/bash
set -euo pipefail
umask 077
if [[ -e /opt/telegram-manager/state/postgres-cutover.complete || ! -f /opt/telegram-manager/secrets/migration.env ]]; then
  echo "Migration already completed or export credential missing; no services stopped" >&2
  exit 1
fi
backup=/opt/telegram-manager/backups
release=/opt/telegram-manager/releases/V20
source_db=(docker run --rm -i --env-file /opt/telegram-manager/secrets/migration.env -v /opt/telegram-manager/secrets/source-ca.crt:/etc/ssl/certs/ca-certificates.crt:ro -v "$backup:/backup" postgres:17.11-alpine)
docker stop telegram-manager-api-1 telegram-manager-v20-stage telegram-manager-db-rest-1 >/dev/null
tar -czf "$backup/api-before-v20.tgz" -C /opt/telegram-manager releases/V19 secrets/api.env state
"${source_db[@]}" psql -X -qAt -v ON_ERROR_STOP=1 < "$release/deploy/check_migration.sql" > "$backup/source-before.txt"
"${source_db[@]}" pg_dump -Fc --no-owner --no-acl --lock-wait-timeout=10s --schema=public --schema=tm_api_private --schema=supabase_migrations -f /backup/source-cutover.dump
"${source_db[@]}" psql -X -qAt -v ON_ERROR_STOP=1 < "$release/deploy/check_migration.sql" > "$backup/source-after.txt"
cmp "$backup/source-before.txt" "$backup/source-after.txt"
docker exec -i telegram-manager-db-1 pg_restore -U postgres -d telegram_manager --clean --if-exists --no-owner --no-acl --exit-on-error --single-transaction < "$backup/source-cutover.dump"
docker exec -i telegram-manager-db-1 psql -U postgres -d telegram_manager -v ON_ERROR_STOP=1 -q < "$release/deploy/database-permissions.sql"
docker exec -i telegram-manager-db-1 psql -X -U postgres -d telegram_manager -qAt -v ON_ERROR_STOP=1 < "$release/deploy/check_migration.sql" > "$backup/destination.txt"
cmp "$backup/source-after.txt" "$backup/destination.txt"
echo 'All relation, function, view and constraint fingerprints match'
docker start telegram-manager-db-rest-1 >/dev/null
