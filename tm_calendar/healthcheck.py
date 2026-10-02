"""Read-only PostgreSQL health probe for the CalDAV container."""
import json

from .context import database


def probe(db):
    with db.transaction(read_only=True) as tx:
        row = tx.one("SELECT count(*) AS n FROM tm_calendar.users WHERE false")
    return {"ok": row is not None, "service": "calendar", "read_only": True}


def main():
    try:
        result = probe(database())
    except Exception:
        result = {"ok": False, "service": "calendar", "read_only": True}
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
