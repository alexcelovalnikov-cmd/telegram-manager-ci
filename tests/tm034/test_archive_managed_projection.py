from tm_api.v25.managed import ManagedBusiness


RID = "df3d2ff9-cf97-4552-89bc-33897d76a7d3"


class FakeTx:
    def __init__(self):
        self.executes = []

    def one(self, query, args=None):
        if "tm_config.reminder_bindings" in query:
            return {
                "task_id": 46,
                "resource_id": RID,
                "workspace_id": "acb7ad84-cf4d-4a0c-96ef-698646ad4919",
                "hidden": False,
            }
        if "tm_calendar.events" in query:
            return {
                "id": RID,
                "deleted_at": None,
                "revision": 2,
            }
        raise AssertionError(query)

    def execute(self, query, args=None):
        self.executes.append((query, args))


def test_archive_projection_delete_skips_inbound_caldav_bridge(monkeypatch):
    tx = FakeTx()
    deleted = {}

    monkeypatch.setattr(
        "tm_api.v25.managed.calendars.api_user",
        lambda _tx, _instance: "owner",
    )

    def fake_delete(_tx, user, ident, revision, *, bridge=True):
        deleted.update(
            user=user,
            ident=ident,
            revision=revision,
            bridge=bridge,
        )
        return {"id": ident, "deleted": True, "revision": revision + 1}

    monkeypatch.setattr("tm_api.v25.managed.calendars.delete", fake_delete)

    plan = {
        "entity": "project",
        "after": {
            "id": 46,
            "project_archived_at": "assigned_at_commit",
        },
    }

    ManagedBusiness("macbook-owner").project(
        tx,
        {"operation": "archive_project", "changes": {"reason": "test"}},
        plan,
        46,
    )

    assert deleted == {
        "user": "owner",
        "ident": RID,
        "revision": 2,
        "bridge": False,
    }
    assert any(
        "UPDATE tm_config.reminder_bindings SET hidden=true" in query
        for query, _args in tx.executes
    )
