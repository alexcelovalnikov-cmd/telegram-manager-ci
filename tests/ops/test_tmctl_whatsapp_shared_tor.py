from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TMCTL = (ROOT / "deploy" / "ops" / "tmctl-root").read_text()
COLLECTOR = (ROOT / "whatsapp" / "collector.mjs").read_text()


def test_whatsapp_reuses_existing_worker_tor_namespace():
    assert 'container_exists "$WORKER_CONTAINER"' in TMCTL
    assert '--network "container:$WORKER_CONTAINER"' in TMCTL
    assert '-e TM_WHATSAPP_SOCKS_PROXY=socks5h://127.0.0.1:9050' in TMCTL
    assert '"$image" node collector.mjs' in TMCTL


def test_whatsapp_start_fails_closed_without_ready_worker_tor():
    assert 'Telegram worker is required for WhatsApp Tor transport' in TMCTL
    assert "grep -F 'TOR_TRANSPORT_READY' >/dev/null" in TMCTL
    assert 'Telegram worker Tor transport is not ready' in TMCTL


def test_collector_routes_websocket_and_fetch_through_proxy_agent():
    assert "new SocksProxyAgent(proxyUrl)" in COLLECTOR
    assert 'agent:proxyAgent,fetchAgent:proxyAgent' in COLLECTOR


def test_worker_recreation_rebinds_enabled_whatsapp_to_new_tor_namespace():
    start = TMCTL.index("worker_start_image()")
    stop = TMCTL.index("worker_stop()")
    body = TMCTL[start:stop]
    assert 'local whatsapp_should_rebind=false' in body
    assert 'if [[ "${TM_WHATSAPP_ENABLED:-false}" == "true" ]]' in body
    assert body.index('docker rm -f "$WHATSAPP_CONTAINER"') < body.index('docker rm -f "$WORKER_CONTAINER"')
    assert 'if whatsapp_start_image; then' in body
    assert 'WHATSAPP_REBIND_OK' in body


def test_worker_stop_releases_dependent_whatsapp_namespace_first():
    start = TMCTL.index("worker_stop()")
    stop = TMCTL.index("whatsapp_start_image()")
    body = TMCTL[start:stop]
    assert body.index('docker rm -f "$WHATSAPP_CONTAINER"') < body.index('docker stop "$WORKER_CONTAINER"')


def test_whatsapp_status_detects_stale_shared_tor_namespace():
    start = TMCTL.index("whatsapp_status()")
    stop = TMCTL.index("whatsapp_qr_export()")
    body = TMCTL[start:stop]
    assert 'whatsapp_namespace_matches_worker' in body
    assert 'shared_tor_namespace=ready' in body
    assert 'shared_tor_namespace=stale' in body
    assert 'shared_tor_namespace=worker_absent' in body


def test_whatsapp_start_is_idempotent_when_namespace_and_session_are_ready():
    start = TMCTL.index("whatsapp_start_image()")
    stop = TMCTL.index("whatsapp_stop()")
    body = TMCTL[start:stop]
    assert 'whatsapp_namespace_matches_worker' in body
    assert '"(connected|qr_ready)"' in body
    assert 'reused=true' in body


def test_namespace_match_uses_current_worker_container_identity():
    start = TMCTL.index("whatsapp_namespace_matches_worker()")
    stop = TMCTL.index("with_deploy_lock()")
    body = TMCTL[start:stop]
    assert "'{{.Id}}'" in body
    assert "'{{.HostConfig.NetworkMode}}'" in body
    assert 'container:$worker_id' in body


def test_whatsapp_status_uses_current_worker_tor_not_legacy_whatsapp_log():
    start = TMCTL.index("whatsapp_status()")
    stop = TMCTL.index("whatsapp_qr_export()")
    body = TMCTL[start:stop]
    assert "TOR_TRANSPORT_READY" in body
    assert "shared_tor_transport=ready" in body
    assert "shared_tor_transport=pending_or_failed" in body
    assert 'tor-bootstrap.log' not in body


def test_worker_and_whatsapp_readiness_checks_consume_complete_docker_logs():
    start = TMCTL.index("worker_start_image()")
    stop = TMCTL.index("whatsapp_qr_export()")
    body = TMCTL[start:stop]
    assert "grep -F 'TELEGRAM V18 CONNECTED' >/dev/null" in body
    assert body.count("grep -F 'TOR_TRANSPORT_READY' >/dev/null") >= 2
    assert "grep -E 'WHATSAPP_(QR_READY|CONNECTED)' >/dev/null" in body
    assert "docker logs \"$WORKER_CONTAINER\" 2>&1 | grep -q" not in body
    assert "docker logs \"$WHATSAPP_CONTAINER\" 2>&1 | grep -Eq" not in body


def test_collector_uses_live_whatsapp_web_version_for_pairing():
    assert "fetchLiveWaWebVersion" in COLLECTOR
    assert "https.get('https://web.whatsapp.com/sw.js'" in COLLECTOR
    assert "agent:proxyAgent" in COLLECTOR
    assert "fetchLatestWaWebVersion" not in COLLECTOR
    assert "fetchLatestBaileysVersion" not in COLLECTOR
    assert "wa_web_version_source" in COLLECTOR
    assert "live_tor_https" in COLLECTOR
    assert "whatsapp_live_web_version_unavailable" in COLLECTOR
