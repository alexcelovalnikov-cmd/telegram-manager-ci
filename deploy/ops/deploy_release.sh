#!/usr/bin/env bash
set -euo pipefail

version="${1:-}"
if [[ ! "$version" =~ ^V[0-9]+$ ]]; then
  echo "ERROR: release must look like V32" >&2
  exit 2
fi

CONFIG=/etc/telegram-manager/tmctl.env
CONTROL_REPO=/opt/telegram-manager/git-main
RUNTIME=/opt/telegram-manager/runtime
RELEASES=/opt/telegram-manager/releases
BACKUPS=/opt/telegram-manager/backups
VALIDATION=/opt/telegram-manager/validation

# shellcheck disable=SC1090
source "$CONFIG"

DEPLOY_KEY="${TM_DEPLOY_KEY:-/opt/telegram-manager/secrets/github-deploy-key}"
[[ -f "$DEPLOY_KEY" && ! -L "$DEPLOY_KEY" ]] || {
  echo "ERROR: missing project-specific deploy key: $DEPLOY_KEY" >&2
  exit 1
}

required=(
  TM_RELEASE_REPO
  TM_RELEASE_COMPOSE
  TM_API_IMAGE
  TM_CALENDAR_IMAGE
  TM_API_ENV_FILE
  TM_API_DSN_FILE
  TM_CALENDAR_ENV_FILE
  TM_DATABASE_NETWORK
  TM_STATE_DIR
  TM_PROJECT_NAME
)
for key in "${required[@]}"; do
  [[ -n "${!key:-}" ]] || {
    echo "ERROR: missing config key $key" >&2
    exit 1
  }
done

lower="$(printf '%s' "$version" | tr '[:upper:]' '[:lower:]')"
release_dir="$RELEASES/$version"
release_compose="$release_dir/deploy/v31/compose.production.yaml"
api_image="telegram-manager:$version"
calendar_image="telegram-manager-calendar:$version"
shadow_project="telegram-manager-${lower}-shadow"
production_project="telegram-manager-${lower}-production"
shadow_state="$RUNTIME/shadows/$version"
previous_file="$RUNTIME/previous.env"
google_secret_source=/opt/telegram-manager/secrets/google-sheets-service-account.json

prepare_google_secret() {
  local state_dir="$1"
  local target="$state_dir/google-sheets-service-account.json"
  rm -rf -- "$target"
  if [[ -f "$google_secret_source" && ! -L "$google_secret_source" ]]; then
    install -m 400 -o 10001 -g 10001 "$google_secret_source" "$target"
  else
    install -m 400 -o 10001 -g 10001 /dev/null "$target"
  fi
}

old_release_repo="$TM_RELEASE_REPO"
old_compose="$TM_RELEASE_COMPOSE"
old_api_image="$TM_API_IMAGE"
old_calendar_image="$TM_CALENDAR_IMAGE"
old_project="$TM_PROJECT_NAME"
old_state="$TM_STATE_DIR"
old_api_env="$TM_API_ENV_FILE"
old_api_dsn="$TM_API_DSN_FILE"
old_calendar_env="$TM_CALENDAR_ENV_FILE"
old_network="$TM_DATABASE_NETWORK"

export GIT_SSH_COMMAND="ssh -i $DEPLOY_KEY -o IdentitiesOnly=yes"

echo "== Fetch release tag =="
git -C "$CONTROL_REPO" fetch origin main:refs/remotes/origin/main --tags

tag_ref="refs/tags/$version"
git -C "$CONTROL_REPO" show-ref --verify --quiet "$tag_ref" || {
  echo "ERROR: Git tag $version does not exist" >&2
  exit 1
}
release_commit="$(git -C "$CONTROL_REPO" rev-parse "$tag_ref^{commit}")"
release_tree="$(git -C "$CONTROL_REPO" rev-parse "$tag_ref^{tree}")"

git -C "$CONTROL_REPO" merge-base --is-ancestor "$release_commit" origin/main || {
  echo "ERROR: $version is not reachable from origin/main" >&2
  exit 1
}

current_name="$(basename "$old_release_repo")"
if [[ "$current_name" =~ ^V([0-9]+)$ ]]; then
  current_number="${BASH_REMATCH[1]}"
  next_number="${version#V}"
  if (( next_number <= current_number )); then
    echo "ERROR: refusing non-forward deploy $current_name -> $version" >&2
    exit 1
  fi
fi

echo "release=$version"
echo "commit=$release_commit"
echo "tree=$release_tree"

