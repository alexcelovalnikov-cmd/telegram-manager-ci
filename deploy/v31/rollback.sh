#!/usr/bin/env bash
set -euo pipefail

current_compose="${1:?Usage: rollback.sh CURRENT_COMPOSE PREVIOUS_COMPOSE}"
previous_compose="${2:?Usage: rollback.sh CURRENT_COMPOSE PREVIOUS_COMPOSE}"

has_service() {
  local compose_file="$1"
  local service="$2"
  docker compose -f "$compose_file" config --services | grep -Fxq "$service"
}

docker compose -f "$current_compose" config -q
docker compose -f "$previous_compose" config -q

echo "Stopping current release"
docker compose -f "$current_compose" stop api || true
if has_service "$current_compose" calendar; then
  docker compose -f "$current_compose" stop calendar || true
fi

echo "Starting previous release"
if has_service "$previous_compose" calendar; then
  docker compose -f "$previous_compose" start calendar
fi
docker compose -f "$previous_compose" start api

if command -v curl >/dev/null 2>&1; then
  api_addr="$(docker compose -f "$previous_compose" port api 8000 | tail -n 1)"
  expected="${TM_EXPECT_ROLLBACK_API_HTTP:-401}"
  code="000"
  for _ in {1..20}; do
    code="$(curl -sS -o /dev/null -w '%{http_code}' "http://$api_addr/" || true)"
    [[ "$code" == "$expected" ]] && break
    sleep 1
  done
  if [[ "$code" != "$expected" ]]; then
    echo "ERROR: rollback API HTTP status $code, expected $expected" >&2
    exit 1
  fi
  echo "rollback_api_http=$code"
fi

echo "ROLLBACK_OK"
