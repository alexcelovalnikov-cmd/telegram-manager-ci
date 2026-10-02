import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('backup_copy', ROOT/'mac/backup_from_server.py')
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


def bundle(extra=None, omit=None):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w:gz') as archive:
        entries = {'database.dump': b'PGDMPsynthetic', 'secrets/api-v20.env': b'synthetic', 'state/oauth.json': b'{}'}
        entries.update(extra or {})
        for name, data in entries.items():
            if name == omit:
                continue
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return out.getvalue()


def test_isolated_transport_atomic_private_copy(tmp_path):
    root = tmp_path/'copies'
    def run(args, **kwargs):
        assert args[-3:] == ['telegram-server', 'tmctl', 'backup-export']
        assert '-F' not in args and kwargs['check'] and kwargs['timeout'] == 120
        kwargs['stdout'].write(bundle())
    saved = backup.fetch(root, run)
    assert saved.stat().st_mode & 0o777 == 0o600
    assert root.stat().st_mode & 0o777 == 0o700
    assert not list(root.glob('*.partial'))


@pytest.mark.parametrize('data', [bundle(omit='database.dump'), bundle(extra={'../escape': b'x'}), bundle()[:-12], b'not-gzip'])
def test_invalid_download_preserves_previous(tmp_path, data):
    root = tmp_path/'copies'
    root.mkdir(mode=0o700)
    old = root/'previous.tar.gz'
    old.write_bytes(bundle())
    def run(args, **kwargs):
        kwargs['stdout'].write(data)
    with pytest.raises(Exception):
        backup.fetch(root, run)
    assert old.read_bytes() == bundle()
    assert not list(root.glob('*.partial'))
    assert list(root.glob('*.tar.gz')) == [old]


def test_transport_failure_preserves_previous(tmp_path):
    root = tmp_path/'copies'
    root.mkdir(mode=0o700)
    previous = root/'previous.tar.gz'
    previous.write_bytes(b'previous')
    def run(args, **kwargs):
        kwargs['stdout'].write(b'partial')
        raise subprocess.TimeoutExpired(args, 120)
    with pytest.raises(subprocess.TimeoutExpired):
        backup.fetch(root, run)
    assert previous.read_bytes() == b'previous'
    assert not list(root.glob('*.partial'))


@pytest.mark.parametrize('symlink', [False, True])
def test_unsafe_directory_never_connects(tmp_path, symlink):
    root = tmp_path/'copies'
    if symlink:
        target = tmp_path/'target'
        target.mkdir(mode=0o700)
        root.symlink_to(target, target_is_directory=True)
    else:
        root.mkdir(mode=0o755)
    def run(*args, **kwargs):
        pytest.fail('must not connect')
    with pytest.raises(ValueError):
        backup.fetch(root, run)


def controller(tmp_path):
    config = tmp_path/'config'
    keys = ['TM_RELEASE_REPO', 'TM_RELEASE_COMPOSE', 'TM_PREVIOUS_COMPOSE', 'TM_API_IMAGE', 'TM_CALENDAR_IMAGE', 'TM_API_ENV_FILE', 'TM_API_DSN_FILE', 'TM_CALENDAR_ENV_FILE', 'TM_DATABASE_NETWORK', 'TM_STATE_DIR']
    config.write_text(''.join(key+'=synthetic\n' for key in keys))
    archive = tmp_path/'latest.tar.gz'
    script = tmp_path/'tmctl'
    source = (ROOT/'deploy/ops/tmctl-root').read_text()
    source = source.replace('CONFIG=/etc/telegram-manager/tmctl.env', 'CONFIG='+str(config), 1)
    source = source.replace("'/opt/telegram-manager/backups/latest.tar.gz'", repr(str(archive)), 1)
    # Test fixture runs unprivileged; production insists on root ownership.
    source = source.replace('info.st_uid != 0', 'info.st_uid != os.getuid()', 1)
    script.write_text(source)
    archive.write_bytes(bundle())
    archive.chmod(0o600)
    return script, archive


def test_fixed_export_exact_bytes(tmp_path):
    script, archive = controller(tmp_path)
    result = subprocess.run(['bash', str(script), 'backup-export'], capture_output=True)
    assert result.returncode == 0
    assert result.stdout == archive.read_bytes()


@pytest.mark.parametrize('kind', ['symlink', 'public-mode', 'empty', 'extra-path'])
def test_export_rejects_unsafe_source_or_arguments(tmp_path, kind):
    script, archive = controller(tmp_path)
    args = ['backup-export']
    if kind == 'symlink':
        target = tmp_path/'other'
        archive.rename(target)
        archive.symlink_to(target)
    elif kind == 'public-mode':
        archive.chmod(0o644)
    elif kind == 'empty':
        archive.write_bytes(b'')
    else:
        args.append('/other/project/secret')
    result = subprocess.run(['bash', str(script), *args], capture_output=True)
    assert result.returncode != 0 and not result.stdout