echo "== Create immutable release snapshot =="
install -d -m 700 "$RELEASES"
if [[ -e "$release_dir" ]]; then
  [[ -f "$release_dir/RELEASE" ]] || {
    echo "ERROR: existing $release_dir has no RELEASE metadata" >&2
    exit 1
  }
  grep -Fxq "source_commit=$release_commit" "$release_dir/RELEASE" || {
    echo "ERROR: existing release commit mismatch" >&2
    exit 1
  }
else
  tmp="$RELEASES/.$version.tmp.$$"
  trap 'rm -rf "$tmp"' EXIT
  install -d -m 700 "$tmp"
  git -C "$CONTROL_REPO" archive "$tag_ref" | tar -x -C "$tmp"
  {
    echo "release=$version"
    echo "source_commit=$release_commit"
    echo "tree=$release_tree"
    echo "created_at=$(date -Is)"
  } > "$tmp/RELEASE"
  chmod 600 "$tmp/RELEASE"
  mv "$tmp" "$release_dir"
  trap - EXIT
fi

[[ -f "$release_compose" ]] || {
  echo "ERROR: release lacks deploy/v31/compose.production.yaml" >&2
  exit 1
}

echo "== Build release images =="
docker build -t "$api_image" "$release_dir"
docker build -f "$release_dir/deploy/v25/Dockerfile.calendar" -t "$calendar_image" "$release_dir"
api_id="$(docker image inspect "$api_image" --format '{{.Id}}')"
calendar_id="$(docker image inspect "$calendar_image" --format '{{.Id}}')"

{
  grep -vE '^(api_image|api_image_id|calendar_image|calendar_image_id)=' "$release_dir/RELEASE"
  echo "api_image=$api_image"
  echo "api_image_id=$api_id"
  echo "calendar_image=$calendar_image"
  echo "calendar_image_id=$calendar_id"
} > "$release_dir/RELEASE.next"
mv "$release_dir/RELEASE.next" "$release_dir/RELEASE"
chmod 600 "$release_dir/RELEASE"

echo "== Prepare isolated shadow state =="
install -d -m 700 "$RUNTIME/shadows"
rm -rf "$shadow_state"
install -d -m 700 -o 10001 -g 10001 "$shadow_state"
prepare_google_secret "$shadow_state"

export TM_PROJECT_NAME="$shadow_project"
export TM_API_IMAGE="$api_image"
export TM_CALENDAR_IMAGE="$calendar_image"
export TM_API_ENV_FILE="$old_api_env"
export TM_API_DSN_FILE="$old_api_dsn"
export TM_CALENDAR_ENV_FILE="$old_calendar_env"
export TM_DATABASE_NETWORK="$old_network"
export TM_STATE_DIR="$shadow_state"
export TM_API_PORT=8767
export TM_CALENDAR_PORT=5233
export TM_WRITES_ENABLED=false
export TM_BACKGROUND_ENABLED=false
export TM_OAUTH_ENABLED=false

echo "== Read-only release validation =="
bash "$release_dir/deploy/v31/validate_release.sh" "$release_compose"

echo "== Shadow verification =="
docker compose -p "$shadow_project" -f "$release_compose" up -d calendar api
shadow_cleanup() {
  docker compose -p "$shadow_project" -f "$release_compose" down >/dev/null 2>&1 || true
}
trap shadow_cleanup EXIT
bash "$release_dir/deploy/v31/post_deploy.sh" "$release_compose"
shadow_cleanup
trap - EXIT

