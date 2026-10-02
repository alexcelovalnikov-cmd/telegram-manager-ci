from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COLLECTOR = (ROOT / "whatsapp" / "collector.mjs").read_text()


def test_live_wa_web_revision_uses_existing_socks_https_agent():
    assert "https.get('https://web.whatsapp.com/sw.js'" in COLLECTOR
    assert "agent:proxyAgent" in COLLECTOR
    assert "live_tor_https" in COLLECTOR
    assert "fetchLatestWaWebVersion" not in COLLECTOR


def test_live_wa_web_revision_fails_closed_when_unavailable():
    assert "wa_web_version_timeout" in COLLECTOR
    assert "whatsapp_live_web_version_unavailable" in COLLECTOR
