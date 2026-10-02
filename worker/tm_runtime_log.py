"""Bounded private error outbox. Never stores exception strings or HTTP bodies.

Only local state transition metadata is queued. Retry sends events, never retries
a business mutation. Server deduplicates UUIDs. Spool survives process restarts.
"""
from __future__ import annotations
import fcntl
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

MAX_EVENTS = 256
SERVICES = {'apple-sync', 'telegram-collector'}
ERROR_TYPES = {'ConnectError','ConnectionError','ConnectionRefusedError','ConnectionResetError',
               'TimeoutError','ReadTimeout','ConnectTimeout','WriteTimeout','PoolTimeout',
               'RuntimeError','ValueError','OSError','APIError','HTTPStatusError','PermissionError'}


def error_kind(exc: BaseException) -> str:
    name = type(exc).__name__
    return name if name in ERROR_TYPES else 'RuntimeFailure'


class RuntimeOutbox:
    def __init__(self, directory: Path, service: str, instance: str):
        if service not in SERVICES:
            raise ValueError('invalid_service')
        self.directory = Path(directory)
        self.service, self.instance = service, instance
        self.path = self.directory / (service + '.json')

    def _operate(self, fn):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink() or self.path.is_symlink():
            raise RuntimeError('unsafe_error_outbox_path')
        os.chmod(self.directory, 0o700)
        lock = self.directory / (self.service + '.lock')
        fd = os.open(lock, os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'r+') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            state = {'events': [], 'current_error': None, 'dropped': 0}
            if self.path.exists():
                if self.path.stat().st_size > 400000:
                    raise RuntimeError('error_outbox_too_large')
                state = json.loads(self.path.read_text(encoding='utf-8'))
                if not isinstance(state, dict) or not isinstance(state.get('events'), list):
                    raise RuntimeError('invalid_error_outbox')
            before = json.dumps(state,sort_keys=True)
            value = fn(state)
            if len(state['events']) > MAX_EVENTS:
                state['dropped'] = int(state.get('dropped',0)) + len(state['events']) - MAX_EVENTS
                state['events'] = state['events'][-MAX_EVENTS:]
            if before == json.dumps(state,sort_keys=True): return value
            tmpfd, tmp = tempfile.mkstemp(prefix='.outbox-', dir=self.directory)
            try:
                os.fchmod(tmpfd, 0o600)
                with os.fdopen(tmpfd, 'w', encoding='utf-8') as stream:
                    json.dump(state, stream, ensure_ascii=False, separators=(',', ':'))
                    stream.flush(); os.fsync(stream.fileno())
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp): os.unlink(tmp)
            return value

    def observe(self, exc: BaseException | None):
        if exc is None and not self.path.exists(): return None
        kind = error_kind(exc) if exc is not None else None
        def append(state):
            previous = state.get('current_error')
            if previous == kind: return
            state['current_error'] = kind
            state['events'].append({'event_id':str(uuid4()), 'observed_at':datetime.now(timezone.utc).isoformat(),
              'service_name':self.service, 'client_version':'V18', 'event_type':'error' if kind else 'recovered',
              'operation':'heartbeat_transport', 'error_kind':kind, 'previous_error_kind':previous})
        return self._operate(append)

    def flush(self, db):
        if not self.path.exists(): return 0
        # A bounded lock section prevents overlap with another recovery sender.
        def send(state):
            batch = state['events'][:32]
            if not batch: return 0
            response = db.rpc('tm_record_runtime_events_v18',{'p_instance_id':self.instance,'p_events':batch}).execute().data
            expected = {x['event_id'] for x in batch}
            if not isinstance(response,dict) or set(response.get('acknowledged_ids', [])) != expected:
                raise RuntimeError('runtime_event_ack_mismatch')
            state['events'] = [x for x in state['events'] if x['event_id'] not in expected]
            return len(batch)
        return self._operate(send)

    def status(self):
        if not self.path.exists(): return {'pending':0,'dropped':0}
        return self._operate(lambda s: {'pending':len(s['events']), 'dropped':s.get('dropped',0)})