echo "== Fresh recovery backup before cutover =="
python3 "$release_dir/deploy/backup_database.py"
docker exec -i telegram-manager-db-1 pg_restore --list < "$BACKUPS/latest.dump" >/dev/null

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
cutover_dir="$VALIDATION/${version}-cutover-$stamp"
install -d -m 700 "$cutover_dir"
cp "$BACKUPS/latest.dump" "$cutover_dir/database.dump"
cp "$BACKUPS/latest.tar.gz" "$cutover_dir/recovery.tar.gz"
sha256sum "$release_compose" > "$cutover_dir/compose.sha256"
{
  echo "release=$version"
  echo "source_commit=$release_commit"
  echo "tree=$release_tree"
  echo "api_image=$api_image"
  echo "api_image_id=$api_id"
  echo "calendar_image=$calendar_image"
  echo "calendar_image_id=$calendar_id"
  echo "previous_project=$old_project"
  echo "previous_compose=$old_compose"
  echo "created_at=$(date -Is)"
} > "$cutover_dir/state.txt"
chmod 600 "$cutover_dir"/*
echo "CUTOVER_DIR=$cutover_dir"

export TM_PROJECT_NAME="$production_project"
export TM_API_IMAGE="$api_image"
export TM_CALENDAR_IMAGE="$calendar_image"
export TM_API_ENV_FILE="$old_api_env"
export TM_API_DSN_FILE="$old_api_dsn"
export TM_CALENDAR_ENV_FILE="$old_calendar_env"
export TM_DATABASE_NETWORK="$old_network"
export TM_STATE_DIR="$old_state"
prepare_google_secret "$old_state"
export TM_API_PORT=8766
export TM_CALENDAR_PORT=5232
export TM_WRITES_ENABLED=true
export TM_BACKGROUND_ENABLED=true
export TM_OAUTH_ENABLED=true

rollback_on_error() {
  rc=$?
  trap - ERR
  echo "DEPLOY_FAILED rc=$rc; attempting rollback to $old_project" >&2
  docker compose -p "$production_project" -f "$release_compose" stop api calendar >/dev/null 2>&1 || true

  TM_PROJECT_NAME="$old_project"   TM_API_IMAGE="$old_api_image"   TM_CALENDAR_IMAGE="$old_calendar_image"   TM_API_ENV_FILE="$old_api_env"   TM_API_DSN_FILE="$old_api_dsn"   TM_CALENDAR_ENV_FILE="$old_calendar_env"   TM_DATABASE_NETWORK="$old_network"   TM_STATE_DIR="$old_state"   TM_API_PORT=8766 TM_CALENDAR_PORT=5232   TM_WRITES_ENABLED=true TM_BACKGROUND_ENABLED=true TM_OAUTH_ENABLED=true   docker compose -p "$old_project" -f "$old_compose" start calendar api >/dev/null 2>&1 || true

  echo "ROLLBACK_ATTEMPTED" >&2
  exit "$rc"
}
trap rollback_on_error ERR

echo "== Cut over $old_project -> $production_project =="
TM_PROJECT_NAME="$old_project" TM_API_IMAGE="$old_api_image" TM_CALENDAR_IMAGE="$old_calendar_image" TM_API_ENV_FILE="$old_api_env" TM_API_DSN_FILE="$old_api_dsn" TM_CALENDAR_ENV_FILE="$old_calendar_env" TM_DATABASE_NETWORK="$old_network" TM_STATE_DIR="$old_state" TM_API_PORT=8766 TM_CALENDAR_PORT=5232 TM_WRITES_ENABLED=true TM_BACKGROUND_ENABLED=true TM_OAUTH_ENABLED=true docker compose -p "$old_project" -f "$old_compose" stop api calendar

docker compose -p "$production_project" -f "$release_compose" up -d calendar api
bash "$release_dir/deploy/v31/post_deploy.sh" "$release_compose"
trap - ERR

echo "== Save rollback metadata =="
install -d -m 700 "$RUNTIME"
cat > "$previous_file" <<EOF
TM_PREVIOUS_RELEASE_REPO=$old_release_repo
TM_PREVIOUS_COMPOSE=$old_compose
TM_PREVIOUS_API_IMAGE=$old_api_image
TM_PREVIOUS_CALENDAR_IMAGE=$old_calendar_image
TM_PREVIOUS_PROJECT_NAME=$old_project
TM_PREVIOUS_STATE_DIR=$old_state
TM_PREVIOUS_API_ENV_FILE=$old_api_env
TM_PREVIOUS_API_DSN_FILE=$old_api_dsn
TM_PREVIOUS_CALENDAR_ENV_FILE=$old_calendar_env
TM_PREVIOUS_DATABASE_NETWORK=$old_network
EOF
chmod 600 "$previous_file"

python3 - "$CONFIG"   "TM_RELEASE_REPO=$release_dir"   "TM_RELEASE_COMPOSE=$release_compose"   "TM_PREVIOUS_COMPOSE=$old_compose"   "TM_PROJECT_NAME=$production_project"   "TM_API_IMAGE=$api_image"   "TM_CALENDAR_IMAGE=$calendar_image" <<'PY'
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

echo "== Post-cutover recovery backup =="
python3 "$release_dir/deploy/backup_database.py"
docker exec -i telegram-manager-db-1 pg_restore --list < "$BACKUPS/latest.dump" >/dev/null

rm -rf "$shadow_state"
echo "DEPLOY_OK release=$version project=$production_project commit=$release_commit"
