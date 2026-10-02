from pathlib import Path


def test_worker_login_reads_persisted_tor_log_on_container_loss():
    text = Path("deploy/ops/tmctl-root").read_text()
    assert '--- persisted tor-bootstrap.log ---' in text
    assert 'tail -n 80 "$WORKER_ROOT/state/tor-bootstrap.log"' in text
