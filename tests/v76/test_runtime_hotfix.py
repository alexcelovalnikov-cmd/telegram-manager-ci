from pathlib import Path

from tm_api.v25.business import rules_profile

def test_rcc_settlement_keeps_projects_payments_profile():
    assert rules_profile({'analysis':{'profile':'rcc_settlement'}},'default')=='projects_payments'
    assert rules_profile({'analysis':{'profile':'postproduction'}},'default')=='postproduction'
    assert rules_profile({'analysis':{}},'projects_payments')=='projects_payments'

def test_google_secret_staging_replaces_docker_created_directory():
    text=Path('deploy/ops/deploy_release.sh').read_text()
    assert 'rm -rf -- "$target"' in text
    assert 'install -m 400 -o 10001 -g 10001 "$google_secret_source" "$target"' in text

def test_running_worker_is_promoted_to_current_release_image():
    text=Path('deploy/v31/post_deploy.sh').read_text()
    assert 'worker_was_running=true' in text
    assert "TM_WORKER_ENABLED=true" in text
    assert '/usr/local/sbin/tmctl-root worker-start' in text
