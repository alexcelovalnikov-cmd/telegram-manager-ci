from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_worker_bundles_local_tor_transport():
    dockerfile = (ROOT / "Dockerfile.worker").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements.worker.txt").read_text(encoding="utf-8")
    assert " tor" in dockerfile
    assert "run_with_tor.py" in dockerfile
    assert "PySocks==1.7.1" in requirements


def test_telegram_clients_use_transport_helper():
    collector = (ROOT / "worker" / "telegram_supabase.py").read_text(encoding="utf-8")
    login = (ROOT / "worker" / "telegram_qr_login.py").read_text(encoding="utf-8")
    for text in (collector, login):
        assert "telethon_proxy" in text
        assert "proxy=telethon_proxy()" in text


def test_tor_wrapper_waits_for_full_bootstrap():
    text = (ROOT / "worker" / "run_with_tor.py").read_text(encoding="utf-8")
    assert "Bootstrapped 100%" in text
    assert "SocksPort 127.0.0.1:9050" in text
    assert "127.0.0.1:9050" in text
