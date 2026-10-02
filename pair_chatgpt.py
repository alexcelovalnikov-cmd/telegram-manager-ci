"""Owner-only OAuth pairing administration; never prints bearer tokens."""
import argparse
import json
from pathlib import Path
import httpx

parser=argparse.ArgumentParser()
parser.add_argument('action',choices=['inspect','approve','revoke'])
parser.add_argument('request_id',nargs='?')
args=parser.parse_args()
keyfile=Path.home()/'Library/Application Support/TelegramManagerAPI/api.key'
if keyfile.is_symlink() or keyfile.stat().st_mode & 0o077:
    raise SystemExit('Unsafe owner credential file')
base='https://service.example.invalid/telegram-manager/oauth-owner'
try:
    with httpx.Client(headers={'Authorization':'Bearer '+keyfile.read_text().strip()},timeout=20) as client:
        if args.action=='revoke':
            result=client.post(base+'/revoke')
        else:
            import re
            if not re.fullmatch('[a-f0-9]{32}',args.request_id or ''):
                raise ValueError('Invalid pairing code')
            url=base+'/pairings/'+args.request_id
            result=client.get(url)
            result.raise_for_status()
            if args.action=='approve':
                current=result.json()
                if current['resource']!='https://service.example.invalid/telegram-manager/mcp' or current['scope']!='tm:read tm:write':
                    raise ValueError('Unexpected pairing scope')
                result=client.post(url+'/approve',json={k:current[k] for k in ('client_id','resource','scope')})
        result.raise_for_status()
        print(json.dumps(result.json()))
except Exception:
    raise SystemExit('Pairing operation failed; no credentials displayed') from None
