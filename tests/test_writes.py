from dataclasses import replace
import httpx
import pytest
from starlette.testclient import TestClient
from test_gateway import setup, HEADERS, SECRET
from tm_api.app import create_app
from tm_api.backend import Backend, GuardRejected, Unavailable
from tm_api.service import Service, WRITE_TOOLS

PAYLOAD = {'request_key': 'window-test-000001', 'change': {
    'project_id': 1, 'expected_updated_at': '2026-09-28T00:00:00Z', 'confirmed': True,
    'window': {'kind': 'relative', 'within_days': 7},
    'evidence': [{'kind': 'user', 'confirmation_ref': 'test-confirmation', 'statement': 'Within seven days'}]}}

RECON_PAYLOAD = {'request_key': 'recon-window-00001', 'change': {
    'project_id': 43, 'expected_updated_at': '2026-09-29T04:22:29Z',
    'window': {'kind': 'relative', 'within_days': 7},
    'evidence': [{'kind': 'telegram', 'chat_id': 266334332, 'message_id': 348580,
                  'content_token': 'token-vk-payment'}],
    'finding_key': 'reconciliation:266334332:348580:payment',
    'rule_key': 'payment_lightning_two_week_rule',
    'rationale': 'Дмитрий однозначно обещал оплату на этой неделе.'}}


def write_backend(backend, error=None):
    async def call(instance, key, operation, payload):
        backend.calls.append((operation, {'instance': instance, 'key': key, 'payload': payload}))
        if error:
            raise error
        return {'applied': True}
    backend.guarded_write = call


def test_strict_writes_and_owner_scope(setup):
    from copy import deepcopy
    config, backend = setup
    write_backend(backend)
    with TestClient(create_app(replace(config, writes_enabled=True), backend)) as c:
        assert c.post('/api/set_payment_window', json=PAYLOAD, headers=HEADERS).status_code == 200
        assert backend.calls[0][1]['instance'] == 'macbook-owner'
        assert backend.calls[0][1]['payload']['window']['within_days'] == 7
        for key, value in [('confirmed', 1), ('confirmed', 'true'), ('confirmed', False),
                           ('instance_id', 'other'), ('sql', 'update tasks'), ('project_id', '1')]:
            p = deepcopy(PAYLOAD)
            p['change'][key] = value
            assert c.post('/api/set_payment_window', json=p, headers=HEADERS).status_code == 422
        p = deepcopy(PAYLOAD)
        p['change']['evidence'] = []
        assert c.post('/api/set_payment_window', json=p, headers=HEADERS).status_code == 422
    assert len(backend.calls) == 1


@pytest.mark.asyncio
async def test_actual_record_payment_uses_direct_v25_compat_path(setup):
    config, backend = setup
    seen = []
    class Engine:
        def record_payment_compat(self, request_key, payload):
            seen.append((request_key, payload))
            return {'applied': True, 'compatibility': 'v25_direct_ledger'}
    class Dynamic:
        engine = Engine()
        async def run(self, fn, *args):
            return fn(*args)
    service = Service(backend, replace(
        config, writes_enabled=True, configurable_enabled=True))
    service.dynamic = Dynamic()
    payload = {'event': {'kind': 'received'}, 'confirmed': True}
    result = await service.record_payment('payment-route-test-0001', payload)
    assert result['compatibility'] == 'v25_direct_ledger'
    assert seen == [('payment-route-test-0001', payload)]
    assert backend.calls == []


def test_reconciliation_payment_window_uses_standing_policy_without_user_evidence(setup):
    config, backend = setup
    write_backend(backend)
    with TestClient(create_app(replace(config, writes_enabled=True), backend)) as c:
        r = c.post('/api/set_reconciliation_payment_window', json=RECON_PAYLOAD, headers=HEADERS)
        assert r.status_code == 200, r.text
        assert backend.calls[-1][0] == 'set_payment_window'
        payload = backend.calls[-1][1]['payload']
        assert payload['confirmed'] is True
        assert payload['evidence'][0]['kind'] == 'telegram'
        assert all(item['kind'] != 'user' for item in payload['evidence'])
        body = r.json()
        assert body['authorization'] == 'reconciliation_standing_policy'
        assert body['rule_key'] == 'payment_lightning_two_week_rule'
    bad = {**RECON_PAYLOAD, 'change': {**RECON_PAYLOAD['change'], 'evidence': [
        {'kind': 'user', 'confirmation_ref': 'not-allowed', 'statement': 'Подтверждаю'}]}}
    with TestClient(create_app(replace(config, writes_enabled=True), backend)) as c:
        assert c.post('/api/set_reconciliation_payment_window', json=bad, headers=HEADERS).status_code == 422


@pytest.mark.parametrize('error,status,outcome', [(GuardRejected(SECRET), 409, None), (Unavailable(SECRET), 503, 'unknown')])
def test_guard_and_timeout_are_not_reported_as_success(setup, error, status, outcome):
    config, backend = setup
    write_backend(backend, error)
    with TestClient(create_app(replace(config, writes_enabled=True), backend)) as c:
        r = c.post('/api/set_payment_window', json=PAYLOAD, headers=HEADERS)
        assert r.status_code == status and SECRET not in r.text
        assert r.json().get('outcome') == outcome
        if outcome:
            assert r.json()['retry_same_key_and_arguments'] is True


def test_mcp_writes_annotated_and_validated(setup):
    config, backend = setup
    write_backend(backend)
    headers = HEADERS | {'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-06-18'}
    with TestClient(create_app(replace(config, writes_enabled=True), backend)) as c:
        def rpc(method, params):
            return c.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}, headers=headers).json()
        rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}})
        tools = rpc('tools/list', {})['result']['tools']
        assert {t['name'] for t in tools if not t['annotations']['readOnlyHint']} == set(WRITE_TOOLS)
        r = rpc('tools/call', {'name': 'set_payment_window', 'arguments': PAYLOAD})
        assert not r['result'].get('isError'), r
        p = PAYLOAD | {'sql': 'select 1'}
        assert rpc('tools/call', {'name': 'set_payment_window', 'arguments': p})['result']['isError']
        answer = {'review_id': '00000000-0000-0000-0000-000000000001', 'review_revision': 1,
                  'expected_version': 1, 'action': 'resolved', 'user_text': 'Confirmed', 'confirmed': True}
        r = c.post('/api/answer_review_question', json={'request_key': 'answer-test-0001', 'answer': answer}, headers=HEADERS)
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_backend_write_is_one_fixed_rpc_and_hides_errors(setup):
    config, _ = setup
    seen = []
    async def handler(request):
        seen.append(request)
        return httpx.Response(400, json={'code': 'P0001', 'message': SECRET})
    backend = Backend(config, httpx.MockTransport(handler))
    with pytest.raises(GuardRejected):
        await backend.guarded_write(config.instance_id, 'request-00000001', 'set_payment_window', {})
    assert seen[0].method == 'POST' and seen[0].url.path == '/rpc/tm_api_execute_v22'
    with pytest.raises(ValueError):
        await backend.guarded_write(config.instance_id, 'request-00000001', 'execute_sql', {})
    assert len(seen) == 1
    await backend.close()
