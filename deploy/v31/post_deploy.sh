#!/usr/bin/env bash
set -euo pipefail

compose_file="${1:-$(cd "$(dirname "$0")" && pwd)/compose.production.yaml}"

docker compose -f "$compose_file" config -q

wait_healthy() {
  local service="$1"
  local cid status running
  cid="$(docker compose -f "$compose_file" ps -q "$service")"
  if [[ -z "$cid" ]]; then
    echo "ERROR: service $service has no container" >&2
    return 1
  fi

  for _ in {1..40}; do
    running="$(docker inspect --format '{{.State.Running}}' "$cid")"
    status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$cid")"
    if [[ "$running" != "true" ]]; then
      echo "ERROR: service $service is not running" >&2
      docker logs --tail=80 "$cid" >&2 || true
      return 1
    fi
    if [[ "$status" == "healthy" ]]; then
      echo "$service=healthy"
      return 0
    fi
    if [[ "$status" == "unhealthy" ]]; then
      echo "ERROR: service $service is unhealthy" >&2
      docker inspect --format '{{json .State.Health.Log}}' "$cid" >&2 || true
      return 1
    fi
    sleep 1
  done

  echo "ERROR: service $service did not become healthy in time" >&2
  docker inspect "$cid" >&2 || true
  return 1
}

wait_healthy api
wait_healthy calendar

docker compose -f "$compose_file" exec -T api python -m tm_api.healthcheck
docker compose -f "$compose_file" exec -T calendar python -m tm_calendar.healthcheck
docker compose -f "$compose_file" exec -T api python -m tm_api.v25.preflight

if command -v curl >/dev/null 2>&1; then
  api_addr="$(docker compose -f "$compose_file" port api 8000 | tail -n 1)"
  calendar_addr="$(docker compose -f "$compose_file" port calendar 5232 | tail -n 1)"

  api_code="$(curl -sS -o /dev/null -w '%{http_code}' "http://$api_addr/")"
  calendar_code="$(curl -sS -o /dev/null -w '%{http_code}' "http://$calendar_addr/")"

  if [[ "$api_code" != "${TM_EXPECT_API_HTTP:-401}" ]]; then
    echo "ERROR: API HTTP status $api_code, expected ${TM_EXPECT_API_HTTP:-401}" >&2
    exit 1
  fi
  if [[ "$calendar_code" != "${TM_EXPECT_CALENDAR_HTTP:-302}" ]]; then
    echo "ERROR: Calendar HTTP status $calendar_code, expected ${TM_EXPECT_CALENDAR_HTTP:-302}" >&2
    exit 1
  fi

  echo "api_http=$api_code"
  echo "calendar_http=$calendar_code"
fi

echo "POST_DEPLOY_OK"

# Production-only: keep the restricted controller in sync with the successfully
# verified release. Shadow validation must never change host control files.
if [[ "${TM_PROJECT_NAME:-}" == *-production ]]; then
  repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
  release_name="$(basename "$repo_root")"
  worker_was_running=false
  if docker inspect telegram-manager-worker --format '{{.State.Running}}' 2>/dev/null | grep -qx true; then
    worker_was_running=true
  fi
  whatsapp_was_running=false
  if docker inspect telegram-manager-whatsapp --format '{{.State.Running}}' 2>/dev/null | grep -qx true; then
    whatsapp_was_running=true
  fi

  # V49+ server runtime: build the Telegram/media worker from the exact
  # immutable release tree. The worker is staged separately and is never
  # started during first cutover until the Telegram session has been copied
  # after the Mac collector is stopped.
  if [[ "$release_name" =~ ^V[0-9]+$ && -f "$repo_root/Dockerfile.worker" ]]; then
    worker_image="telegram-manager-worker:$release_name"
    echo "== Build server worker image $worker_image =="
    docker build -f "$repo_root/Dockerfile.worker" -t "$worker_image" "$repo_root"

    python3 - /etc/telegram-manager/tmctl.env "$worker_image" <<'PYCFG'
