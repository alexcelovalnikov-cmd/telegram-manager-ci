"""Production WSGI entrypoint with per-request context cleanup and safe failures."""
import os
from importlib.metadata import version
from radicale import config
from radicale.app import Application
from tm_api.v24.common import Rejected
from .context import credential,current

if version('radicale')!='3.8.1':
    raise RuntimeError('This storage adapter requires Radicale 3.8.1; run compatibility tests before changing it')
configuration=config.load([(os.environ.get('RADICALE_CONFIG','/etc/radicale/config'),False)])
radicale=Application(configuration)

def application(environ,start_response):
    # CalDAV writes use conditional requests; stale clients cannot blind-overwrite.
    path=environ.get('PATH_INFO','').strip('/').split('/')
    method=environ.get('REQUEST_METHOD','GET')
    match=environ.get('HTTP_IF_MATCH','')
    create=environ.get('HTTP_IF_NONE_MATCH','')=='*'
    conditional=bool(match and match!='*')
    if len(path)==3 and ((method=='PUT' and not (conditional or create)) or (method in ('DELETE','MOVE') and not conditional)):
        data=b'An explicit ETag precondition is required'
        start_response('428 Precondition Required',[('Content-Type','text/plain'),('Content-Length',str(len(data)))])
        return [data]
    try:
        length=int(environ.get('CONTENT_LENGTH') or 0)
    except (ValueError,TypeError):
        start_response('400 Bad Request',[('Content-Length','0')]);return []
    if length<0:
        start_response('400 Bad Request',[('Content-Length','0')]);return []
    if length>2097152:
        start_response('413 Content Too Large',[('Content-Length','0')]);return []
    auth=credential.set(None);ctx=current.set(None)
    status={};headers=[];chunks=[]
    def capture(code,h,exc_info=None):
        status['code']=code;headers[:]=h
        return chunks.append
    try:
        # Buffer the bounded DAV response until transactions have committed.
        # Never return success before PostgreSQL confirms its transaction.
        body=radicale(environ,capture)
        try:
            for part in body:chunks.append(part)
        finally:
            if hasattr(body,'close'):body.close()
        start_response(status['code'],headers)
        return chunks
    except Rejected as exc:
        data=exc.code.encode()
        start_response('403 Forbidden',[('Content-Type','text/plain; charset=utf-8'),('Content-Length',str(len(data)))])
        return [data]
    except Exception:
        data=b'Calendar service unavailable'
        start_response('503 Service Unavailable',[('Content-Type','text/plain'),('Content-Length',str(len(data)))])
        return [data]
    finally:
        current.reset(ctx);credential.reset(auth)
