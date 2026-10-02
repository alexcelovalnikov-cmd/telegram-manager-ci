"""Local administration only; passwords are read from the TTY, never CLI arguments."""
import argparse,getpass,os
from argon2 import PasswordHasher
from tm_api.v24.database import Database
from tm_api.v24.common import Rejected
from . import repository as repo

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    create=sub.add_parser('create-user');create.add_argument('username');create.add_argument('--instance')
    password=sub.add_parser('set-password');password.add_argument('username')
    for name in ('suspend-user','resume-user'):
        child=sub.add_parser(name);child.add_argument('username')
    member=sub.add_parser('set-member');member.add_argument('username');member.add_argument('--calendar',required=True);member.add_argument('--actor',required=True);member.add_argument('--role',choices=['owner','viewer','editor'],required=True);member.add_argument('--status',choices=['active','suspended','revoked'],default='active');member.add_argument('--revision',type=int,required=True)
    args=parser.parse_args();repo.username(args.username)
    hashed=None
    if args.command in ('create-user','set-password'):
        first=getpass.getpass('Новый отдельный пароль CalDAV (от 16 символов): ')
        if len(first)<16 or len(first)>1024 or first!=getpass.getpass('Повторите пароль: '):
            raise Rejected('password_length_or_confirmation_invalid')
        hashed=PasswordHasher().hash(first);del first
    db=Database(os.environ.get('TM_CALENDAR_ADMIN_DSN',''))
    with db.transaction() as tx:
        repo.lock(tx)
        if args.command=='create-user':
            if tx.one('SELECT username FROM tm_calendar.users WHERE username=%s',(args.username,)):raise Rejected('account_already_exists')
            tx.execute('INSERT INTO tm_calendar.users(username,instance_id,password_hash) VALUES(%s,%s,%s)',(args.username,args.instance,hashed))
        elif args.command=='set-member':
            repo.member(tx,args.actor,args.calendar,args.username,args.role,args.status,args.revision)
            print('Права изменены. Другие пользователи не затронуты.');return
        else:
            row=tx.one('SELECT username,active,revision FROM tm_calendar.users WHERE username=%s FOR UPDATE',(args.username,))
            if not row:raise Rejected('account_not_found')
            if args.command=='set-password':
                tx.execute('UPDATE tm_calendar.users SET password_hash=%s,revision=revision+1,updated_at=clock_timestamp() WHERE username=%s',(hashed,args.username))
            else:
                tx.execute('UPDATE tm_calendar.users SET active=%s,revision=revision+1,updated_at=clock_timestamp() WHERE username=%s',(args.command=='resume-user',args.username))
        repo.calendar_audit(tx,'local-admin',args.command,args.username,None,{'account_changed':True})
    print('Операция выполнена. Пароль и его хеш не выводятся.')

if __name__=='__main__':
    try:main()
    except (KeyboardInterrupt,EOFError):raise SystemExit(1)
    except Rejected as exc:raise SystemExit(exc.code) from None
    except Exception:raise SystemExit('Admin operation failed; no success was confirmed') from None
