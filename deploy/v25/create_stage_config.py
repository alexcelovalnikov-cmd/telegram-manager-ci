"""Create NEW local-only staging credentials. Never reads or copies production secrets."""
import base64,hashlib,hmac,json,os,secrets
from pathlib import Path

def b64(data):return base64.urlsafe_b64encode(data).rstrip(b'=')
def main():
    root=Path(__file__).resolve().parents[2];dest=root/'.staging'
    dest.mkdir(mode=0o700,exist_ok=False)
    def write(name,text):
        fd=os.open(dest/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:f.write(text+'\n')
    password=secrets.token_urlsafe(40);jwt_secret=secrets.token_urlsafe(48);owner=secrets.token_urlsafe(48)
    admin=f'postgresql://postgres:{password}@db:5432/tm_v24_test'
    header=b64(b'{"alg":"HS256","typ":"JWT"}');payload=b64(b'{"role":"service_role"}')
    body=header+b'.'+payload;jwt=(body+b'.'+b64(hmac.new(jwt_secret.encode(),body,hashlib.sha256).digest())).decode()
    api_password=secrets.token_urlsafe(40);calendar_password=secrets.token_urlsafe(40)
    api=f'postgresql://tm_v24_api_login:{api_password}@db:5432/tm_v24_test'
    calendar=f'postgresql://tm_v24_calendar_login:{calendar_password}@db:5432/tm_v24_test'
    write('logins.sql',"BEGIN;\nDO $$BEGIN IF current_database()<>'tm_v24_test' THEN RAISE EXCEPTION 'test_database_required';END IF;END$$;\n"
          +f"CREATE ROLE tm_v24_api_login LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD '{api_password}';\n"
          +f"CREATE ROLE tm_v24_calendar_login LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD '{calendar_password}';\n"
          +'GRANT tm_v24_api TO tm_v24_api_login;\nGRANT tm_v24_calendar TO tm_v24_calendar_login;\nCOMMIT;')
    write('database.env',f'POSTGRES_USER=postgres\nPOSTGRES_DB=tm_v24_test\nPOSTGRES_PASSWORD={password}')
    write('rest.env',f'PGRST_DB_URI={admin}\nPGRST_DB_SCHEMAS=public\nPGRST_DB_ANON_ROLE=anon\nPGRST_JWT_SECRET={jwt_secret}')
    write('api.env',f'TM_DATABASE_REST_URL=http://db-rest:3000\nTM_DATABASE_TOKEN={jwt}\nTM_API_TOKEN_SHA256={hashlib.sha256(owner.encode()).hexdigest()}\nTM_WRITES_ENABLED=true\nTM_DYNAMIC_ENABLED=true\nTM_CONFIGURABLE_ENABLED=true\nTM_V24_DSN={api}\nTM_OAUTH_ENABLED=false\nTM_SYNC_TOKEN_SHA256=\nTM_BACKGROUND_ENABLED=false\nTM_AUDIT_PATH=/state/audit.sqlite\nTM_ALLOWED_HOSTS=localhost,127.0.0.1\nTM_PUBLIC_URL=https://stage.invalid/telegram-manager')
    write('calendar.env',f'TM_CALENDAR_DSN={calendar}')
    write('tests.env',f'TM_V24_TEST_DSN={admin}\nTM_TEST_ALLOW_DATABASE_WRITES=YES\nTM_V24_TEST_API_DSN={api}\nTM_CALENDAR_DSN={calendar}\nRADICALE_CONFIG=/etc/radicale/config')
    write('owner.token',owner);state=dest/'api-state';state.mkdir(mode=0o700);os.chown(state,10001,10001)
    print('Изолированная .staging создана. Секреты не выведены. Только тестовая БД tm_v24_test; не используйте эти DSN в production.')
if __name__=='__main__':main()
