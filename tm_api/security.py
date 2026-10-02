"""Authentication, fixed bounds, safe responses and durable metadata-only audit."""
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
import uuid
from collections import deque
from pathlib import Path
from starlette.responses import JSONResponse
from .service import WRITE_TOOLS as LEGACY_WRITE_TOOLS
from .v24.register import WRITE_NAMES
WRITE_TOOLS = LEGACY_WRITE_TOOLS + WRITE_NAMES


def sanitize(value, secrets=()):
    if isinstance(value, dict):
        return {k: sanitize(v, secrets) for k, v in value.items()
                if not re.search(r"password|authorization|api.?key|service_role|secret|session|claim_token|access_token|refresh_token|id_token|code_verifier|last_error$|sync_error$", k, re.I)}
    if isinstance(value, list):
        return [sanitize(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
        value = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[redacted]", value)
        value = re.sub(r"\bsb_secret_[A-Za-z0-9_-]+", "[redacted]", value)
        return value
    return value


class Audit:
    def __init__(self, path):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if p.is_symlink():
            raise ValueError("Unsafe audit path")
        self.db = sqlite3.connect(p)
        os.chmod(p, 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, stamp REAL NOT NULL, operation TEXT NOT NULL, outcome TEXT NOT NULL)")
        self.db.execute("CREATE TRIGGER IF NOT EXISTS no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append_only'); END")
        self.db.execute("CREATE TRIGGER IF NOT EXISTS no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'append_only'); END")
        self.db.commit()

    def record(self, operation, outcome):
        self.db.execute("INSERT INTO events VALUES (?,?,?,?)", (str(uuid.uuid4()), time.time(), operation, outcome))
        self.db.commit()


class Guard:
    def __init__(self, app, config, validators=None, oauth=None):
        self.app, self.config = app, config
        self.validators = validators or {}
        self.oauth = oauth
        self.accepted = deque()
        self.unauthenticated = deque()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        async def reject(code, reason):
            extra = {"Cache-Control": "no-store"}
            if code in (401, 403) and self.oauth:
                extra['WWW-Authenticate'] = 'Bearer resource_metadata="' + self.oauth.issuer + '/.well-known/oauth-protected-resource", scope="tm:read tm:write"'
            await JSONResponse({"error": reason}, status_code=code,
                               headers=extra)(scope, receive, send)
        host = headers.get("host", "").split(":")[0]
        if host not in self.config.allowed_hosts:
            return await reject(400, "invalid_host")
        oauth_origin = self.config.public_url.split('/telegram-manager')[0] if (self.oauth or getattr(self.config,'frameio_enabled',False)) else None
        if "origin" in headers and headers["origin"] not in (*self.config.allowed_origins, oauth_origin):
            return await reject(403, "invalid_origin")
        auth = headers.get("authorization", "")
        owner = auth.startswith("Bearer ") and hmac.compare_digest(
            hashlib.sha256(auth[7:].encode()).hexdigest(), self.config.token_sha256)
        bearer = auth[7:] if auth.startswith('Bearer ') else ''
        oauth_read = bool(self.oauth and self.oauth.verify(bearer,'tm:read'))
        oauth_write = bool(self.oauth and self.oauth.verify(bearer,'tm:write'))
        authenticated = owner or oauth_read or oauth_write
        from .oauth_routes import PUBLIC_PATHS
        from .frameio_routes import PUBLIC_PATHS as FRAMEIO_PUBLIC_PATHS
        public_auth = bool((self.oauth and scope['path'] in PUBLIC_PATHS) or
                           (getattr(self.config,'frameio_enabled',False) and scope['path'] in FRAMEIO_PUBLIC_PATHS))
        # Fixed-size, single-owner buckets; untrusted IPs cannot allocate memory.
        bucket = self.accepted if authenticated else self.unauthenticated
        now = time.monotonic()
        while bucket and bucket[0] <= now - 60:
            bucket.popleft()
        if len(bucket) >= self.config.requests_per_minute:
            return await reject(429, "rate_limited")
        bucket.append(now)
        if (scope['path'].startswith('/oauth-owner/') or scope['path'].startswith('/frameio-owner/')) and not owner:
            return await reject(401, 'owner_authentication_required')
        if not authenticated and not public_auth:
            return await reject(401, "authentication_required")
        if scope["method"] not in ("GET", "POST", "DELETE"):
            return await reject(405, "method_not_allowed")
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > 32768:
                return await reject(413, "request_too_large")
            if not message.get("more_body"):
                break
        if scope["path"] == "/mcp" and scope["method"] == "POST":
            try:
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    return await reject(400, "invalid_request")
                if payload.get("method") == "tools/call":
                    params = payload.get("params")
                    required_write = isinstance(params,dict) and params.get('name') in WRITE_TOOLS
                    if not owner and not (oauth_write if required_write else oauth_read):
                        return await reject(403,'insufficient_scope')
                    if not isinstance(params, dict) or params.get("name") not in self.validators:
                        reason = "unknown_operation"
                    else:
                        try:
                            self.validators[params["name"]].model_validate(params.get("arguments", {}))
                            reason = None
                        except ValueError:
                            reason = "invalid_arguments"
                    if reason:
                        await JSONResponse({"jsonrpc": "2.0", "id": payload.get("id"),
                            "result": {"isError": True, "content": [{"type": "text", "text": reason}]}})(scope, receive, send)
                        return
            except (ValueError, TypeError):
                return await reject(400, "invalid_request")
        if not owner and not public_auth and scope['path'] != '/mcp':
            operation = scope['path'].removeprefix('/api/')
            if not (oauth_write if operation in WRITE_TOOLS else oauth_read):
                return await reject(403,'insufficient_scope')
        sent = False
        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()
        async def safe_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message["headers"]) + [(b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")]
            await send(message)
        await self.app(scope, replay, safe_send)
