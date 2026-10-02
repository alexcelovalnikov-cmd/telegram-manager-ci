from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COLLECTOR = (ROOT / "whatsapp" / "collector.mjs").read_text()


def test_whatsapp_uses_web_browser_fingerprint_for_fresh_pairing():
    assert "Browsers.ubuntu('Chrome')" in COLLECTOR
    assert "Browsers.macOS('Desktop')" not in COLLECTOR
