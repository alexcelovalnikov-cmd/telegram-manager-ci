from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

def test_tor_timeout_includes_bootstrap_log():
    text = (ROOT / "worker" / "run_with_tor.py").read_text(encoding="utf-8")
    assert 'raise RuntimeError("Tor bootstrap timeout\\n" + details)' in text
    assert 'read_text(errors="replace")[-8000:]' in text
