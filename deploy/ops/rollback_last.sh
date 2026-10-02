#!/usr/bin/env bash
set -euo pipefail

CONFIG=/etc/telegram-manager/tmctl.env
PREVIOUS=/opt/telegram-manager/runtime/previous.env
FAILED=/opt/telegram-manager/runtime/failed-release.env

[[ -r "$CONFIG" ]] || { echo "ERROR: missing $CONFIG" >&2; exit 1; }
[[ -r "$PREVIOUS" ]] || { echo "ERROR: no previous release metadata" >&2; exit 1; }

# shellcheck disable=SC1090
source "$CONFIG"
# shellcheck disable=SC1090
source "$PREVIOUS"

required=(
  TM_PREVIOUS_RELEASE_REPO
  TM_PREVIOUS_COMPOSE
  TM_PREVIOUS_API_IMAGE
  TM_PREVIOUS_CALENDAR_IMAGE
  TM_PREVIOUS_PROJECT_NAME
  TM_PREVIOUS_STATE_DIR
  TM_PREVIOUS_API_ENV_FILE
  TM_PREVIOUS_API_DSN_FILE
  TM_PREVIOUS_CALENDAR_ENV_FILE
  TM_PREVIOUS_DATABASE_NETWORK
)
for key in "${required[@]}"; do
  [[ -n "${!key:-}" ]] || { echo "ERROR: missing rollback key $key" >&2; exit 1; }
done

current_release_repo="$TM_RELEASE_REPO"
current_compose="$TM_RELEASE_COMPOSE"
current_api_image="$TM_API_IMAGE"
current_calendar_image="$TM_CALENDAR_IMAGE"
current_project="$TM_PROJECT_NAME"

echo "== Stop current release $current_project =="
TM_PROJECT_NAME="$current_project" docker compose -p "$current_project" -f "$current_compose" stop api calendar

echo "== Start previous release $TM_PREVIOUS_PROJECT_NAME =="
TM_PROJECT_NAME="$TM_PREVIOUS_PROJECT_NAME" TM_API_IMAGE="$TM_PREVIOUS_API_IMAGE" TM_CALENDAR_IMAGE="$TM_PREVIOUS_CALENDAR_IMAGE" TM_API_ENV_FILE="$TM_PREVIOUS_API_ENV_FILE" TM_API_DSN_FILE="$TM_PREVIOUS_API_DSN_FILE" TM_CALENDAR_ENV_FILE="$TM_PREVIOUS_CALENDAR_ENV_FILE" TM_DATABASE_NETWORK="$TM_PREVIOUS_DATABASE_NETWORK" TM_STATE_DIR="$TM_PREVIOUS_STATE_DIR" TM_API_PORT=8766 TM_CALENDAR_PORT=5232 TM_WRITES_ENABLED=true TM_BACKGROUND_ENABLED=true TM_OAUTH_ENABLED=true docker compose -p "$TM_PREVIOUS_PROJECT_NAME" -f "$TM_PREVIOUS_COMPOSE" start calendar api

previous_post="$TM_PREVIOUS_RELEASE_REPO/deploy/v31/post_deploy.sh"
if [[ -x "$previous_post" || -f "$previous_post" ]]; then
  export TM_PROJECT_NAME="$TM_PREVIOUS_PROJECT_NAME"
  export TM_API_IMAGE="$TM_PREVIOUS_API_IMAGE"
  export TM_CALENDAR_IMAGE="$TM_PREVIOUS_CALENDAR_IMAGE"
  export TM_API_ENV_FILE="$TM_PREVIOUS_API_ENV_FILE"
  export TM_API_DSN_FILE="$TM_PREVIOUS_API_DSN_FILE"
  export TM_CALENDAR_ENV_FILE="$TM_PREVIOUS_CALENDAR_ENV_FILE"
  export TM_DATABASE_NETWORK="$TM_PREVIOUS_DATABASE_NETWORK"
  export TM_STATE_DIR="$TM_PREVIOUS_STATE_DIR"
  export TM_API_PORT=8766
  export TM_CALENDAR_PORT=5232
  export TM_WRITES_ENABLED=true
  export TM_BACKGROUND_ENABLED=true
  export TM_OAUTH_ENABLED=true
  bash "$previous_post" "$TM_PREVIOUS_COMPOSE"
else
  code="$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8766/)"
  [[ "$code" == "401" ]] || {
    echo "ERROR: rollback API status $code" >&2
    exit 1
  }
fi

cat > "$FAILED" <<EOF
TM_FAILED_RELEASE_REPO=$current_release_repo
TM_FAILED_COMPOSE=$current_compose
TM_FAILED_API_IMAGE=$current_api_image
TM_FAILED_CALENDAR_IMAGE=$current_calendar_image
TM_FAILED_PROJECT_NAME=$current_project
EOF
chmod 600 "$FAILED"

python3 - "$CONFIG"   "TM_RELEASE_REPO=$TM_PREVIOUS_RELEASE_REPO"   "TM_RELEASE_COMPOSE=$TM_PREVIOUS_COMPOSE"   "TM_PROJECT_NAME=$TM_PREVIOUS_PROJECT_NAME"   "TM_API_IMAGE=$TM_PREVIOUS_API_IMAGE"   "TM_CALENDAR_IMAGE=$TM_PREVIOUS_CALENDAR_IMAGE" <<'PY'
from pathlib import Path
import os
import sys
path=Path(sys.argv[1])
updates=dict(item.split("=",1) for item in sys.argv[2:])
lines=path.read_text().splitlines()
out=[]
seen=set()
for line in lines:
    if "=" in line and not line.lstrip().startswith("#"):
        key=line.split("=",1)[0]
        if key in updates:
            out.append(key+"="+updates[key])
            seen.add(key)
            continue
    out.append(line)
for key,value in updates.items():
    if key not in seen:
        out.append(key+"="+value)
tmp=path.with_suffix(".tmp")
tmp.write_text("\n".join(out)+"\n")
os.chmod(tmp,0o600)
tmp.replace(path)
PY

python3 "$TM_PREVIOUS_RELEASE_REPO/deploy/backup_database.py"
docker exec -i telegram-manager-db-1 pg_restore --list < /opt/telegram-manager/backups/latest.dump >/dev/null

echo "ROLLBACK_OK project=$TM_PREVIOUS_PROJECT_NAME"
