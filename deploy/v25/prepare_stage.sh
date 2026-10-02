#!/bin/bash
# Creates ONLY an isolated test deployment. It never selects the production DB.
set -euo pipefail
if [[ $# -ne 1 || ! -f "$1" ]]; then
  printf '%s\n' 'Usage: bash deploy/v25/prepare_stage.sh /absolute/path/Telegram_Manager_Schema_V22.sql.gz' >&2
  exit 2
fi
schema=$(realpath "$1")
root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$root"
if [[ ! -d .staging ]]; then python3 deploy/v25/create_stage_config.py; fi
compose=(docker compose -f deploy/v25/compose.stage.yaml)
"${compose[@]}" up -d db
ready=0
for attempt in {1..30}; do
  if "${compose[@]}" exec -T db pg_isready -U postgres -d tm_v24_test >/dev/null 2>&1; then ready=1;break;fi
  sleep 2
done
[[ "$ready" = 1 ]] || { echo 'Staging DB did not become ready' >&2;exit 1; }
name=$("${compose[@]}" exec -T db psql -U postgres -d tm_v24_test -Atc 'SELECT current_database()')
[[ "$name" = tm_v24_test ]] || { echo 'Non-test DB refused' >&2;exit 1; }
count=$("${compose[@]}" exec -T db psql -U postgres -d tm_v24_test -Atc "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('public','tm_v24','tm_config','tm_calendar') AND c.relkind IN ('r','v','m')")
[[ "$count" = 0 ]] || { echo 'Test DB is not empty. Stopped without overwriting or deleting anything.' >&2;exit 1; }
psql=("${compose[@]}" exec -T db psql -X -U postgres -d tm_v24_test -v ON_ERROR_STOP=1)
"${psql[@]}" < deploy/v25/000_stage_roles.sql
# Fresh PostgreSQL already has an empty public schema; the V22 schema-only dump recreates it.
"${psql[@]}" -c 'DROP SCHEMA public;'
case "$schema" in
  *.sql.gz) gzip -t "$schema";gzip -dc "$schema" | "${psql[@]}" ;;
  *.sql) "${psql[@]}" < "$schema" ;;
  *) echo 'Expected schema-only .sql or .sql.gz' >&2;exit 2 ;;
esac
for migration in deploy/v24/001_schema.sql deploy/v24/002_roles.sql deploy/v24/003_search_indexes.sql deploy/v25/001_configuration.sql deploy/v25/002_permissions.sql deploy/v25/003_internal_rls.sql .staging/logins.sql; do
  "${psql[@]}" < "$migration"
done
printf '%s\n' 'Isolated tm_v24_test initialized with V25 migrations. No production data copied.'
printf '%s\n' 'Next:
  docker compose -f deploy/v25/compose.stage.yaml build api calendar
  docker compose -f deploy/v25/compose.stage.yaml --profile tests build tests'
