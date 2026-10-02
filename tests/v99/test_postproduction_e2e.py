from contextlib import contextmanager

from tm_api.v25 import reading
from tm_api.v25.reading import ConfigReader


class Tx:
    def all(self, sql, args=()):
        if "entity_type='postproduction-job'" in sql:
            return [{"id": "job-1", "data": {"title": "Project", "source_chats": ["Forum"]}}]
        raise AssertionError(sql)

    def one(self, sql, args=()):
        if "FROM public.telegram_messages" in sql:
            return {"saved_messages": 4 if args[0] == -1001 else 2,
                    "latest_message_at": "2026-10-01T03:00:00+00:00"}
        if "FROM public.integration_health" in sql:
            return {"status": "running", "last_heartbeat_at": "2026-10-01T04:00:00+00:00",
                    "last_success_at": "2026-10-01T04:00:00+00:00",
                    "details": {"dynamic_backfill_pending": 0,
                                "dialog_candidates_scanned_at": "2026-10-01T04:00:00+00:00",
                                "dialog_candidates": [{"chat_id": -1001, "forum": True,
                                                       "topic_titles": ["General", "Color"]}],
                                "source_exclusion_guard": {
                                    "available": True,
                                    "applied_before_content": True,
                                    "excluded_name_count": 2,
                                    "excluded_chat_id_count": 1,
                                },
                                "postproduction_discovery": {
                                    "classifier": "deterministic_v105",
                                    "private_evidence_limit": 12,
                                    "candidates": [],
                                    "skipped": [{"status": "skipped_by_exclusion"}],
                                }}}
        if "FROM tm_config.entities" in sql:
            return {"total": 2, "jobs": 1, "items": 1}
        if "FROM tm_config.analysis_receipts" in sql:
            return {"count": 2, "latest_at": "2026-09-29T14:51:00+00:00"}
        if "FROM tm_calendar.events" in sql:
            return {"count": 0}
        raise AssertionError(sql)


class Db:
    @contextmanager
    def transaction(self, read_only=False):
        assert read_only is True
        yield Tx()


def test_analysis_context_exposes_read_only_postproduction_e2e(monkeypatch):
    ctx = {
        "workspace": {"id": "11111111-1111-4111-8111-111111111111",
                      "settings": {"analysis": {"profile": "postproduction"},
                                   "reminder_list": {"mode": "server_new",
                                                     "id": "22222222-2222-4222-8222-222222222222"}}},
        "sources": [
            {"chat_id": -1001, "chat_name": "Forum", "enabled": True, "managed_by_targets": False},
            {"chat_id": -1002, "chat_name": "Group", "enabled": True, "managed_by_targets": False},
        ],
    }
    monkeypatch.setattr(reading.repo, "context", lambda tx, instance, workspace_id: ctx)
    monkeypatch.setattr(reading.calendar, "lock", lambda tx, write=False: None)
    monkeypatch.setattr(reading.calendar, "api_user", lambda tx, instance: "owner")
    monkeypatch.setattr(reading.calendar, "access",
                        lambda tx, user, ident: {"component_type": "VTODO"})
    result = ConfigReader(Db(), "instance").read(
        "analysis_context", workspace_id=ctx["workspace"]["id"])
    diagnostic = result["e2e_diagnostic"]
    assert diagnostic["read_only"] is True
    assert diagnostic["source_stage"]["configured"] == 2
    assert diagnostic["source_stage"]["with_saved_history"] == 2
    assert diagnostic["source_exclusion_stage"]["applied_before_content"] is True
    assert diagnostic["source_exclusion_stage"]["skipped"] == [{"status": "skipped_by_exclusion"}]
    assert diagnostic["candidate_evidence_stage"]["private_current_message_limit"] == 12
    assert diagnostic["topic_routing_stage"]["collector_backfill_pending"] == 0
    assert diagnostic["semantic_stage"]["background_semantic_executor"] is False
    assert diagnostic["managed_stage"]["reminder_projection_supported"] is True
    assert diagnostic["managed_stage"]["reminder_projection_operation"] == "update_postproduction_reminder_projection"
    assert diagnostic["reminder_stage"]["items"] == 0
    assert diagnostic["controlled_pass_ready"] is True
    assert diagnostic["empty_list_explanation"] == [
        "no_background_semantic_executor",
        "managed_entities_do_not_auto_project_to_reminders",
    ]


def test_reconciliation_parent_is_read_from_entity_data():
    from pathlib import Path
    text = Path("tm_api/v32/reconciliation.py").read_text()
    assert "e.data->>'parent_id' AS parent_id" in text
    assert "e.parent_id" not in text


def test_review_preparation_is_bundle_only():
    from pathlib import Path
    text = Path("tm_api/jobs.py").read_text()
    block = text.split("elif name == 'review_preparation':", 1)[1].split(
        "elif name == 'rcc_sheet_sync':", 1)[0]
    assert "get_review_bundle" in block
