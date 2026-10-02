"""OAuth discovery and single-owner pairing; owner approval never appears as an MCP tool."""
import html
import json
from urllib.parse import parse_qs
from starlette.responses import JSONResponse, HTMLResponse, RedirectResponse
from starlette.routing import Route
from .oauth_pairing import ServiceError, SCOPES

PUBLIC_PATHS = frozenset({'/.well-known/oauth-protected-resource',
    '/.well-known/oauth-protected-resource/mcp', '/.well-known/oauth-authorization-server',
    '/oauth/register', '/oauth/authorize', '/oauth/complete', '/oauth/token'})


def routes(auth, record):
    def response(data, status=200):
        return JSONResponse(data, status_code=status, headers={'Cache-Control':'no-store'})

    async def resource(request):
        return response({'resource': auth.resource, 'authorization_servers': [auth.issuer],
                         'scopes_supported': sorted(SCOPES)})

    async def metadata(request):
        return response({'issuer':auth.issuer, 'authorization_endpoint':auth.issuer+'/oauth/authorize',
            'token_endpoint':auth.issuer+'/oauth/token', 'registration_endpoint':auth.issuer+'/oauth/register',
            'authorization_response_iss_parameter_supported':True,
            'response_types_supported':['code'], 'grant_types_supported':['authorization_code','refresh_token'],
            'code_challenge_methods_supported':['S256'], 'token_endpoint_auth_methods_supported':['none'],
            'scopes_supported':sorted(SCOPES)})

    async def register(request):
        data = await request.json()
        if not isinstance(data, dict): raise ServiceError('invalid_client_metadata',400)
        result = auth.register(data)
        record('oauth_register','ok')
        return response(result,201)

    def waiting(rid):
        return HTMLResponse('''<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Telegram Manager</title>
<style>body{background:#141414;color:#eee;font:17px system-ui;max-width:640px;margin:12vh auto;padding:24px}a{color:#0a84ff}code{word-break:break-all}</style>
<h1>Подключение Telegram Manager</h1><p>Ожидается разрешение владельца на доступ к выбранным чатам, проектам и операциям сверки.</p>
<p>Код подключения: <code>''' + html.escape(rid) + '''</code></p>
<p>Передайте этот код в текущий чат Codex. Ключи и пароли вводить не нужно.</p>
<p><a href="''' + auth.issuer + '/oauth/complete?request_id=' + rid + '''">Проверить подключение</a></p></html>''',
            headers={'Cache-Control':'no-store','Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",'Referrer-Policy':'no-referrer'})

    async def authorize(request):
        params=dict(request.query_params)
        if any(len(v)>2048 for v in params.values()): raise ServiceError('invalid_request',400)
        rid,cookie=auth.start(params)
        result=waiting(rid)
        result.set_cookie('__Secure-tm_pair',cookie,max_age=600,secure=True,httponly=True,samesite='lax',path='/telegram-manager/oauth')
        record('oauth_authorize','pending')
        return result

    async def complete(request):
        rid=request.query_params.get('request_id','')
        target=auth.complete(rid,request.cookies.get('__Secure-tm_pair'))
        if not target: return waiting(rid)
        record('oauth_authorize','completed')
        return RedirectResponse(target,status_code=303,headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})

    async def token(request):
        if request.headers.get('content-type','').split(';')[0]!='application/x-www-form-urlencoded':
            raise ServiceError('invalid_request',400)
        form=parse_qs((await request.body()).decode(),keep_blank_values=True)
        if any(len(v)!=1 for v in form.values()): raise ServiceError('invalid_request',400)
        result=auth.exchange({k:v[0] for k,v in form.items()})
        record('oauth_token','ok')
        return response(result)

    async def pending(request):
        return response(auth.pending(request.path_params['request_id']))

    async def approve(request):
        data=await request.json()
        current=auth.pending(request.path_params['request_id'])
        if data!={k:current[k] for k in ('client_id','resource','scope')}:
            raise ServiceError('pairing_details_changed',409)
        result=auth.approve(request.path_params['request_id'])
        record('oauth_owner_approve','ok')
        return response(result)

    async def revoke(request):
        auth.revoke_all(); record('oauth_revoke','ok')
        return response({'revoked':True})

    def safe(fn):
        async def handler(request):
            try: return await fn(request)
            except ServiceError as exc: return response({'error':exc.code},exc.status)
            except (ValueError,TypeError,UnicodeError): return response({'error':'invalid_request'},400)
            except Exception: return response({'error':'authorization_unavailable'},503)
        return handler
    specs=[('/.well-known/oauth-protected-resource',resource,['GET']),
           ('/.well-known/oauth-protected-resource/mcp',resource,['GET']),
           ('/.well-known/oauth-authorization-server',metadata,['GET']),
           ('/oauth/register',register,['POST']),('/oauth/authorize',authorize,['GET']),
           ('/oauth/complete',complete,['GET']),('/oauth/token',token,['POST']),
           ('/oauth-owner/pairings/{request_id}',pending,['GET']),
           ('/oauth-owner/pairings/{request_id}/approve',approve,['POST']),
           ('/oauth-owner/revoke',revoke,['POST'])]
    return [Route(path,safe(fn),methods=methods) for path,fn,methods in specs]
