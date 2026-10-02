#!/usr/bin/env bash
set -euo pipefail

state="${TM_WHATSAPP_STATE_DIR:-/state}"
data="$state/tor-data"
torrc="$state/torrc"
log="$state/tor-bootstrap.log"

mkdir -p "$data"
rm -f "$log"
cat > "$torrc" <<EOF
ClientOnly 1
SocksPort 127.0.0.1:9050
DataDirectory $data
Log notice stdout
EOF
chmod 600 "$torrc"

tor -f "$torrc" >"$log" 2>&1 &
tor_pid=$!
child_pid=""

cleanup() {
  if [[ -n "$child_pid" ]] && kill -0 "$child_pid" 2>/dev/null; then
    kill -TERM "$child_pid" 2>/dev/null || true
  fi
  if kill -0 "$tor_pid" 2>/dev/null; then
    kill -TERM "$tor_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

for _ in $(seq 1 120); do
  if ! kill -0 "$tor_pid" 2>/dev/null; then
    tail -n 80 "$log" >&2 || true
    echo "ERROR: WhatsApp Tor transport exited before bootstrap" >&2
    exit 1
  fi
  if grep -q 'Bootstrapped 100%' "$log" 2>/dev/null; then
    echo "WHATSAPP_TOR_READY"
    break
  fi
  sleep 1
done

grep -q 'Bootstrapped 100%' "$log" 2>/dev/null || {
  tail -n 80 "$log" >&2 || true
  echo "ERROR: WhatsApp Tor bootstrap timeout" >&2
  exit 1
}

export TM_WHATSAPP_SOCKS_PROXY="socks5h://127.0.0.1:9050"
node collector.mjs &
child_pid=$!
wait "$child_pid"
