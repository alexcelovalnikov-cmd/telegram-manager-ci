"""Local server backup with atomic publication and bounded retention."""
import datetime
import os
from pathlib import Path
import subprocess
import sqlite3
import tarfile
import tempfile
os.umask(0o077)
root=Path('/opt/telegram-manager/backups')
root.mkdir(mode=0o700, exist_ok=True)
stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
final=root/('telegram-manager-'+stamp+'.dump')
part=final.with_suffix('.partial')
try:
    with part.open('xb') as out:
        subprocess.run(['docker','exec','telegram-manager-db-1','pg_dump','-U','postgres','-d','telegram_manager','-Fc','--no-owner','--no-acl'],stdout=out,check=True)
        out.flush(); os.fsync(out.fileno())
    subprocess.run(['docker','exec','-i','telegram-manager-db-1','pg_restore','--list'],stdin=part.open('rb'),stdout=subprocess.DEVNULL,check=True)
    part.replace(final)
    latest=root/'latest.dump'
    staging=root/'latest.next'
    staging.unlink(missing_ok=True)
    os.link(final,staging)
    staging.replace(latest)
    for old in sorted(root.glob('telegram-manager-*.dump'),reverse=True)[14:]:
        old.unlink()
    # Snapshot SQLite through its backup API; copying a live WAL file is unsafe.
    with tempfile.TemporaryDirectory(dir=root) as temporary:
        temp=Path(temporary)
        for name in ('audit.sqlite','jobs.sqlite'):
            source=Path('/opt/telegram-manager/state')/name
            with sqlite3.connect('file:'+str(source)+'?mode=ro',uri=True) as src, sqlite3.connect(temp/name) as dst:
                src.backup(dst)
        bundle=root/('telegram-manager-'+stamp+'.tar.gz')
        pending=bundle.with_suffix('.partial')
        with tarfile.open(pending,'w:gz') as archive:
            archive.add(final,arcname='database.dump')
            for name in ('database.env','postgrest.env','api-v20.env','mac-sync.key'):
                archive.add('/opt/telegram-manager/secrets/'+name,arcname='secrets/'+name)
            archive.add('/opt/telegram-manager/state/oauth.json',arcname='state/oauth.json')
            for name in ('audit.sqlite','jobs.sqlite'):
                archive.add(temp/name,arcname='state/'+name)
        pending.replace(bundle)
        link=root/'latest-bundle.next'; link.unlink(missing_ok=True)
        os.link(bundle,link); link.replace(root/'latest.tar.gz')
        for old in sorted(root.glob('telegram-manager-*.tar.gz'),reverse=True)[14:]: old.unlink()
    print('Database and service recovery backup completed')
except Exception:
    part.unlink(missing_ok=True)
    raise SystemExit('Database backup failed') from None
