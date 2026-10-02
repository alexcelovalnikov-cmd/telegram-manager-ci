from contextlib import contextmanager

from tm_api import healthcheck as api_health
from tm_calendar import healthcheck as calendar_health


class FakeTx:
    def __init__(self, row):
        self.row = row
        self.calls = []

    def one(self, statement, params=()):
        self.calls.append((statement, params))
        return self.row


class FakeDb:
    def __init__(self, row):
        self.tx = FakeTx(row)
        self.read_only = None

    @contextmanager
    def transaction(self, read_only=False):
        self.read_only = read_only
        yield self.tx


def test_api_health_probe_is_read_only():
    db = FakeDb({"ok": 1})
    assert api_health.probe(db) == {
        "ok": True,
        "service": "api",
        "read_only": True,
    }
    assert db.read_only is True
    assert db.tx.calls == [("SELECT 1 AS ok", ())]


def test_calendar_health_probe_is_read_only():
    db = FakeDb({"n": 0})
    assert calendar_health.probe(db) == {
        "ok": True,
        "service": "calendar",
        "read_only": True,
    }
    assert db.read_only is True
    assert db.tx.calls == [
        ("SELECT count(*) AS n FROM tm_calendar.users WHERE false", ())
    ]
