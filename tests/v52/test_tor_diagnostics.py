from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

def test_tor_bootstrap_errors_are_visible():
    text = (ROOT / "worker" / "run_with_tor.py").read_text(encoding="utf-8")
    assert "tor-bootstrap.log" in text
    assert "Log notice stdout" in text
    assert "stderr=subprocess.STDOUT" in text
    assert "Tor exited before bootstrap\\n" in text
