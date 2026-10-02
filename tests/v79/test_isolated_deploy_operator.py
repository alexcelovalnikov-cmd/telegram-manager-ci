from pathlib import Path


def test_restricted_controller_targets_project_operator():
    installer = Path("deploy/ops/install_tmctl.sh").read_text()
    controller = Path("deploy/ops/tmctl-root").read_text()
    assert 'operator_user="${TM_OPERATOR_USER:-telegram-manager}"' in installer
    assert 'Server Admin Controller V7+ is required for tmctl-root installation/update' in installer
    assert 'if (( sac_major < 7 )); then' in installer
    assert 'install -m 755 -o root -g root "$controller" /usr/local/sbin/tmctl-root' not in installer
    assert 'cat > "$sudoers_file"' not in installer
    assert '/home/chatgpt-tm/worker-stage' not in controller
    assert 'TM_WORKER_STAGE:-/opt/telegram-manager/staging/worker' in controller


def test_deploy_and_rollback_share_project_lock():
    controller = Path("deploy/ops/tmctl-root").read_text()
    assert 'DEPLOY_LOCK="${TM_DEPLOY_LOCK:-/run/lock/telegram-manager.deploy.lock}"' in controller
    assert 'flock -n 9' in controller
    assert 'with_deploy_lock bash "$TM_RELEASE_REPO/deploy/ops/deploy_release.sh" "$release"' in controller
    assert 'with_deploy_lock bash "$TM_RELEASE_REPO/deploy/ops/rollback_last.sh"' in controller


def test_deploy_key_is_project_scoped():
    deploy = Path("deploy/ops/deploy_release.sh").read_text()
    installer = Path("deploy/ops/install_tmctl.sh").read_text()
    assert '/root/.ssh/telegram_manager_github' not in deploy
    assert 'TM_DEPLOY_KEY:-/opt/telegram-manager/secrets/github-deploy-key' in deploy
    assert 'legacy_deploy_key=/root/.ssh/telegram_manager_github' in installer
    assert 'deploy_key=/opt/telegram-manager/secrets/github-deploy-key' in installer


def test_privileged_ops_use_immutable_release_scripts():
    controller = Path("deploy/ops/tmctl-root").read_text()
    assert 'bash "$TM_RELEASE_REPO/deploy/ops/calendar_accounts.sh"' in controller
    assert '/opt/telegram-manager/git-main/deploy/ops/deploy_release.sh' not in controller
    assert '/opt/telegram-manager/git-main/deploy/ops/rollback_last.sh' not in controller
