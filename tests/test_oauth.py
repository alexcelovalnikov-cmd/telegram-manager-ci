from dataclasses import replace
from urllib.parse import parse_qs, urlsplit
import re
import pytest
from starlette.testclient import TestClient
from test_gateway import setup, HEADERS
from test_writes import PAYLOAD, write_backend
from tm_api.app import create_app
from tm_api.oauth_pairing import Pairing, CALLBACK, ServiceError, challenge

VERIFIER='v'*64


def grant(auth, scope='tm:read tm:write'):
    client=auth.register({'redirect_uris':[CALLBACK]})
    params={'client_id':client['client_id'],'redirect_uri':CALLBACK,'response_type':'code',
            'resource':auth.resource,'scope':scope,'code_challenge_method':'S256',
            'code_challenge':challenge(VERIFIER),'state':'test-state'}
    rid,cookie=auth.start(params)
    assert auth.complete(rid,cookie) is None
    auth.approve(rid)
    url=auth.complete(rid,cookie)
    q=parse_qs(urlsplit(url).query)
    assert q['iss']==[auth.issuer] and q['state']==['test-state']
    exchange={'grant_type':'authorization_code','client_id':client['client_id'],'code':q['code'][0],
              'redirect_uri':CALLBACK,'resource':auth.resource,'code_verifier':VERIFIER}
    return auth.exchange(exchange),exchange


def test_pkce_resource_scope_replay_restart_and_revocation(tmp_path):
    path=tmp_path/'oauth.json'
    now=[1000]
    auth=Pairing(path,'https://example.com/telegram-manager',lambda:now[0])
    tokens,exchange=grant(auth)
    assert auth.verify(tokens['access_token'],'tm:write')
    assert tokens['access_token'] not in path.read_text()
    assert tokens['refresh_token'] not in path.read_text()
    assert not path.stat().st_mode & 0o077
    with pytest.raises(ServiceError): auth.exchange(exchange)
    with pytest.raises(ServiceError): auth.exchange(exchange|{'resource':'https://attacker'})
    auth=Pairing(path,auth.issuer,lambda:now[0])
    assert auth.verify(tokens['access_token'],'tm:read')
    now[0]+=901
    assert not auth.verify(tokens['access_token'],'tm:read')
    refresh={'grant_type':'refresh_token','client_id':exchange['client_id'],'resource':auth.resource,'refresh_token':tokens['refresh_token']}
    with pytest.raises(ServiceError): auth.exchange(refresh|{'scope':'tm:admin'})
    rotated=auth.exchange(refresh)
    assert auth.verify(rotated['access_token'],'tm:write')
    with pytest.raises(ServiceError): auth.exchange(refresh)
    auth.revoke_all()
    assert not auth.verify(rotated['access_token'],'tm:read')
    with pytest.raises(ServiceError): auth.exchange(refresh|{'refresh_token':rotated['refresh_token']})


def test_invalid_callback_pkce_cookie_and_unapproved_request(tmp_path):
    auth=Pairing(tmp_path/'oauth.json','https://example.com/telegram-manager')
    with pytest.raises(ServiceError): auth.register({'redirect_uris':['https://attacker.example/cb']})
    client=auth.register({'redirect_uris':[CALLBACK]})
    p={'client_id':client['client_id'],'redirect_uri':CALLBACK,'response_type':'code','resource':auth.resource,
       'scope':'tm:read','code_challenge_method':'S256','code_challenge':challenge(VERIFIER)}
    with pytest.raises(ServiceError): auth.start(p|{'code_challenge_method':'plain'})
    with pytest.raises(ServiceError): auth.start(p|{'resource':'https://attacker'})
    rid,cookie=auth.start(p)
    with pytest.raises(ServiceError): auth.complete(rid,'wrong-cookie')
    assert auth.complete(rid,cookie) is None
    auth.approve(rid)
    code=parse_qs(urlsplit(auth.complete(rid,cookie)).query)['code'][0]
    with pytest.raises(ServiceError): auth.exchange({'grant_type':'authorization_code','client_id':client['client_id'],
        'redirect_uri':CALLBACK,'resource':auth.resource,'code':code,'code_verifier':'x'*64})


def test_http_discovery_pairing_and_scope_enforcement(setup,tmp_path):
    config,backend=setup
    write_backend(backend)
    config=replace(config,writes_enabled=True,oauth_enabled=True,public_url='https://testserver/telegram-manager',oauth_path=str(tmp_path/'oauth.json'))
    with TestClient(create_app(config,backend),base_url='https://testserver') as c:
        assert c.get('/.well-known/oauth-authorization-server').json()['code_challenge_methods_supported']==['S256']
        assert c.get('/.well-known/oauth-protected-resource').json()['resource']==config.public_url+'/mcp'
        assert 'resource_metadata' in c.get('/health').headers['www-authenticate']
        client=c.post('/oauth/register',json={'redirect_uris':[CALLBACK]}).json()['client_id']
        p={'client_id':client,'redirect_uri':CALLBACK,'response_type':'code','resource':config.public_url+'/mcp',
           'scope':'tm:read','code_challenge_method':'S256','code_challenge':challenge(VERIFIER)}
        page=c.get('/oauth/authorize',params=p)
        rid=re.search(r'<code>([a-f0-9]{32})</code>',page.text)[1]
        cookie=c.cookies.get('__Secure-tm_pair')
        assert 'HttpOnly' in page.headers['set-cookie'] and 'Secure' in page.headers['set-cookie']
        assert c.post('/oauth-owner/pairings/'+rid+'/approve',json={}).status_code==401
        pending=c.get('/oauth-owner/pairings/'+rid,headers=HEADERS).json()
        data={k:pending[k] for k in ('client_id','resource','scope')}
        assert c.post('/oauth-owner/pairings/'+rid+'/approve',json=data|{'scope':'tm:write'},headers=HEADERS).status_code==409
        assert c.post('/oauth-owner/pairings/'+rid+'/approve',json=data,headers=HEADERS).status_code==200
        # Reverse proxy strips /telegram-manager; emulate its cookie forwarding.
        redirect=c.get('/oauth/complete',params={'request_id':rid},headers={'Cookie':'__Secure-tm_pair='+cookie},follow_redirects=False)
        assert redirect.status_code==303
        code=parse_qs(urlsplit(redirect.headers['location']).query)['code'][0]
        result=c.post('/oauth/token',data={'grant_type':'authorization_code','client_id':client,'redirect_uri':CALLBACK,
             'resource':config.public_url+'/mcp','code_verifier':VERIFIER,'code':code})
        assert result.status_code==200,result.text
        headers={'Authorization':'Bearer '+result.json()['access_token']}
        assert c.get('/health',headers=headers).status_code==200
        assert c.post('/api/set_payment_window',json=PAYLOAD,headers=headers).status_code==403
        assert c.get('/oauth-owner/pairings/'+rid,headers=headers).status_code==401
        mcp_headers=headers|{'Accept':'application/json, text/event-stream'}
        assert c.post('/mcp',headers=mcp_headers,json={'jsonrpc':'2.0','id':1,'method':'tools/call',
            'params':{'name':'set_payment_window','arguments':PAYLOAD}}).status_code==403
        c.post('/oauth-owner/revoke',headers=HEADERS)
        assert c.get('/health',headers=headers).status_code==401
