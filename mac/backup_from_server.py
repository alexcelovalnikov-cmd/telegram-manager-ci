"""Private off-server recovery copy through isolated Telegram Manager identity."""
import datetime
import gzip
import os
from pathlib import Path
import stat
import subprocess
import tarfile
import tempfile

MAX_COMPRESSED = 512 * 1024 * 1024
MAX_EXPANDED = 1024 * 1024 * 1024


def validate(path):
    if not 0 < path.stat().st_size <= MAX_COMPRESSED:
        raise ValueError('backup size outside limit')
    total = 0
    with gzip.open(path, 'rb') as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_EXPANDED:
                raise ValueError('expanded backup outside limit')
    names = set()
    with tarfile.open(path, 'r:gz') as archive:
        for member in archive:
            name = member.name
            if name.startswith('/') or '..' in Path(name).parts or member.issym() or member.islnk():
                raise ValueError('unsafe archive member')
            if not (member.isfile() or member.isdir()) or name in names:
                raise ValueError('unexpected archive member')
            names.add(name)
        required = {'database.dump', 'secrets/api-v20.env', 'state/oauth.json'}
        if not required.issubset(names):
            raise ValueError('incomplete recovery archive')
        member = archive.getmember('database.dump')
        if not member.isfile() or archive.extractfile(member).read(5) != b'PGDMP':
            raise ValueError('invalid database dump')


def fetch(root=None, run=subprocess.run):
    root = Path(root) if root is not None else Path.home()/'Library/Application Support/TelegramManagerAPI/backups'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('unsafe private backup directory')
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    fd, temporary = tempfile.mkstemp(prefix=stamp, suffix='.partial', dir=root)
    partial = Path(temporary)
    try:
        with os.fdopen(fd, 'wb') as output:
            run(['/usr/bin/ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20',
                 'telegram-server', 'tmctl', 'backup-export'], stdout=output,
                stderr=subprocess.DEVNULL, check=True, timeout=120)
            output.flush()
            os.fsync(output.fileno())
        validate(partial)
        target = root/(stamp+'.tar.gz')
        partial.replace(target)
        for old in sorted(root.glob('*.tar.gz'), reverse=True)[14:]:
            if old.is_file() and not old.is_symlink():
                old.unlink()
        return target
    finally:
        partial.unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        fetch()
        print('Private off-server backup verified')
    except Exception:
        raise SystemExit('Telegram Manager backup download failed; previous copies preserved') from None
