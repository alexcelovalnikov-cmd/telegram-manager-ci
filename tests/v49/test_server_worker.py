from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_worker_sources_compile():
    for path in (ROOT / "worker").glob("*.py"):
        compile(path.read_text(encoding="utf-8"), str(path), "exec")


def test_worker_image_is_server_only_and_non_root():
    text = (ROOT / "Dockerfile.worker").read_text(encoding="utf-8")
    assert "TM_SERVER_WORKER=1" in text
    assert "TM_MEDIA_BACKEND=linux" in text
    assert "USER 10001" in text
    assert "telegram_supabase.py" in text


def test_restricted_controller_has_fixed_worker_surface():
    text = (ROOT / "deploy/ops/tmctl-root").read_text(encoding="utf-8")
    for command in ("worker-login-start", "worker-login-status", "worker-import", "worker-enable", "worker-disable", "worker-status"):
        assert command in text
    assert 'WORKER_STAGE="${TM_WORKER_STAGE:-/opt/telegram-manager/staging/worker}"' in text
    assert "WORKER_ROOT=/opt/telegram-manager/runtime/worker" in text


def test_post_deploy_builds_same_tag_worker():
    text = (ROOT / "deploy/v31/post_deploy.sh").read_text(encoding="utf-8")
    assert 'worker_image="telegram-manager-worker:$release_name"' in text
    assert "Dockerfile.worker" in text
    assert "TM_WORKER_ENABLED=true" in text
