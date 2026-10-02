"""Separate native-agent credential; fixed PostgREST paths, never SQL or MCP access."""
import hashlib
import hmac
import json
import re
import time
from collections import deque
from pathlib import Path
from urllib.parse import parse_qsl
import httpx
from starlette.responses import JSONResponse, Response

RPC = {r['name']: frozenset(r['args']) for r in json.loads(
    Path(__file__).with_name('v18_rpc_requirements.json').read_text())}
RPC['tm_contract_v16'] = frozenset({'p_requirements'})
TABLES = {
    'tasks': {'GET', 'PATCH'},
    'integration_health': {'GET', 'POST'},
    'telegram_users': {'POST'},
    'telegram_chat_group_targets': {'POST', 'PATCH'},
    'source_exclusions': {'GET'},
    **{name: {'GET'} for name in ('tm_jobs_v11', 'tm_release_capabilities',
       'tm_review_items', 'tm_runtime_settings', 'tm_client_preferences',
       'telegram_messages', 'telegram_attachments')},
}

class SyncGateway:
    def __init__(self, app, config, backend, record, dynamic=None):
        self.app, self.config, self.backend, self.record = app, config, backend, record
        self.dynamic = dynamic
        self.accepted, self.rejected = deque(), deque()

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not scope['path'].startswith('/sync/'):
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope['headers']}
        async def fail(status, code):
            await JSONResponse({'code': code, 'message': 'Native sync request rejected',
                                'details': None, 'hint': None}, status_code=status,
                               headers={'Cache-Control': 'no-store'})(scope, receive, send)
        auth = headers.get('authorization', '')
        authorized = bool(self.config.sync_token_sha256 and auth.startswith('Bearer ') and
            hmac.compare_digest(hashlib.sha256(auth[7:].encode()).hexdigest(), self.config.sync_token_sha256))
        bucket = self.accepted if authorized else self.rejected
        now = time.monotonic()
        while bucket and bucket[0] <= now - 60:
            bucket.popleft()
        if len(bucket) >= (1200 if authorized else 60):
            return await fail(429, 'rate_limited')
        bucket.append(now)
        if not authorized:
            return await fail(401, 'native_authentication_required')
        if headers.get('host', '').split(':')[0] not in self.config.allowed_hosts or 'origin' in headers:
            return await fail(403, 'invalid_origin_or_host')
        prefix = '/sync/v1/'
        if not scope['path'].startswith(prefix):
            return await fail(404, 'unknown_operation')
        path = scope['path'][len(prefix):]
        method = scope['method']
        rpc_name = path[4:] if path.startswith('rpc/') else None
        if rpc_name:
            if rpc_name not in RPC or method != 'POST':
                return await fail(404, 'unknown_operation')
        elif path not in TABLES or method not in TABLES[path]:
            return await fail(404, 'unknown_operation')
        if len(scope.get('query_string', b'')) > 16384:
            return await fail(413, 'request_too_large')
        body = bytearray()
        while True:
            event = await receive()
            if event['type'] == 'http.disconnect':
                return
            body.extend(event.get('body', b''))
            if len(body) > 4 * 1024 * 1024:
                return await fail(413, 'request_too_large')
            if not event.get('more_body'):
                break
        if method != 'GET':
            try:
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise ValueError()
                if rpc_name and (set(payload) - RPC[rpc_name]):
                    raise ValueError()
                if 'p_instance_id' in payload and payload['p_instance_id'] != self.config.instance_id:
                    raise ValueError()
                if path == 'integration_health' and payload.get('instance_id') != self.config.instance_id:
                    raise ValueError()
                if path == 'tasks' and not dict(parse_qsl(scope.get('query_string', b'').decode())).get('id', '').startswith('eq.'):
                    raise ValueError()
                if path == 'telegram_chat_group_targets':
                    query = dict(parse_qsl(scope.get('query_string', b'').decode()))
                    if method == 'POST':
                        allowed = {'group_id','selector_kind','selector_value','display_name','chosen_chat_id','enabled'}
                        if set(payload) - allowed or type(payload.get('group_id')) is not int or payload.get('selector_kind') != 'title' or not str(payload.get('selector_value','')).startswith('dynamic:') or type(payload.get('chosen_chat_id')) is not int or payload.get('enabled') is not True:
                            raise ValueError()
                        group_id = payload['group_id']
                    else:
                        if set(payload) != {'enabled'} or payload.get('enabled') is not False or not query.get('id','').startswith('eq.'):
                            raise ValueError()
                        target_id = int(query['id'][3:])
                        targets = await self.backend.rows('telegram_chat_group_targets', select='id,group_id,selector_value', id='eq.'+str(target_id), limit='1')
                        if not targets or not str(targets[0].get('selector_value','')).startswith('dynamic:'):
                            return await fail(403, 'dynamic_source_target_forbidden')
                        group_id = int(targets[0]['group_id'])
                    groups = await self.backend.rows('telegram_chat_groups', select='id,rules_profile,reminder_list_instance_id', id='eq.'+str(group_id), reminder_list_instance_id='eq.'+self.config.instance_id, limit='1')
                    if not groups or groups[0].get('rules_profile') != 'postproduction':
                        return await fail(403, 'dynamic_source_target_forbidden')
            except (ValueError, UnicodeError):
                return await fail(422, 'invalid_arguments')
        if path == 'source_exclusions':
            if self.dynamic is None or not hasattr(self.dynamic, 'configuration'):
                return await fail(503, 'source_exclusions_unavailable')
            self.record('sync_source_exclusions', 'started')
            try:
                result = await self.dynamic.run(self.dynamic.configuration.read, 'source_exclusions')
            except Exception:
                self.record('sync_source_exclusions', 'unavailable')
                return await fail(503, 'source_exclusions_unavailable')
            self.record('sync_source_exclusions', 'ok')
            await JSONResponse([result], headers={'Cache-Control':'no-store'})(scope, receive, send)
            return
        forwarded = {k: headers[k] for k in ('prefer', 'range', 'range-unit', 'accept') if k in headers}
        # Profiles and authentication are never forwarded from the client.
        forwarded['content-type'] = 'application/json'
        self.record('sync_' + (rpc_name or path), 'started')
        try:
            response = await self.backend.http.request(method, path,
                params=parse_qsl(scope.get('query_string', b'').decode(), keep_blank_values=True),
                content=bytes(body) if body else None, headers=forwarded, timeout=30)
        except httpx.HTTPError:
            self.record('sync_' + (rpc_name or path), 'unavailable')
            return await fail(503, 'outcome_unknown_retry_same_arguments')
        if response.is_error:
            self.record('sync_' + (rpc_name or path), 'rejected')
            try:
                code = response.json().get('code', '')
            except ValueError:
                code = ''
            return await fail(response.status_code, code if re.fullmatch(r'[A-Za-z0-9_]{1,30}', code) else 'database_rejected')
        self.record('sync_' + (rpc_name or path), 'ok')
        result_headers = {k: response.headers[k] for k in ('content-type','content-range','range-unit','preference-applied') if k in response.headers}
        result_headers['cache-control'] = 'no-store'
        await Response(response.content, status_code=response.status_code, headers=result_headers)(scope, receive, send)
