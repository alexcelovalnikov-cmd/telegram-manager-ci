import hashlib
from dataclasses import replace
import httpx
from starlette.testclient import TestClient
from test_gateway import setup, HEADERS, SECRET
from tm_api.app import create_app
from tm_api.backend import Backend

MAC_TOKEN = 'mac-agent-test-token-distinct-from-owner'
MAC = {'Authorization': 'Bearer ' + MAC_TOKEN}

def make_client(setup):
    config, _ = setup
    config = replace(config, sync_token_sha256=hashlib.sha256(MAC_TOKEN.encode()).hexdigest())
    seen=[]
    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={'ok': True}, headers={'content-range':'0-0/1'})
    return TestClient(create_app(config, Backend(config, httpx.MockTransport(handle)))), seen

def test_separate_credentials_and_closed_paths(setup):
    client, seen=make_client(setup)
    with client as c:
        assert c.get('/sync/v1/tasks', headers=HEADERS).status_code == 401
        assert c.get('/health', headers=MAC).status_code == 401
        for path in ('rpc/sql', 'rpc/tm_api_execute_v19', 'tm_api_private.operations', 'secrets'):
            assert c.post('/sync/v1/'+path, headers=MAC,json={}).status_code == 404
        assert c.delete('/sync/v1/tasks', headers=MAC).status_code == 404
        assert c.get('/sync/v1/tasks',headers=MAC|{'Origin':'https://chatgpt.com'}).status_code == 403
    assert seen == []

def test_native_contract_and_scoped_requests(setup):
    client, seen=make_client(setup)
    with client as c:
        assert c.post('/sync/v1/rpc/tm_contract_v16',headers=MAC,json={'p_requirements':[]}).status_code == 200
        assert c.post('/sync/v1/rpc/tm_review_bundle_v18',headers=MAC,json={'p_instance_id':'other'}).status_code == 422
        assert c.post('/sync/v1/rpc/tm_review_bundle_v18',headers=MAC,json={'sql':'SELECT 1'}).status_code == 422
        assert c.patch('/sync/v1/tasks',headers=MAC,json={'status':'completed'}).status_code == 422
        assert c.patch('/sync/v1/tasks?id=eq.12&updated_at=eq.stamp',headers=MAC,json={'sync_error':None}).status_code == 200
        assert c.post('/sync/v1/integration_health',headers=MAC,json={'instance_id':'other'}).status_code == 422
    assert len(seen)==2
    assert all(r.headers['authorization'] == 'Bearer '+SECRET for r in seen)
    assert seen[1].url.params['updated_at']=='eq.stamp'

def test_native_payload_bound_and_no_forwarded_profiles(setup):
    client, seen=make_client(setup)
    with client as c:
        assert c.post('/sync/v1/rpc/tm_ingest_message_v10',headers=MAC,content=b'x'*(4*1024*1024+1)).status_code == 413
        r=c.get('/sync/v1/tasks',headers=MAC|{'Accept-Profile':'tm_api_private'})
        assert r.status_code==200 and r.headers['content-range']=='0-0/1'
    assert 'accept-profile' not in seen[0].headers
