from pathlib import Path

TMCTL = Path("deploy/ops/tmctl-root").read_text()


def _function_body(name: str, next_name: str) -> str:
    start = TMCTL.index(name + "() {")
    end = TMCTL.index(next_name + "() {", start)
    return TMCTL[start:end]


def test_whatsapp_start_requires_shared_worker_tor():
    body = _function_body("whatsapp_start_image", "whatsapp_stop")
    assert 'container_exists "$WORKER_CONTAINER"' in body
    assert "TOR_TRANSPORT_READY" in body
    assert "Telegram worker Tor transport is not ready" in body
    assert '--network "container:$WORKER_CONTAINER"' in body


def test_whatsapp_start_has_bounded_qr_or_connected_wait():
    body = _function_body("whatsapp_start_image", "whatsapp_stop")
    assert "seq 1 90" in body
    assert "WHATSAPP_(QR_READY|CONNECTED)" in body


def test_whatsapp_status_reports_shared_tor_diagnostics():
    body = _function_body("whatsapp_status", "whatsapp_qr_export")
    assert "shared_tor_transport=ready" in body
    assert "shared_tor_transport=pending_or_failed" in body
    assert "shared_tor_namespace=ready" in body
    assert "shared_tor_namespace=stale" in body
    assert "tor-bootstrap.log" not in body
