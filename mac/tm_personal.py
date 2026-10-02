"""Read-only preflight for the existing owner's installation, never onboarding."""
from __future__ import annotations
import re
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_HOST = 'service.example.invalid'
INSTANCE_ID = 'macbook-owner'
BUILD_ID = 'v18-personal-20260928-1'


def check_existing(home: Path | None = None, env: dict | None = None) -> dict:
    home=Path(home or Path.home())
    app=home/'Library/Application Support/RentRabbit'
    required=[home/'.telegram_helper_env', home/'telegram_helper.session',
              home/'telegram-helper-venv/bin/python', app/'runtime/VERSION']
    if any(not p.is_file() for p in required):
        raise RuntimeError('Нужна существующая личная установка V16; First Start не применяется')
    if app.is_symlink() or (app/'runtime').is_symlink() or required[0].is_symlink() or required[1].is_symlink():
        raise RuntimeError('Небезопасный путь существующей установки')
    version=(app/'runtime/VERSION').read_text(encoding='utf-8').strip()
    if not re.fullmatch(r'Telegram Manager V(?:16|18)',version):
        raise RuntimeError('Не подтверждена исходная личная V16/V18; установка остановлена')
    if env is not None:
        parsed=urlsplit(env.get('TM_SYNC_URL',''))
        if parsed.scheme!='https' or parsed.hostname!=PROJECT_HOST or parsed.username or parsed.password or parsed.port or parsed.path != '/telegram-manager/sync/v1' or parsed.query or parsed.fragment:
            raise RuntimeError('Подключение не относится к личной базе Владельца')
    return {'previous_version':version,'instance_id':INSTANCE_ID,'preserve_credentials':True,
            'preserve_session':True,'preserve_groups_and_lists':True,'new_environment':False}
