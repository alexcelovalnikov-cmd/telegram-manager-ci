import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from copy import deepcopy
import httpx
import pytest
from starlette.testclient import TestClient
from tm_api.app import create_app
from tm_api.config import Config
from tm_api.backend import Backend, Unavailable
from tm_api.security import Audit, sanitize
from tm_api.service import Service, READ_TOOLS, required_mac_services_available


TOKEN = "independent-api-test-key"
SECRET = "database-secret-test-value"
HEADERS = {"Authorization": "Bearer " + TOKEN}


class Fake:
    def __init__(self):
        self.calls = []
        self.offline = False
        self.old = False
        self.tasks = [{"id": 1, "context_group_id": 7, "title": "10.20 RCC - 40к ⚡️",
                       "description": "Ручная заметка", "project_data": {"amount_rub": "40000.00"},
                       "status": "completed", "record_kind": "project", "payment_status": "awaiting",
                       "project_archived_at": "2026-09-01T00:00:00Z"}]

    async def rows(self, table, **query):
        self.calls.append((table, query))
        if self.offline:
            raise Unavailable("sensitive URL " + SECRET)
        if table == "telegram_chat_groups":
            return [{"id": 7}]
        if table == "tasks":
            return deepcopy(self.tasks)
        if table == "integration_health":
            return [{"service_name": n, "status": "running", "last_heartbeat_at":
                     "2000-01-01T00:00:00Z" if self.old else datetime.now(timezone.utc).isoformat(),
                     "last_success_at": None, "details": {"version": "V18", "secret": SECRET}}
                    for n in ["apple-sync", "telegram-collector"]]
        return []

    async def rpc(self, name, args):
        self.calls.append((name, args))
        if self.offline:
            raise Unavailable(SECRET)
        if name == "tm_personal_contract_v18":
            return {"client": 18, "core": 16, "operating_contract": {"revision": 18}}
        if name == "tm_review_focus_get_v18":
            return {"version": 5, "review_id": None, "current": False}
        if name == "tm_review_bundle_v18":
            return {"due": [], "focus": {"version": 5}, "unsafe": SECRET}
        return {}

    async def close(self):
        pass


@pytest.fixture
def setup(tmp_path):
    config = Config("https://database.example.invalid", SECRET,
                    hashlib.sha256(TOKEN.encode()).hexdigest(), allowed_hosts=("testserver",),
                    audit_path=str(tmp_path / "audit.sqlite"))
    return config, Fake()


def test_auth_required_on_every_surface(setup):
    config, backend = setup
    with TestClient(create_app(config, backend)) as c:
        for path in ["/health", "/mcp", "/api/list_projects"]:
            assert c.post(path, json={}).status_code == 401
            assert c.post(path, json={}, headers={"Authorization": "Bearer " + SECRET}).status_code == 401
    assert not backend.calls


def test_origin_and_host_rejected(setup):
    config, backend = setup
    with TestClient(create_app(config, backend)) as c:
        assert c.get("/health", headers=HEADERS | {"Origin": "https://attacker.example"}).status_code == 403
        assert c.get("/health", headers=HEADERS | {"Host": "attacker.example"}).status_code == 400
    assert not backend.calls


def test_rate_limit_separate_from_failed_auth(setup):
    config, backend = setup
    with TestClient(create_app(replace(config, requests_per_minute=2), backend)) as c:
        for _ in range(2):
            assert c.get("/health").status_code == 401
        assert c.get("/health").status_code == 429
        assert c.get("/health", headers=HEADERS).status_code == 200
        assert c.get("/health", headers=HEADERS).status_code == 200
        assert c.get("/health", headers=HEADERS).status_code == 429


@pytest.mark.parametrize("body", [{"limit": 0}, {"limit": 101}, {"limit": "5"}, {"after_id": -1},
                                    {"sql": "delete from tasks"}, {"instance_id": "other"},
                                    {"include_archived": "false"}, {"limit": True}])
def test_rest_validation(setup, body):
    config, backend = setup
    with TestClient(create_app(config, backend)) as c:
        assert c.post("/api/list_projects", json=body, headers=HEADERS).status_code == 422
    assert not backend.calls


def test_bound_body_and_no_write_routes(setup):
    config, backend = setup
    with TestClient(create_app(config, backend)) as c:
        assert c.post("/api/list_projects", content="x" * 32769, headers=HEADERS).status_code == 413
        for name in ["execute_sql", "rpc", "record_payment", "set_review_focus", "answer_review_question"]:
            assert c.post("/api/" + name, json={}, headers=HEADERS).status_code == 404
    assert not backend.calls


