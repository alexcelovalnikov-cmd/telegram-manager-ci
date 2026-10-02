#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
controller="$repo_root/deploy/ops/tmctl-root"
operator_user="${TM_OPERATOR_USER:-telegram-manager}"
staging="/opt/telegram-manager/staging"

if [[ "$(id -u)" -eq 0 ]]; then
  echo "ERROR: routine tmctl staging must run as the project user, not root" >&2
  exit 1
fi
if [[ "$(id -un)" != "$operator_user" ]]; then
  echo "ERROR: expected project user $operator_user, got $(id -un)" >&2
  exit 1
fi
[[ -d "$staging" && -w "$staging" ]] || {
  echo "ERROR: project staging is not writable: $staging" >&2
  exit 1
}

install -m 755 "$controller" "$staging/tmctl-root"
cat > "$staging/tmctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
exec /usr/bin/sudo -n /usr/local/sbin/tmctl-root "$@"
EOF
chmod 755 "$staging/tmctl"

echo "TMCTL_STAGE_OK operator=$operator_user"
sha256sum "$staging/tmctl-root" "$staging/tmctl"
echo "Next: serverctl project-adapter-install-preview telegram-manager deploy tmctl-root --with-wrapper"
