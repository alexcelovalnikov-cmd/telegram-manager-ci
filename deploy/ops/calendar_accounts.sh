#!/usr/bin/env bash
set -euo pipefail

mode="${1:-}"
username="${2:-}"
instance="${TM_INSTANCE_ID:-macbook-owner}"
calendar_image="${TM_CALENDAR_IMAGE:-}"
db_container="${TM_DATABASE_CONTAINER:-telegram-manager-db-1}"
credential_dir="${TM_CALDAV_CREDENTIAL_DIR:-/opt/telegram-manager/runtime/credentials}"

fail(){ echo "ERROR: $*" >&2; exit 1; }

[[ "$mode" == "owner" || "$mode" == "user" ]] || fail "usage: calendar_accounts.sh owner <username> | user <username>"
[[ "$username" =~ ^[A-Za-z0-9_-]{1,64}$ ]] || fail "invalid calendar username"
[[ "$username" != "caldav" && "$username" != "telegram-manager" ]] || fail "reserved calendar username"
[[ -n "$calendar_image" ]] || fail "TM_CALENDAR_IMAGE is required"
docker inspect "$db_container" >/dev/null 2>&1 || fail "database container not found"
docker image inspect "$calendar_image" >/dev/null 2>&1 || fail "calendar image not found"

db_name="$(docker exec "$db_container" printenv POSTGRES_DB 2>/dev/null || true)"
db_user="$(docker exec "$db_container" printenv POSTGRES_USER 2>/dev/null || true)"
db_name="${db_name:-postgres}"
db_user="${db_user:-postgres}"

query(){
  docker exec -i "$db_container" psql -X -A -t -q -v ON_ERROR_STOP=1 -U "$db_user" -d "$db_name" "$@"
}

if [[ "$mode" == "owner" ]]; then
  existing="$(query -v instance="$instance" <<'SQL'
SELECT username FROM tm_calendar.users WHERE instance_id=:'instance' AND active LIMIT 1;
SQL
)"
  if [[ -n "$existing" ]]; then
    echo "CALENDAR_OWNER_EXISTS username=$existing"
    exit 0
  fi
else
  existing="$(query -v username="$username" <<'SQL'
SELECT username FROM tm_calendar.users WHERE username=:'username' LIMIT 1;
SQL
)"
  if [[ -n "$existing" ]]; then
    echo "CALENDAR_USER_EXISTS username=$existing"
    exit 0
  fi
fi

collision="$(query -v username="$username" <<'SQL'
SELECT username FROM tm_calendar.users WHERE username=:'username' LIMIT 1;
SQL
)"
[[ -z "$collision" ]] || fail "calendar username already exists"

password="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(40))
PY
)"
password_hash="$(printf '%s' "$password" | docker run --rm -i --entrypoint python "$calendar_image" -c 'import sys; from argon2 import PasswordHasher; print(PasswordHasher().hash(sys.stdin.read()))')"
[[ "$password_hash" == '$argon2id$'* ]] || fail "failed to generate Argon2id hash"

if [[ "$mode" == "owner" ]]; then
  query -v username="$username" -v instance="$instance" -v password_hash="$password_hash" <<'SQL' >/dev/null
BEGIN;
SELECT pg_advisory_xact_lock(74240928);
INSERT INTO tm_calendar.users(username,instance_id,password_hash,active)
VALUES (:'username',:'instance',:'password_hash',true);
COMMIT;
SQL
  credential_file="$credential_dir/caldav-owner.env"
else
  query -v username="$username" -v password_hash="$password_hash" <<'SQL' >/dev/null
BEGIN;
SELECT pg_advisory_xact_lock(74240928);
INSERT INTO tm_calendar.users(username,instance_id,password_hash,active)
VALUES (:'username',NULL,:'password_hash',true);
COMMIT;
SQL
  credential_file="$credential_dir/caldav-user-$username.env"
fi

install -d -m 700 -o root -g root "$credential_dir"
tmp="$(mktemp "$credential_dir/.caldav.XXXXXX")"
chmod 600 "$tmp"
{
  printf 'CALDAV_USERNAME=%s\n' "$username"
  printf 'CALDAV_PASSWORD=%s\n' "$password"
} > "$tmp"
chown root:root "$tmp"
mv "$tmp" "$credential_file"
unset password password_hash

if [[ "$mode" == "owner" ]]; then
  echo "CALENDAR_OWNER_PROVISIONED username=$username credential_file=$credential_file"
else
  echo "CALENDAR_USER_PROVISIONED username=$username credential_file=$credential_file"
fi
