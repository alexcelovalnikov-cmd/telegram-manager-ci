from tm_api.v24.business import Business


class FakeTx:
    def __init__(self):
        self.ones = []
        self.executes = []

    def one(self, query, args=None):
        self.ones.append((query, args))
        if query.startswith("UPDATE public.tasks SET project_data="):
            return {
                "id": 46,
                "updated_at": "2026-10-02T04:00:00+00:00",
                "project_archived_at": "2026-10-02T04:00:00+00:00",
            }
        raise AssertionError(query)

    def execute(self, query, args=None):
        self.executes.append((query, args))


def test_archive_execute_uses_isolated_metadata_path_and_preserves_history():
    tx = FakeTx()
    plan = {
        "entity": "project",
        "target_id": 46,
        "before": {"id": 46, "project_archived_at": None},
        "after": {
            "id": 46,
            "record_kind": "project",
            "context_group_id": 2,
            "updated_at": "2026-09-29T06:18:21+00:00",
            "title": "CC Демообъект Участник Б - ?",
            "description": "",
            "status": "open",
            "due_at": None,
            "reminder_due_spec": {"kind": "none"},
            "tags": [],
            "target_app": None,
            "project_data": {
                "label": "CC Демообъект Участник Б - ?",
                "archive_reason": "Ошибочная отдельная финансовая позиция",
            },
            "payment_status": "unknown",
            "payment_evidence": None,
            "payment_confirmed_at": None,
            "project_archived_at": "assigned_at_commit",
        },
        "workspace_id": "acb7ad84-cf4d-4a0c-96ef-698646ad4919",
    }
    mutation = {
        "operation": "archive_project",
        "evidence": [{
            "kind": "user",
            "statement": "Убрать ошибочную позицию",
            "confirmation_ref": "synthetic-tm030",
        }],
    }

    result = Business("macbook-owner").execute(tx, mutation, plan)

    assert result["archived"] is True
    assert len(tx.ones) == 1
    query, args = tx.ones[0]
    assert "project_archived_at=clock_timestamp()" in query
    assert "tm_payment_title" not in query
    assert args[1] == 46
    assert len(tx.executes) == 1
    assert "tm_project_events" in tx.executes[0][0]
    assert tx.executes[0][1][1] == "archive_project"
