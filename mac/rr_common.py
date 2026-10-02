"""Local credentials, restricted state directories, and bounded DB I/O."""
import fcntl
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from rr_groups import GroupConfig

APP_DIR = Path.home() / "Library" / "Application Support" / "RentRabbit"
ENV_FILE = Path.home() / ".telegram_helper_env"
INSTANCE_ID = "macbook-owner"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_env(path=ENV_FILE):
    if path.is_symlink():
        raise RuntimeError("Символическая ссылка вместо настроек запрещена")
    if not path.is_file():
        raise RuntimeError("Не найден локальный файл настроек .telegram_helper_env")
    if path.stat().st_mode & 0o077:
        raise RuntimeError("Файл .telegram_helper_env должен иметь права 600")
    result = dict(os.environ)
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] in ("'", '"') and value[-1:] == value[:1]:
            value = value[1:-1]
        result[key] = value
    return result


def database(env):
    from postgrest import SyncPostgrestClient
    from urllib.parse import urlsplit
    endpoint = env.get("TM_SYNC_URL", "")
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "https" or parsed.hostname != "service.example.invalid"
            or parsed.path != "/telegram-manager/sync/v1" or parsed.username
            or parsed.password or parsed.port or parsed.query or parsed.fragment):
        raise RuntimeError("Не подтверждён личный сервер Telegram Manager")
    token = env.get("TM_SYNC_TOKEN", "")
    if len(token) < 40:
        raise RuntimeError("Не задан отдельный ключ синхронизации Mac")
    return SyncPostgrestClient(endpoint, headers={"Authorization": "Bearer " + token}, timeout=30)



def get_config(db):
    return GroupConfig(db.rpc("rr_group_config_v7", {}).execute().data)


def lock_file(name):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    path = APP_DIR / name
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    handle = os.fdopen(fd, "r+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError("Уже запущен другой экземпляр службы: " + name)
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def safe_error(exc):
    # Do not copy HTTP payloads, keys, message texts, or connection strings to logs.
    if isinstance(exc, (RuntimeError, ValueError)) and type(exc).__module__ == 'builtins':
        text = str(exc)
        if not re.search(r"(eyJ|sb_secret_|Bearer|https?://|api.?key|token|password)", text, re.I):
            return type(exc).__name__ + ": " + text[:240]
    code = getattr(exc, 'code', None)
    if code is not None and re.fullmatch(r'[A-Za-z0-9_]{1,30}', str(code)):
        return type(exc).__name__ + ' (code=' + str(code) + ')'
    return type(exc).__name__


def save_health(db, service, status, details, success=False, error=None):
    from tm_personal import BUILD_ID
    data = {"service_name": service, "instance_id": INSTANCE_ID, "status": status,
            "last_heartbeat_at": now_iso(), "updated_at": now_iso(),
            "last_error": error, "details": dict(details, version="V18", build_id=BUILD_ID)}
    if success:
        data["last_success_at"] = now_iso()
    from tm_runtime_log import RuntimeOutbox
    outbox=RuntimeOutbox(APP_DIR/'runtime_error_outbox', service, INSTANCE_ID)
    try:
        db.table("integration_health").upsert(data, on_conflict="service_name,instance_id").execute()
    except Exception as exc:
        try: outbox.observe(exc)
        except Exception: print('RUNTIME LOG: local spool unavailable',flush=True)
        raise
    try:
        outbox.observe(None)
        outbox.flush(db)
    except Exception as exc:
        print('RUNTIME LOG DELIVERY: '+type(exc).__name__,flush=True)


def wait_for_deployment():
    """Installer loads both agents behind a local barrier before enabling writes."""
    import time
    barrier=Path(__file__).resolve().parent / '.deployment_hold'
    if not barrier.exists(): return
    print('Telegram Manager: ожидаю завершения установки; запись выключена.',flush=True)
    for _ in range(180):
        if not barrier.exists(): return
        time.sleep(1)
    raise RuntimeError('Установщик не снял блокировку запуска; требуется проверка состояния')
