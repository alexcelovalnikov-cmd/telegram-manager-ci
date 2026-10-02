#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
controller="$repo_root/deploy/ops/tmctl-root"
config_example="$repo_root/deploy/ops/tmctl.env.example"
operator_user="${TM_OPERATOR_USER:-telegram-manager}"
sac_version_file="/etc/server-admin-controller/installed_version"
staging="/opt/telegram-manager/staging"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR: host bootstrap requires root; routine controller updates use stage_tmctl.sh" >&2
  exit 1
fi
id "$operator_user" >/dev/null 2>&1 || {
  echo "ERROR: project operator user does not exist: $operator_user" >&2
  exit 1
}

sac_major=0
sac_version=""
if [[ -r "$sac_version_file" ]]; then
  sac_version="$(tr -d '\r\n' < "$sac_version_file")"
  if [[ "$sac_version" =~ ^V([0-9]+)$ ]]; then
    sac_major="${BASH_REMATCH[1]}"
  fi
fi
if (( sac_major < 7 )); then
  echo "ERROR: Server Admin Controller V7+ is required for tmctl-root installation/update" >&2
  echo "Telegram Manager will not write /usr/local/sbin/tmctl-root, /usr/local/bin/tmctl, or project sudoers directly." >&2
  exit 1
fi

install -d -m 755 /etc/telegram-manager
install -d -m 700 -o root -g root /opt/telegram-manager/secrets
deploy_key=/opt/telegram-manager/secrets/github-deploy-key
legacy_deploy_key=/root/.ssh/telegram_manager_github
if [[ ! -e "$deploy_key" && -f "$legacy_deploy_key" && ! -L "$legacy_deploy_key" ]]; then
  install -m 600 -o root -g root "$legacy_deploy_key" "$deploy_key"
fi
if [[ -f "$deploy_key" && -f "$legacy_deploy_key" ]] && cmp -s "$deploy_key" "$legacy_deploy_key"; then
  rm -f "$legacy_deploy_key" "$legacy_deploy_key.pub"
fi

install -d -m 700 -o "$operator_user" -g "$operator_user" "$staging"
install -d -m 700 -o "$operator_user" -g "$operator_user" "$staging/worker"

if [[ ! -e /etc/telegram-manager/tmctl.env ]]; then
  install -m 600 -o root -g root "$config_example" /etc/telegram-manager/tmctl.env
fi

wrapper_tmp="$(mktemp)"
trap 'rm -f "$wrapper_tmp"' EXIT
cat > "$wrapper_tmp" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
exec /usr/bin/sudo -n /usr/local/sbin/tmctl-root "$@"
EOF
chmod 755 "$wrapper_tmp"

install -m 755 -o "$operator_user" -g "$operator_user" "$controller" "$staging/tmctl-root"
install -m 755 -o "$operator_user" -g "$operator_user" "$wrapper_tmp" "$staging/tmctl"

echo "TMCTL_STAGE_OK operator=$operator_user server_admin=$sac_version"
sha256sum "$staging/tmctl-root" "$staging/tmctl"
echo "Next: serverctl project-adapter-install-preview telegram-manager deploy tmctl-root --with-wrapper"
echo "Then, after explicit approval: serverctl apply <preview_id> <digest>"
