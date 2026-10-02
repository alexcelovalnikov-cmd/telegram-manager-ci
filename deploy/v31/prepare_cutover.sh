#!/usr/bin/env bash
set -euo pipefail

release="${1:?Usage: prepare_cutover.sh RELEASE [COMPOSE_FILE]}"
script_dir="$(cd "$(dirname "$0")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
compose_file="${2:-$script_dir/compose.production.yaml}"
validation_root="${TM_VALIDATION_ROOT:-/opt/telegram-manager/validation}"
backup_root="${TM_BACKUP_ROOT:-/opt/telegram-manager/backups}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
cutover_dir="$validation_root/${release}-cutover-$stamp"

docker compose -f "$compose_file" config -q

mkdir -p "$cutover_dir"
chmod 700 "$cutover_dir"

python3 "$repo_root/deploy/backup_database.py"

docker exec -i telegram-manager-db-1   pg_restore --list   < "$backup_root/latest.dump"   >/dev/null

cp "$backup_root/latest.dump" "$cutover_dir/database.dump"
cp "$backup_root/latest.tar.gz" "$cutover_dir/recovery.tar.gz"

{
  echo "release=$release"
  echo "created_at=$(date -Is)"
  echo "git_head=$(git -C "$repo_root" rev-parse HEAD)"
  echo "git_tree=$(git -C "$repo_root" rev-parse HEAD^{tree})"
  echo
  echo "=== running telegram-manager containers ==="
  docker ps --format '{{.Names}}|{{.Image}}|{{.Status}}|{{.Ports}}'     | grep 'telegram-manager' || true
} > "$cutover_dir/state.txt"

sha256sum "$compose_file" > "$cutover_dir/compose.sha256"

chmod 600 "$cutover_dir"/*
sync

echo "CUTOVER_BACKUP_OK"
echo "CUTOVER_DIR=$cutover_dir"
