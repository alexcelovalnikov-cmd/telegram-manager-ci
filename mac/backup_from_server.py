"""Keep an off-server recovery copy on the owner's Mac; no database credentials."""
import datetime
import os
from pathlib import Path
import subprocess
import tarfile
os.umask(0o077)
root=Path.home()/'Library/Application Support/TelegramManagerAPI/backups'
root.mkdir(mode=0o700,parents=True,exist_ok=True)
stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
partial=root/(stamp+'.partial')
try:
    subprocess.run(['/usr/bin/scp','-q','-o','BatchMode=yes','-o','ConnectTimeout=20',
        '-F','/home/demo/Documents/GPT/Rent_Rabbit_Plugin/server-access/ssh_config',
        'rr-server:/opt/telegram-manager/backups/latest.tar.gz',str(partial)],check=True,timeout=120)
    with tarfile.open(partial,'r:gz') as archive:
        if archive.extractfile('database.dump').read(5)!=b'PGDMP': raise ValueError('Invalid backup')
        assert {'secrets/api-v20.env','state/oauth.json'}.issubset(archive.getnames())
    partial.chmod(0o600)
    partial.replace(root/(stamp+'.tar.gz'))
    for old in sorted(root.glob('*.tar.gz'),reverse=True)[14:]: old.unlink()
except Exception:
    partial.unlink(missing_ok=True)
    raise SystemExit('Telegram Manager backup download failed') from None
