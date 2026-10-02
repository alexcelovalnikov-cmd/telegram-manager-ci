#!/usr/bin/env bash
set -euo pipefail

instance="${TM_INSTANCE_ID:-macbook-owner}"
calendar_image="${TM_CALENDAR_IMAGE:-}"
db_container="${TM_DATABASE_CONTAINER:-telegram-manager-db-1}"
credential_dir="${TM_CALDAV_CREDENTIAL_DIR:-/opt/telegram-manager/runtime/credentials}"
credential_file="$credential_dir/caldav-owner.env"

fail(){ echo "ERROR: $*" >&2; exit 1; }
[[ "$(id -u)" -eq 0 ]] || fail "root required"
[[ -n "$calendar_image" ]] || fail "TM_CALENDAR_IMAGE is required"
docker inspect "$db_container" >/dev/null 2>&1 || fail "database container not found"
docker image inspect "$calendar_image" >/dev/null 2>&1 || fail "calendar image not found"

IFS= read -r password || fail "password required on stdin"
[[ ${#password} -ge 32 && ${#password} -le 256 ]] || fail "password must be 32..256 characters"
[[ "$password" != *$'\n'* && "$password" != *$'\r'* ]] || fail "invalid password"

db_name="$(docker exec "$db_container" printenv POSTGRES_DB 2>/dev/null || true)"
db_user="$(docker exec "$db_container" printenv POSTGRES_USER 2>/dev/null || true)"
db_name="${db_name:-postgres}"
db_user="${db_user:-postgres}"

query(){
  docker exec -i "$db_container" psql -X -A -t -q -v ON_ERROR_STOP=1 -U "$db_user" -d "$db_name" "$@"
}

username="$(query -v instance="$instance" <<'SQL'
SELECT username FROM tm_calendar.users WHERE instance_id=:'instance' AND active LIMIT 1;
SQL
)"
[[ -n "$username" ]] || fail "calendar owner not provisioned"

password_hash="$(printf '%s' "$password" | docker run --rm -i --entrypoint python "$calendar_image" -c 'import sys; from argon2 import PasswordHasher; print(PasswordHasher().hash(sys.stdin.read()))')"
[[ "$password_hash" == '$argon2id$'* ]] || fail "failed to generate Argon2id hash"

updated="$(query -v username="$username" -v instance="$instance" -v password_hash="$password_hash" <<'SQL'
UPDATE tm_calendar.users
SET password_hash=:'password_hash',revision=revision+1,updated_at=clock_timestamp()
WHERE username=:'username' AND instance_id=:'instance' AND active
RETURNING username;
SQL
)"
[[ "$updated" == "$username" ]] || fail "calendar owner password update failed"

install -d -m 700 -o root -g root "$credential_dir"
tmp="$(mktemp "$credential_dir/.caldav-owner.XXXXXX")"
chmod 600 "$tmp"
{
  printf 'CALDAV_USERNAME=%s\n' "$username"
  printf 'CALDAV_PASSWORD=%s\n' "$password"
} > "$tmp"
chown root:root "$tmp"
mv "$tmp" "$credential_file"
unset password password_hash

echo "CALDAV_OWNER_PASSWORD_UPDATED username=$username credential_file=$credential_file"
