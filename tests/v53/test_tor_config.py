from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

def test_tor_uses_state_config_file():
    text = (ROOT / "worker" / "run_with_tor.py").read_text(encoding="utf-8")
    assert 'torrc = state / "torrc"' in text
    assert '["tor", "-f", str(torrc)]' in text
    assert '"/dev/null"' not in text
    assert "ClientOnly 1" in text
