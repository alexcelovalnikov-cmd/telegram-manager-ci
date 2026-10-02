from contextlib import contextmanager

from tm_calendar import storage


CALENDAR_ID = "00000000-0000-0000-0000-000000000123"


class FakeTx:
    def __init__(self):
        self.queries = []

    def all(self, sql, args=()):
        self.queries.append((sql, args))
        if "FROM tm_calendar.changes" in sql:
            return [{"href": "changed.ics"}]
        if "FROM tm_calendar.events" in sql:
            return [{"href": "current.ics"}]
        raise AssertionError(sql)


class FakeDb:
    def __init__(self, tx):
        self.tx = tx
        self.read_only_values = []

    @contextmanager
    def transaction(self, read_only=False):
        self.read_only_values.append(read_only)
        yield self.tx


def test_incremental_sync_reopens_read_only_transaction_after_storage_lock(monkeypatch):
    tx = FakeTx()
    db = FakeDb(tx)
    row = {"id": CALENDAR_ID, "revision": 3}
    monkeypatch.setattr(storage, "database", lambda: db)
    monkeypatch.setattr(storage.repo, "access", lambda actual_tx, user, ident: row)
    token = storage.current.set(None)
    try:
        collection = storage.Collection(
            "owner/calendar",
            CALENDAR_ID,
            snapshot={"id": CALENDAR_ID, "revision": 2},
            item_snapshot=[],
            username="owner",
        )
        old = "http://telegram-manager.invalid/sync/" + CALENDAR_ID + "/2"
        new_token, hrefs = collection.sync(old)
    finally:
        storage.current.reset(token)

    assert db.read_only_values == [True]
    assert new_token.endswith("/3")
    assert hrefs == ["changed.ics"]
    assert any("FROM tm_calendar.changes" in sql for sql, _ in tx.queries)


def test_snapshot_get_multi_works_after_storage_lock():
    row = {
        "href": "event.ics",
        "icalendar": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:test\r\nDTSTART:20260929T040000Z\r\nDTEND:20260929T050000Z\r\nSUMMARY:Test\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
        "etag": '"etag"',
        "caldav_uid": "test",
        "updated_at": "2026-09-29T04:00:00+00:00",
    }
    token = storage.current.set(None)
    try:
        collection = storage.Collection(
            "owner/calendar",
            CALENDAR_ID,
            snapshot={"id": CALENDAR_ID, "revision": 1},
            item_snapshot=[row],
            username="owner",
        )
        href, item = next(collection.get_multi(["event.ics"]))
    finally:
        storage.current.reset(token)

    assert href == "event.ics"
    assert item is not None
    assert item.uid == "test"