def test_projects_are_unchanged_including_archive_and_manual_notes(setup):
    config, backend = setup
    before = deepcopy(backend.tasks)
    with TestClient(create_app(config, backend)) as c:
        result = c.post("/api/list_projects", json={}, headers=HEADERS).json()
    assert result["items"] == before == backend.tasks
    query = next(q for n, q in backend.calls if n == "tasks")
    assert "project_archived_at" not in query
    assert query["context_group_id"] == "in.(7)"


def test_project_cannot_be_read_outside_owner_scope(setup):
    config, backend = setup
    backend.tasks[0]["context_group_id"] = 99
    with TestClient(create_app(config, backend)) as c:
        assert c.post("/api/get_project", json={"project_id": 1}, headers=HEADERS).status_code == 404


def test_missing_mac_does_not_disable_api(setup):
    config, backend = setup
    backend.old = True
    with TestClient(create_app(config, backend)) as c:
        r = c.get("/health", headers=HEADERS)
        assert r.status_code == 200
        assert r.json()["mac_available"] is False
        assert c.post("/api/list_projects", json={}, headers=HEADERS).status_code == 200


def test_server_storage_does_not_require_legacy_apple_sync_for_mac_health():
    services=[
        {"service_name":"apple-sync","status":"stopped","fresh":False},
        {"service_name":"telegram-collector","status":"running","fresh":True},
    ]
    assert required_mac_services_available(services,False) is True
    assert required_mac_services_available(services,True) is False


def test_db_outage_is_not_success_and_recovers(setup):
    config, backend = setup
    with TestClient(create_app(config, backend)) as c:
        backend.offline = True
        r = c.get("/health", headers=HEADERS)
        assert r.status_code == 503 and SECRET not in r.text
        backend.offline = False
        assert c.get("/health", headers=HEADERS).status_code == 200


def test_focus_reads_do_not_advance_or_mark_shown(setup):
    config, backend = setup
    with TestClient(create_app(config, backend)) as c:
        for _ in range(2):
            r = c.post("/api/get_current_review_question", json={}, headers=HEADERS)
            assert r.json()["focus"]["version"] == 5
        r = c.post("/api/get_review_bundle", json={}, headers=HEADERS)
        assert SECRET not in r.text
    assert all("set" not in n and "reply" not in n and "shown" not in n for n, _ in backend.calls)
    assert backend.calls[-1][1]["p_explicit"] is False


def test_audit_survives_restart_and_never_contains_data(setup):
    config, backend = setup
    for _ in range(2):
        with TestClient(create_app(config, backend)) as c:
            c.post("/api/get_project", json={"project_id": 1}, headers=HEADERS)
    db = sqlite3.connect(config.audit_path)
    rows = db.execute("SELECT * FROM events").fetchall()
    assert len(rows) == 4
    assert SECRET not in str(rows) and TOKEN not in str(rows) and "RCC" not in str(rows)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM events")
    db.close()


def test_mcp_initialize_tools_and_call(setup):
    config, backend = setup
    headers = HEADERS | {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}
    with TestClient(create_app(config, backend)) as c:
        def rpc(method, params=None):
            return c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, headers=headers)
        r = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})
        assert r.status_code == 200, r.text
        tools = rpc("tools/list").json()["result"]["tools"]
        assert set(t["name"] for t in tools) == set(READ_TOOLS)
        assert all(t["annotations"]["readOnlyHint"] for t in tools)
        r = rpc("tools/call", {"name": "list_projects", "arguments": {}})
        assert "40000.00" in r.text, r.text
        assert SECRET not in r.text
        r = rpc("tools/call", {"name": "execute_sql", "arguments": {"sql": "select 1"}})
        assert r.json()["result"]["isError"] is True
        assert rpc("tools/call", {"name": "list_projects", "arguments": {"limit": 101}}).json()["result"]["isError"] is True
        for args in [{"sql": "select 1"}, {"limit": "10"}, {"instance_id": "other"}]:
            assert rpc("tools/call", {"name": "list_projects", "arguments": args}).json()["result"]["isError"] is True


@pytest.mark.asyncio
async def test_backend_performs_only_allowlisted_get(setup):
    config, _ = setup
    seen = []
    async def handler(request):
        seen.append(request)
        return httpx.Response(200, json={})
    backend = Backend(config, httpx.MockTransport(handler))
    await backend.rpc("tm_review_bundle_v18", {"p_explicit": False, "p_instance_id": config.instance_id})
    assert seen[0].method == "GET"
    with pytest.raises(ValueError):
        await backend.rpc("tm_record_payment_v14", {})
    with pytest.raises(ValueError):
        await backend.rows("telegram_messages")
    await backend.close()


def test_secret_redaction():
    assert sanitize({"service_role_key": "s", "nested": [SECRET], "source_token": "evidence-hash"}, (SECRET,)) == {
        "nested": ["[redacted]"], "source_token": "evidence-hash"}
