"""Pure helpers, also tested without network, PostgreSQL, or the MCP SDK."""
import base64
import hashlib
import hmac
import json
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

class Rejected(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k,v in value.items()}
    if isinstance(value, (tuple, list)):
        return [clean(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    return value

def encoded(value):
    return json.dumps(clean(value), sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)

def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()

def diff(before, after, path=''):
    """RFC-6901 paths; arrays treated as indivisible values, null distinct from missing."""
    if isinstance(before, dict) and isinstance(after, dict):
        out=[]
        for key in sorted(before.keys() | after.keys()):
            p=path+'/'+str(key).replace('~','~0').replace('/','~1')
            if key not in before:
                out.append({'op':'add','path':p,'after':after[key]})
            elif key not in after:
                out.append({'op':'remove','path':p,'before':before[key]})
            else:
                out.extend(diff(before[key],after[key],p))
        return out
    if before == after and type(before) is type(after):
        return []
    return [{'op':'replace','path':path or '/', 'before':before,'after':after}]

def strict_fields(changes, allowed, required=()):
    if not isinstance(changes,dict) or set(changes)-set(allowed) or set(required)-set(changes):
        raise Rejected('invalid_or_protected_fields')
    if len(encoded(changes).encode()) > 28000:
        raise Rejected('patch_too_large')

def check_revision(actual, expected):
    if str(actual) != str(expected):
        # Existing V22 CAS values are timestamps, preserve their instant rather than formatting.
        try:
            a=datetime.fromisoformat(str(actual).replace('Z','+00:00'))
            e=datetime.fromisoformat(str(expected).replace('Z','+00:00'))
            if a.tzinfo is not None and e.tzinfo is not None and a==e:
                return
        except ValueError:
            pass
        raise Rejected('revision_conflict')

def make_cursor(data, secret):
    raw=base64.urlsafe_b64encode(encoded(data).encode()).rstrip(b'=')
    return raw.decode()+'.'+hmac.new(secret.encode(),raw,hashlib.sha256).hexdigest()

def read_cursor(cursor, secret, scope):
    try:
        raw, signature=cursor.rsplit('.',1)
        if not hmac.compare_digest(signature,hmac.new(secret.encode(),raw.encode(),hashlib.sha256).hexdigest()):
            raise ValueError()
        value=json.loads(base64.urlsafe_b64decode(raw+'='*((-len(raw))%4)))
        if value['scope']!=scope:
            raise ValueError()
        return value
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise Rejected('invalid_cursor') from None
