from pathlib import Path

POST = Path("deploy/v31/post_deploy.sh").read_text()


def test_optional_whatsapp_failure_does_not_abort_release():
    assert "if ! /usr/local/sbin/tmctl-root whatsapp-start; then" in POST
    assert "optional WhatsApp source did not reach QR/connected state; release remains valid" in POST


def test_required_worker_start_remains_fatal():
    assert "/usr/local/sbin/tmctl-root worker-start" in POST
    assert "if ! /usr/local/sbin/tmctl-root worker-start" not in POST
