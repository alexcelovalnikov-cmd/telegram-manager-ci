#!/bin/bash
# Run on Alexey's Mac. Only remote reads; no reload, backup mutation or migration.
set -euo pipefail
config='/home/demo/Documents/GPT/Rent_Rabbit_Plugin/server-access/ssh_config'
out="$HOME/Downloads/Telegram_Manager_Schema_V22.sql.gz"
manifest="$HOME/Downloads/Telegram_Manager_Runtime_V22.txt"
if [[ -e "$out" || -e "$manifest" ]]; then
  printf '%s\n' 'Output already exists. Preserve it and choose a new version before repeating.' >&2;exit 1
fi
umask 077
tmp=$(mktemp "$HOME/Downloads/tm-schema.XXXXXX")
trap 'rm -f "$tmp"' EXIT
ssh -F "$config" -o BatchMode=yes rr-server \
 'docker exec telegram-manager-db-1 sh -c '\''exec pg_dump -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-postgres}" --schema-only --no-owner --no-privileges --schema=public --schema=extensions --schema=tm_api_private'\''' | gzip > "$tmp"
gzip -t "$tmp"
mv "$tmp" "$out"
ssh -F "$config" -o BatchMode=yes rr-server \
 'docker inspect --format "{{.Image}}" telegram-manager-api-1; docker exec telegram-manager-api-1 sh -c '\''cd /app && find tm_api -type f ! -name "*.pyc" ! -path "*/__pycache__/*" -exec sha256sum {} \; | sort'\''' > "$manifest"
printf 'Созданы:\n%s\n%s\n' "$out" "$manifest"
printf '%s\n' 'Это схема без строк данных и контрольные суммы кода. .env, пароли, сессии и дампы данных не копировались.'
