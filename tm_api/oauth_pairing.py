"""Single-owner OAuth pairing for a personal Telegram Manager MCP server.

Every grant needs explicit local operator approval. No database credential is exposed.
State is stored atomically; bearer tokens/codes are stored only as SHA-256 hashes.
This is a development single-owner authorization server, not a multi-user IdP.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlencode
class ServiceError(Exception):
    def __init__(self, code, status):
        self.code, self.status = code.lower(), status
        super().__init__(self.code)


SCOPES={'tm:read','tm:write'}
CALLBACK='https://chatgpt.com/connector_platform_oauth_redirect'

def digest(s):return hashlib.sha256(s.encode()).hexdigest()
def challenge(s):return base64.urlsafe_b64encode(hashlib.sha256(s.encode()).digest()).rstrip(b'=').decode()

class Pairing:
    def __init__(self,path,issuer,clock=time.time):
        self.path=Path(path)
        if self.path.is_symlink(): raise ValueError('Unsafe OAuth state path')
        self.issuer=issuer.rstrip('/');self.resource=self.issuer+'/mcp';self.clock=clock
        self.lock=threading.RLock();self.failed=False
        self.state=json.loads(self.path.read_text()) if self.path.exists() else {'clients':{},'requests':{},'codes':{},'tokens':{},'grants':{}}

    def save(self):
        tmp=None
        try:
            self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            fd,tmp=tempfile.mkstemp(dir=self.path.parent,prefix='.mcp-auth-')
            with os.fdopen(fd,'w') as f:json.dump(self.state,f);f.flush();os.fsync(f.fileno())
            os.replace(tmp,self.path)
        except Exception:
            self.failed=True
            raise ServiceError('AUTH_STORAGE_UNAVAILABLE',503) from None
        finally:
            if tmp and os.path.exists(tmp):os.unlink(tmp)

    def clean(self):
        if self.failed:raise ServiceError("AUTH_STORAGE_UNAVAILABLE",503)
        now=self.clock()
        for table in ('requests','codes','tokens','grants'):
            self.state[table]={k:v for k,v in self.state[table].items() if v['expires']>now}
        self.state['clients']={k:v for k,v in self.state['clients'].items() if v['expires']>now}

    def register(self,data):
        with self.lock:
            self.clean()
            uris=data.get('redirect_uris')
            if uris!=[CALLBACK] or data.get('token_endpoint_auth_method','none')!='none':
                raise ServiceError('INVALID_CLIENT_METADATA',400)
            if len(self.state['clients'])>=100:raise ServiceError('CLIENT_LIMIT',429)
            cid=secrets.token_urlsafe(24)
            self.state['clients'][cid]={'redirect_uri':CALLBACK,'expires':self.clock()+86400*90}
            self.save()
            return {'client_id':cid,'redirect_uris':uris,'token_endpoint_auth_method':'none','grant_types':['authorization_code','refresh_token'],'response_types':['code']}

    def start(self,p):
        with self.lock:
            self.clean();client=self.state['clients'].get(p.get('client_id'))
            scopes=set(p.get('scope','tm:read').split())
            if (not client or p.get('redirect_uri')!=client['redirect_uri'] or p.get('response_type')!='code'
                or p.get('code_challenge_method')!='S256' or not re.fullmatch(r'[A-Za-z0-9_-]{43}',p.get('code_challenge',''))
                or p.get('resource')!=self.resource or not scopes or not scopes<=SCOPES
                or len(p.get('state',''))>2048):raise ServiceError('INVALID_AUTHORIZATION_REQUEST',400)
            if len(self.state['requests'])>=30:raise ServiceError('PAIRING_LIMIT',429)
            rid=secrets.token_hex(16);cookie=secrets.token_urlsafe(32)
            self.state['requests'][rid]={**p,'scope':' '.join(sorted(scopes)),'cookie':digest(cookie),'expires':self.clock()+600,'approved':False}
            self.save();return rid,cookie

    def approve(self,rid):
        with self.lock:
            self.clean();r=self.state['requests'].get(rid)
            if not r:raise ServiceError('PAIRING_NOT_FOUND',404)
            r['approved']=True;self.save()
            return {'request_id':rid,'client_id':r['client_id'],'resource':r['resource'],'scope':r['scope'],'approved':True}

    def pending(self,rid):
        with self.lock:
            self.clean();r=self.state['requests'].get(rid)
            if not r:raise ServiceError('PAIRING_NOT_FOUND',404)
            return {k:r[k] for k in ('client_id','resource','scope','expires','approved')}

    def complete(self,rid,cookie):
        with self.lock:
            self.clean();r=self.state['requests'].get(rid)
            if not r or not hmac.compare_digest(r['cookie'],digest(cookie or '')):raise ServiceError('INVALID_PAIRING_SESSION',403)
            if not r['approved']:return None
            code=secrets.token_urlsafe(32)
            self.state['codes'][digest(code)]={k:r[k] for k in ('client_id','redirect_uri','resource','scope','code_challenge')}
            self.state['codes'][digest(code)]['expires']=self.clock()+120
            del self.state['requests'][rid];self.save()
            return r['redirect_uri']+'?'+urlencode({'code':code,'state':r.get('state',''),'iss':self.issuer})

    def exchange(self,p):
        with self.lock:
            self.clean()
            if p.get('resource')!=self.resource:raise ServiceError('INVALID_TARGET',400)
            cid=p.get('client_id');grant=p.get('grant_type')
            if cid not in self.state['clients']:raise ServiceError('INVALID_CLIENT',400)
            if grant=='authorization_code':
                code_hash=digest(p.get('code',''));r=self.state['codes'].get(code_hash)
                verifier=p.get('code_verifier','')
                if (not r or r['client_id']!=cid or p.get('redirect_uri')!=r['redirect_uri'] or
                    not re.fullmatch(r'[A-Za-z0-9._~-]{43,128}',verifier) or not hmac.compare_digest(challenge(verifier),r['code_challenge'])):
                    raise ServiceError('INVALID_GRANT',400)
                del self.state['codes'][code_hash]
                gid=secrets.token_urlsafe(24);self.state['grants'][gid]={'active':True,'client_id':cid,'scope':r['scope'],'expires':self.clock()+86400*30}
            elif grant=='refresh_token':
                rh=digest(p.get('refresh_token',''));r=self.state['tokens'].get(rh)
                if not r or r['kind']!='refresh' or r['client_id']!=cid:raise ServiceError('INVALID_GRANT',400)
                gid=r['grant'];g=self.state['grants'].get(gid)
                if not g or not g['active'] or g['expires']<=self.clock():raise ServiceError('INVALID_GRANT',400)
                if p.get('scope') and p['scope']!=g['scope']:raise ServiceError('INVALID_SCOPE',400)
                del self.state['tokens'][rh]
            else:raise ServiceError('UNSUPPORTED_GRANT_TYPE',400)
            g=self.state['grants'][gid];access=secrets.token_urlsafe(32);refresh=secrets.token_urlsafe(32)
            for value,kind,expiry in [(access,'access',self.clock()+900),(refresh,'refresh',g['expires'])]:
                self.state['tokens'][digest(value)]={'kind':kind,'client_id':cid,'grant':gid,'resource':self.resource,'scope':g['scope'],'expires':expiry}
            self.save();return {'access_token':access,'refresh_token':refresh,'token_type':'Bearer','expires_in':900,'scope':g['scope']}

    def verify(self,token,scope):
        with self.lock:
            if self.failed:return False
            r=self.state['tokens'].get(digest(token));g=self.state['grants'].get(r['grant']) if r else None
            if not r or r['kind']!='access' or r['resource']!=self.resource or r['expires']<=self.clock() or not g or not g['active'] or g['expires']<=self.clock():return False
            return scope in r['scope'].split()

    def revoke_all(self):
        with self.lock:
            self.state['tokens']={};self.state['codes']={};self.state['requests']={};self.state['grants']={};self.save()
