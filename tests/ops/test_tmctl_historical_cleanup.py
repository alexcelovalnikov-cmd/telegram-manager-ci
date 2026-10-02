from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
TMCTL_ROOT = REPO_ROOT / "deploy" / "ops" / "tmctl-root"


def _runner(tmp_path: Path, containers: dict[str, dict[str, str]]):
    state = tmp_path / "docker-state.json"
    state.write_text(json.dumps({"containers": containers}))
    preview = tmp_path / "historical.preview"
    config = tmp_path / "tmctl.env"
    config.write_text(
        "\n".join(
            [
                "TM_RELEASE_REPO=/tmp/release",
                "TM_RELEASE_COMPOSE=/tmp/compose.yml",
                "TM_PREVIOUS_COMPOSE=/tmp/previous.yml",
                "TM_API_IMAGE=telegram-manager:V90",
                "TM_CALENDAR_IMAGE=telegram-manager-calendar:V90",
                "TM_API_ENV_FILE=/tmp/api.env",
                "TM_API_DSN_FILE=/tmp/api-dsn.env",
                "TM_CALENDAR_ENV_FILE=/tmp/calendar.env",
                "TM_DATABASE_NETWORK=telegram-manager_database",
                "TM_STATE_DIR=/tmp/state",
                "TM_PROJECT_NAME=telegram-manager-v90-production",
                f"TM_HISTORICAL_CLEANUP_PREVIEW={preview}",
                f"TM_DEPLOY_LOCK={tmp_path / 'deploy.lock'}",
            ]
        )
        + "\n"
    )

    script = tmp_path / "tmctl-root"
    source = TMCTL_ROOT.read_text()
    source = source.replace(
        "CONFIG=/etc/telegram-manager/tmctl.env",
        f"CONFIG={config}",
        1,
    )
    script.write_text(source)
    script.chmod(0o755)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

path = os.environ["FAKE_DOCKER_STATE"]
data = json.loads(open(path).read())
containers = data["containers"]
args = sys.argv[1:]

if args[:1] == ["ps"]:
    for name in containers:
        print(name)
    raise SystemExit(0)

if args[:1] == ["inspect"]:
    name = args[1]
    if name not in containers:
        raise SystemExit(1)
    item = containers[name]
    if "--format" not in args:
        print("{}")
        raise SystemExit(0)
    fmt = args[args.index("--format") + 1]
    values = {
        "{{.State.Status}}": item["status"],
        "{{.Id}}": item["id"],
        "{{.Config.Image}}": item["image"],
    }
    if fmt not in values:
        raise SystemExit(2)
    print(values[fmt])
    raise SystemExit(0)

if args[:1] == ["rm"]:
    name = args[1]
    if name not in containers:
        raise SystemExit(1)
    del containers[name]
    with open(path, "w") as handle:
        json.dump(data, handle)
    print(name)
    raise SystemExit(0)

raise SystemExit(2)
"""
    )
    docker.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["FAKE_DOCKER_STATE"] = str(state)

    def run(*args: str):
        return subprocess.run(
            [str(script), *args],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    def read_state():
        return json.loads(state.read_text())["containers"]

    return run, read_state, preview


def _containers():
    return {
        "telegram-manager-v90-production-api-1": {
            "status": "exited",
            "id": "id-v90-api",
            "image": "telegram-manager:V90",
        },
        "telegram-manager-v90-production-calendar-1": {
            "status": "exited",
            "id": "id-v90-cal",
            "image": "telegram-manager-calendar:V90",
        },
        "telegram-manager-v89-production-api-1": {
            "status": "exited",
            "id": "id-v89-api",
            "image": "telegram-manager:V89",
        },
        "telegram-manager-v89-production-calendar-1": {
            "status": "exited",
            "id": "id-v89-cal",
            "image": "telegram-manager-calendar:V89",
        },
        "telegram-manager-v88-production-api-1": {
            "status": "running",
            "id": "id-v88-api",
            "image": "telegram-manager:V88",
        },
        "telegram-manager-v87-production-api-1": {
            "status": "exited",
            "id": "id-v87-api",
            "image": "telegram-manager:V87",
        },
        "telegram-manager-v87-production-calendar-1": {
            "status": "exited",
            "id": "id-v87-cal",
            "image": "telegram-manager-calendar:V87",
        },
        "telegram-manager-db-1": {
            "status": "running",
            "id": "id-db",
            "image": "postgres:17",
        },
    }


def test_preview_is_fail_closed_and_excludes_current_production(tmp_path):
    run, _, preview = _runner(tmp_path, _containers())
    result = run("historical-cleanup-preview")

    assert result.returncode == 0, result.stderr
    assert "count=2" in result.stdout
    assert "rollback_project=telegram-manager-v89-production" in result.stdout
    assert "telegram-manager-v87-production-api-1" in result.stdout
    assert "telegram-manager-v87-production-calendar-1" in result.stdout
    assert "telegram-manager-v89-production-api-1" not in result.stdout
    assert "telegram-manager-v89-production-calendar-1" not in result.stdout
    assert "telegram-manager-v90-production-api-1" not in result.stdout
    assert "telegram-manager-v88-production-api-1" not in result.stdout
    assert preview.exists()


def test_apply_consumes_exact_preview_and_removes_only_planned_containers(tmp_path):
    run, read_state, preview = _runner(tmp_path, _containers())
    planned = run("historical-cleanup-preview")
    digest = re.search(r"digest=([a-f0-9]{64})", planned.stdout).group(1)

    applied = run("historical-cleanup-apply", digest)

    assert applied.returncode == 0, applied.stderr
    assert "removed=2" in applied.stdout
    assert "rollback_project=telegram-manager-v89-production" in applied.stdout
    remaining = read_state()
    assert "telegram-manager-v87-production-api-1" not in remaining
    assert "telegram-manager-v87-production-calendar-1" not in remaining
    assert "telegram-manager-v89-production-api-1" in remaining
    assert "telegram-manager-v89-production-calendar-1" in remaining
    assert "telegram-manager-v90-production-api-1" in remaining
    assert "telegram-manager-v88-production-api-1" in remaining
    assert "telegram-manager-db-1" in remaining
    assert not preview.exists()


def test_apply_refuses_if_a_planned_container_changes_state(tmp_path):
    run, read_state, preview = _runner(tmp_path, _containers())
    planned = run("historical-cleanup-preview")
    digest = re.search(r"digest=([a-f0-9]{64})", planned.stdout).group(1)

    state = read_state()
    state["telegram-manager-v87-production-api-1"]["status"] = "running"
    state_path = Path(os.environ.get("UNUSED", str(tmp_path / "docker-state.json")))
    state_path.write_text(json.dumps({"containers": state}))

    applied = run("historical-cleanup-apply", digest)

    assert applied.returncode != 0
    assert "no longer exited" in applied.stderr
    remaining = read_state()
    assert "telegram-manager-v87-production-api-1" in remaining
    assert "telegram-manager-v87-production-calendar-1" in remaining
    assert "telegram-manager-v89-production-api-1" in remaining
    assert "telegram-manager-v89-production-calendar-1" in remaining
    assert preview.exists()


def test_preview_refuses_when_no_complete_rollback_release_exists(tmp_path):
    containers = _containers()
    del containers["telegram-manager-v89-production-calendar-1"]
    del containers["telegram-manager-v87-production-calendar-1"]
    run, _, preview = _runner(tmp_path, containers)

    result = run("historical-cleanup-preview")

    assert result.returncode != 0
    assert "no complete historical rollback release found" in result.stderr
    assert not preview.exists()