from pathlib import Path
import os,sys
path=Path(sys.argv[1]); value=sys.argv[2]
lines=path.read_text().splitlines(); out=[]; seen=False
for line in lines:
    if line.startswith('TM_WORKER_IMAGE='):
        out.append('TM_WORKER_IMAGE='+value); seen=True
    else:
        out.append(line)
if not seen: out.append('TM_WORKER_IMAGE='+value)
if not any(line.startswith('TM_WORKER_ENABLED=') for line in out):
    out.append('TM_WORKER_ENABLED=false')
tmp=path.with_suffix('.tmp'); tmp.write_text('\n'.join(out)+'\n'); os.chmod(tmp,0o600); tmp.replace(path)
PYCFG
  fi

  if [[ "$release_name" =~ ^V[0-9]+$ && -f "$repo_root/Dockerfile.whatsapp" ]]; then
    whatsapp_image="telegram-manager-whatsapp:$release_name"
    echo "== Build self-hosted WhatsApp image $whatsapp_image =="
    docker build -f "$repo_root/Dockerfile.whatsapp" -t "$whatsapp_image" "$repo_root"
    python3 - /etc/telegram-manager/tmctl.env "$whatsapp_image" <<'PYWA'
from pathlib import Path
import os,sys
path=Path(sys.argv[1]); value=sys.argv[2]
lines=path.read_text().splitlines(); out=[]; seen=False
for line in lines:
    if line.startswith("TM_WHATSAPP_IMAGE="):
        out.append("TM_WHATSAPP_IMAGE="+value); seen=True
    else:
        out.append(line)
if not seen: out.append("TM_WHATSAPP_IMAGE="+value)
if not any(line.startswith("TM_WHATSAPP_ENABLED=") for line in out):
    out.append("TM_WHATSAPP_ENABLED=false")
tmp=path.with_suffix(".tmp"); tmp.write_text("\n".join(out)+"\n"); os.chmod(tmp,0o600); tmp.replace(path)
PYWA
  fi

  bash "$repo_root/deploy/ops/install_tmctl.sh"
  if command -v serverctl >/dev/null 2>&1; then
    echo "== Preview project adapter update through Server Admin Controller =="
    serverctl project-adapter-install-preview telegram-manager deploy tmctl-root --with-wrapper || \
      echo "WARNING: Server Admin Controller adapter preview unavailable; staged files were left untouched" >&2
  fi

  # After the initial migration, future releases automatically roll the
  # server worker to the image built from the same verified tag.
  if [[ "$worker_was_running" == true ]] || grep -qx 'TM_WORKER_ENABLED=true' /etc/telegram-manager/tmctl.env 2>/dev/null; then
    python3 - /etc/telegram-manager/tmctl.env <<'PYWORKER'
from pathlib import Path
import os,sys
path=Path(sys.argv[1]); lines=path.read_text().splitlines(); out=[]; seen=False
for line in lines:
    if line.startswith('TM_WORKER_ENABLED='):
        out.append('TM_WORKER_ENABLED=true'); seen=True
    else:
        out.append(line)
if not seen: out.append('TM_WORKER_ENABLED=true')
tmp=path.with_suffix('.tmp'); tmp.write_text('\n'.join(out)+'\n'); os.chmod(tmp,0o600); tmp.replace(path)
PYWORKER
    /usr/local/sbin/tmctl-root worker-start
  fi
  if [[ "$whatsapp_was_running" == true ]] || grep -qx 'TM_WHATSAPP_ENABLED=true' /etc/telegram-manager/tmctl.env 2>/dev/null; then
    if ! /usr/local/sbin/tmctl-root whatsapp-start; then
      echo "WARNING: optional WhatsApp source did not reach QR/connected state; release remains valid" >&2
    fi
  fi
fi
