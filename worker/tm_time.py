"""Python 3.9-compatible ISO/Postgres timestamp parsing, without guessing zones."""
import re
from datetime import datetime, timezone

def parse_instant(value):
    if isinstance(value,datetime):
        dt=value
    else:
        if not isinstance(value,str) or len(value)>100:
            raise ValueError('invalid_timestamp')
        s=value.strip()
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}(?::?\d{2})?)',s):
            raise ValueError('timestamp_requires_date_time_and_offset')
        s=s.replace(' ','T').replace('Z','+00:00')
        s=re.sub(r'([+-]\d{2})$',r'\1:00',s)
        s=re.sub(r'([+-]\d{2})(\d{2})$',r'\1:\2',s)
        s=re.sub(r'\.(\d+)(?=[+-])',lambda m:'.'+m[1][:6].ljust(6,'0'),s)
        dt=datetime.fromisoformat(s)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError('timezone_required')
    return dt.astimezone(timezone.utc)
