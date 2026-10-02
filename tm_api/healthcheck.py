"""Read-only runtime health probe for container orchestration.

The probe validates the configured least-privilege PostgreSQL connection without
starting the ASGI application or touching SQLite state.
"""
import json
import os

from .config import Config
from .v24.database import Database


def probe(db):
    with db.transaction(read_only=True) as tx:
        row = tx.one("SELECT 1 AS ok")
    return {"ok": bool(row and row.get("ok") == 1), "service": "api", "read_only": True}


def main():
    try:
        config = Config.from_env()
        result = probe(Database(config.v24_dsn))
    except Exception:
        result = {"ok": False, "service": "api", "read_only": True}
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
