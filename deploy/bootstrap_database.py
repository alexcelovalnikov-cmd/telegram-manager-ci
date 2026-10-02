"""One-time server bootstrap. Emits no credentials; never overwrites secrets."""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets

root = Path('/opt/telegram-manager/secrets')
paths = ['database.env','postgrest.env','api-v20.env','database-init.sql','mac-sync.key']
if any((root / name).exists() for name in paths):
    raise SystemExit('Existing database configuration; refusing to overwrite')
os.umask(0o077)
def save(name, data):
    (root/name).write_text(data)
    (root/name).chmod(0o600)
password, authenticator, jwt_secret, mac = [secrets.token_hex(32) for _ in range(4)]
def enc(obj):
    return base64.urlsafe_b64encode(json.dumps(obj,separators=(',',':')).encode()).rstrip(b'=')
head = enc({'alg':'HS256','typ':'JWT'}) + b'.' + enc({'role':'service_role','iss':'telegram-manager'})
token = (head + b'.' + base64.urlsafe_b64encode(hmac.new(jwt_secret.encode(),head,hashlib.sha256).digest()).rstrip(b'=')).decode()
save('database.env',f'POSTGRES_PASSWORD={password}\nPOSTGRES_DB=telegram_manager\nPOSTGRES_INITDB_ARGS=--locale-provider=icu --icu-locale=en-US --locale=en_US.utf8\n')
save('postgrest.env',f'PGRST_DB_URI=postgres://authenticator:{authenticator}@db:5432/telegram_manager\nPGRST_DB_SCHEMAS=public\nPGRST_DB_EXTRA_SEARCH_PATH=public,extensions\nPGRST_JWT_SECRET={jwt_secret}\nPGRST_DB_POOL=8\nPGRST_DB_MAX_ROWS=10000\nPGRST_LOG_LEVEL=crit\nPGRST_SERVER_HOST=0.0.0.0\n')
previous=[s for s in (root/'api.env').read_text().splitlines() if s and not s.startswith(('SUPABASE_','TM_DATABASE_','TM_SYNC_'))]
save('api-v20.env','\n'.join(previous)+f'\nTM_DATABASE_REST_URL=http://db-rest:3000\nTM_DATABASE_TOKEN={token}\nTM_SYNC_TOKEN_SHA256={hashlib.sha256(mac.encode()).hexdigest()}\n')
save('mac-sync.key',mac+'\n')
save('database-init.sql',f'''CREATE ROLE anon NOLOGIN;
CREATE ROLE authenticated NOLOGIN;
CREATE ROLE service_role NOLOGIN BYPASSRLS;
CREATE ROLE authenticator LOGIN NOINHERIT PASSWORD '{authenticator}';
GRANT service_role TO authenticator;
CREATE SCHEMA extensions;
CREATE EXTENSION pgcrypto WITH SCHEMA extensions;
CREATE EXTENSION "uuid-ossp" WITH SCHEMA extensions;
''')
print('Independent database and Mac credentials saved securely')
