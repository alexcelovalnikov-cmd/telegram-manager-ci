#!/usr/bin/env bash
set -euo pipefail

host="${1:-service.example.invalid}"
site="${TM_NGINX_SITE:-/etc/nginx/sites-enabled/default}"
snippet_dir=/etc/nginx/snippets
snippet="$snippet_dir/telegram-manager-caldav.conf"
db_container="${TM_DATABASE_CONTAINER:-telegram-manager-db-1}"

fail(){ echo "ERROR: $*" >&2; exit 1; }

[[ "$(id -u)" -eq 0 ]] || fail "root required"
[[ "$host" =~ ^[A-Za-z0-9.-]+$ ]] || fail "invalid host"
[[ -f "$site" && -w "$site" ]] || fail "nginx site is not writable: $site"
command -v nginx >/dev/null 2>&1 || fail "nginx not found"
command -v systemctl >/dev/null 2>&1 || fail "systemctl not found"

install -d -m 755 -o root -g root "$snippet_dir"
before="$(mktemp)"
cp "$site" "$before"
had_snippet=0
[[ -e "$snippet" ]] && had_snippet=1

docker inspect "$db_container" >/dev/null 2>&1 || fail "database container not found"
db_name="$(docker exec "$db_container" printenv POSTGRES_DB 2>/dev/null || true)"
db_user="$(docker exec "$db_container" printenv POSTGRES_USER 2>/dev/null || true)"
db_name="${db_name:-postgres}"
db_user="${db_user:-postgres}"
usernames="$(docker exec -i "$db_container" psql -X -A -t -q -v ON_ERROR_STOP=1 -U "$db_user" -d "$db_name"   -c "SELECT u.username FROM tm_calendar.users u WHERE u.active AND (u.instance_id IS NOT NULL OR EXISTS (SELECT 1 FROM tm_calendar.members m JOIN tm_calendar.calendars c ON c.id=m.calendar_id WHERE m.username=u.username AND m.status='active' AND c.deleted_at IS NULL)) ORDER BY u.username")"
[[ -n "$usernames" ]] || fail "no active calendar users found"

tmp="$(mktemp)"
cat > "$tmp" <<'EOF'
location = /.well-known/caldav {
  return 301 https://$host/caldav/;
}

location /caldav/ {
  proxy_pass http://127.0.0.1:5232/;
  proxy_http_version 1.1;
  proxy_set_header Host $host;
  proxy_set_header X-Forwarded-Proto https;
  proxy_set_header X-Forwarded-For $remote_addr;
  proxy_set_header Connection "";
  proxy_buffering off;
  proxy_read_timeout 45s;
  client_max_body_size 2m;
}

EOF

# Radicale returns absolute DAV hrefs rooted at /<username>/.
# Generate an explicit proxy route for every active calendar account so Apple
# clients can follow principal/home-set URLs after initial /caldav/ discovery.
while IFS= read -r username; do
  [[ "$username" =~ ^[A-Za-z0-9_-]{1,64}$ ]] || fail "invalid calendar username in database"
  [[ "$username" != "caldav" && "$username" != "telegram-manager" ]] || fail "reserved calendar username"
  cat >> "$tmp" <<EOF
location /$username/ {
  proxy_pass http://127.0.0.1:5232/$username/;
  proxy_http_version 1.1;
  proxy_set_header Host \$host;
  proxy_set_header X-Forwarded-Proto https;
  proxy_set_header X-Forwarded-For \$remote_addr;
  proxy_set_header Connection "";
  proxy_buffering off;
  proxy_read_timeout 45s;
  client_max_body_size 2m;
}

EOF
done <<< "$usernames"
install -m 644 -o root -g root "$tmp" "$snippet"
rm -f "$tmp"

python3 - "$site" "$host" "$snippet" <<'PY'
from pathlib import Path
import re,sys
site=Path(sys.argv[1]); host=sys.argv[2]; snippet=sys.argv[3]
text=site.read_text()
include=f" include {snippet};"
if include in text:
    raise SystemExit(0)
pattern=re.compile(rf"(?m)^(\s*)server_name\s+{re.escape(host)};\s*$")
matches=list(pattern.finditer(text))
if len(matches)!=1:
    raise SystemExit(f"expected exactly one server_name {host}, found {len(matches)}")
m=matches[0]
indent=m.group(1)
insert="\n"+indent+f"include {snippet};"
text=text[:m.end()]+insert+text[m.end():]
site.write_text(text)
PY

if ! nginx -t; then
  cp "$before" "$site"
  if [[ "$had_snippet" -eq 0 ]]; then rm -f "$snippet"; fi
  nginx -t || true
  rm -f "$before"
  fail "nginx validation failed; configuration restored"
fi
rm -f "$before"
systemctl reload nginx

redirect_code="$(curl --noproxy '*' -sS --resolve "$host:443:127.0.0.1" -o /dev/null -w '%{http_code}' "https://$host/.well-known/caldav")"
root_code="$(curl --noproxy '*' -sS --resolve "$host:443:127.0.0.1" -o /dev/null -w '%{http_code}' "https://$host/caldav/")"
[[ "$redirect_code" == "301" ]] || fail "unexpected well-known status: $redirect_code"
[[ "$root_code" == "401" || "$root_code" == "302" ]] || fail "unexpected CalDAV unauthenticated status: $root_code"

route_count=0
while IFS= read -r username; do
  code="$(curl --noproxy '*' -sS --resolve "$host:443:127.0.0.1" -o /dev/null -w '%{http_code}' "https://$host/$username/")"
  [[ "$code" == "401" || "$code" == "302" ]] || fail "unexpected DAV principal status for $username: $code"
  route_count=$((route_count+1))
done <<< "$usernames"

echo "CALDAV_PUBLIC_OK server=https://$host/caldav/ well_known=$redirect_code unauthenticated=$root_code principal_routes=$route_count"
