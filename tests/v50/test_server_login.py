from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_qr_login_is_fixed_path_and_no_password_prompt():
    text = (ROOT / "worker" / "telegram_qr_login.py").read_text(encoding="utf-8")
    assert "/state/telegram_helper.session" in text
    assert "/state/login-qr.png" in text
    assert "QR_READY" in text
    assert "QR_LOGIN_OK" in text
    assert "QR_2FA_REQUIRED" in text
    assert "input(" not in text


def test_worker_runtime_contract_is_server_owned():
    text = (ROOT / "tm_api" / "service.py").read_text(encoding="utf-8")
    assert '"mac_required": apple_sync_required' in text
    assert '"production_runtime_available": runtime_available' in text
    assert '"telegram_collector_available": collector_available' in text
    assert '"runtime_location": details.get("runtime_location")' in text


def test_worker_model_mount_matches_linux_model_path():
    ctl = (ROOT / "deploy" / "ops" / "tmctl-root").read_text(encoding="utf-8")
    assert "TM_MODELS_DIR=/models" in ctl
    assert '$WORKER_ROOT/models:/models:ro' in ctl
