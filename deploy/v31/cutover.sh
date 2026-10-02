#!/usr/bin/env bash
set -euo pipefail

previous_compose="${1:?Usage: cutover.sh PREVIOUS_COMPOSE NEXT_COMPOSE}"
next_compose="${2:?Usage: cutover.sh PREVIOUS_COMPOSE NEXT_COMPOSE}"
script_dir="$(cd "$(dirname "$0")" && pwd)"

docker compose -f "$previous_compose" config -q
docker compose -f "$next_compose" config -q

if [[ "${TM_CUTOVER_BACKUP_OK:-}" != "YES" ]]; then
  echo "ERROR: set TM_CUTOVER_BACKUP_OK=YES only after prepare_cutover.sh reports CUTOVER_BACKUP_OK" >&2
  exit 1
fi

cutover_dir="${TM_CUTOVER_DIR:-}"
if [[ -z "$cutover_dir" ]]; then
  echo "ERROR: set TM_CUTOVER_DIR to the directory printed by prepare_cutover.sh" >&2
  exit 1
fi
for required in database.dump recovery.tar.gz state.txt compose.sha256; do
  if [[ ! -s "$cutover_dir/$required" ]]; then
    echo "ERROR: missing verified cutover artifact: $cutover_dir/$required" >&2
    exit 1
  fi
done

has_service() {
  local compose_file="$1"
  local service="$2"
  docker compose -f "$compose_file" config --services | grep -Fxq "$service"
}

previous_api="$(docker compose -f "$previous_compose" ps -q --all api || true)"
previous_calendar=""
if has_service "$previous_compose" calendar; then
  previous_calendar="$(docker compose -f "$previous_compose" ps -q --all calendar || true)"
fi

if [[ -z "$previous_api" ]]; then
  echo "ERROR: previous API container not found" >&2
  exit 1
fi

rollback() {
  local rc=$?
  trap - ERR
  echo "CUTOVER_FAILED rc=$rc; restoring previous release" >&2

  docker compose -f "$next_compose" stop api >/dev/null 2>&1 || true
  if has_service "$next_compose" calendar; then
    docker compose -f "$next_compose" stop calendar >/dev/null 2>&1 || true
  fi
  docker compose -f "$previous_compose" start api >/dev/null 2>&1 || true
  if [[ -n "$previous_calendar" ]]; then
    docker compose -f "$previous_compose" start calendar >/dev/null 2>&1 || true
  fi

  echo "ROLLBACK_ATTEMPTED" >&2
  exit "$rc"
}
trap rollback ERR

echo "Stopping previous production writers"
docker compose -f "$previous_compose" stop api
if [[ -n "$previous_calendar" ]]; then
  docker compose -f "$previous_compose" stop calendar
fi

echo "Starting next production release"
if has_service "$next_compose" calendar; then
  docker compose -f "$next_compose" up -d calendar
fi
docker compose -f "$next_compose" up -d api

bash "$script_dir/post_deploy.sh" "$next_compose"

trap - ERR
echo "CUTOVER_OK"
