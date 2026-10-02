"""Create independent least-privilege runtime logins. Run locally as DB administrator.

Only writes two private environment files, never outputs their values. Supply the
administrator DSN via TM_PROVISION_ADMIN_DSN, not a CLI argument. Not a deployment.
"""
import argparse,os,secrets
from pathlib import Path
from urllib.parse import quote
import psycopg
from psycopg import sql

def main():
    p=argparse.ArgumentParser();p.add_argument('--directory',required=True);p.add_argument('--database',required=True);p.add_argument('--runtime-host',default='db');p.add_argument('--confirm',action='store_true');args=p.parse_args()
    if not args.confirm:raise SystemExit('Explicit --confirm is required for credential provisioning')
    directory=Path(args.directory)
    directory.mkdir(mode=0o700,parents=True,exist_ok=False)
    passwords={kind:secrets.token_urlsafe(40) for kind in ('api','calendar')}
    suffix=secrets.token_hex(4);users={kind:'tm_v24_'+kind+'_'+suffix for kind in passwords}
    with psycopg.connect(os.environ['TM_PROVISION_ADMIN_DSN']) as conn:
        if conn.execute('SELECT current_database()').fetchone()[0]!=args.database:raise SystemExit('Wrong database')
        for kind,password in passwords.items():
            conn.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD {}').format(sql.Identifier(users[kind]),sql.Literal(password)))
            conn.execute(sql.SQL('GRANT {} TO {}').format(sql.Identifier('tm_v24_'+kind),sql.Identifier(users[kind])))
            dsn=f'postgresql://{users[kind]}:{quote(password,safe="")}@{args.runtime_host}:5432/{quote(args.database,safe="")}'
            key='TM_V24_DSN' if kind=='api' else 'TM_CALENDAR_DSN'
            fd=os.open(directory/(kind+'.env'),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as f:f.write(key+'='+dsn+'\n')
    print('Runtime credentials provisioned. Values were not printed. Store this directory outside the repository.')
if __name__=='__main__':
    try:main()
    except Exception:raise SystemExit('Provisioning failed; inspect locally without posting secrets') from None
