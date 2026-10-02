import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path("worker").resolve()))

from rr_groups import GroupConfig
from telegram_supabase import Collector, Outbox
from tm_source_discovery import (
    activation_candidates,
    classify_postproduction_evidence,
    discovery_bundle,
    read_candidates,
)
from tm_api.v25.reading import umbrella_binding_hint
from tm_reminders.models import Reminder


class Dialog:
    def __init__(self, chat_id, name, text="", *, forbid_message=False):
        self.id = chat_id
        self.name = name
        self.entity = type("User", (), {"bot": False, "username": None, "usernames": []})()
        self._message = SimpleNamespace(
            message=text,
            date=datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc),
        )
        self.forbid_message = forbid_message
        self.message_reads = 0

    @property
    def message(self):
        self.message_reads += 1
        if self.forbid_message:
            raise AssertionError("excluded private message must not be read")
        return self._message


class DialogClient:
    def __init__(self, *dialogs):
        self.dialogs = dialogs

    async def iter_dialogs(self, limit=None):
        for dialog in self.dialogs[:limit]:
            yield dialog


def test_exclusion_by_display_name_happens_before_private_message_read():
    dialog = Dialog(101, " Secret Person ", forbid_message=True)
    rows, topics, skipped = asyncio.run(read_candidates(
        DialogClient(dialog),
        excluded_names={"secret person"},
        excluded_chat_ids=set(),
    ))
    assert rows == []
    assert topics == {}
    assert skipped == [{
        "chat_id": 101,
        "status": "skipped_by_exclusion",
        "matched_by": "display_name",
    }]
    assert dialog.message_reads == 0
    assert discovery_bundle(rows, skipped)["skipped"] == [
        {"status": "skipped_by_exclusion"}
    ]


def test_exclusion_by_stable_chat_id_happens_before_private_message_read():
    dialog = Dialog(202, "Renamable Person", forbid_message=True)
    rows, _, skipped = asyncio.run(read_candidates(
        DialogClient(dialog),
        excluded_names=set(),
        excluded_chat_ids={202},
    ))
    assert rows == []
    assert skipped[0]["matched_by"] == "chat_id"
    assert dialog.message_reads == 0


def test_nonexcluded_private_candidate_is_bounded_and_public_preview_stays_private():
    dialog = Dialog(303, "Editor", "Первый драфт готов, отправил V1")
    rows, _, skipped = asyncio.run(read_candidates(
        DialogClient(dialog),
        excluded_names=set(),
        excluded_chat_ids=set(),
        private_evidence_limit=1,
    ))
    assert skipped == []
    assert dialog.message_reads == 1
    assert rows[0]["last_message_preview"] == ""
    assert rows[0]["_evidence_status"] == "single_current_message"
    assert rows[0]["_classification"]["state"] == "post_started"
    assert activation_candidates(rows)[0]["chat_id"] == 303


def test_shoot_only_does_not_activate_and_ambiguous_post_mention_fails_closed():
    shoot = {"kind": "private", "_evidence_preview": "Съёмка завтра, монтаж будет позже"}
    ambiguous = {"kind": "private", "_evidence_preview": "Монтаж обсудим на следующей неделе"}
    assert classify_postproduction_evidence(shoot)["state"] == "not_started"
    assert activation_candidates([shoot]) == []
    assert classify_postproduction_evidence(ambiguous)["state"] == "ambiguous"
    assert activation_candidates([ambiguous]) == []


def test_confirmed_waiting_materials_candidate_can_be_monitored_without_reminder():
    waiting = {
        "kind": "private",
        "_evidence_preview": "Ждём материалы для монтажа интро, исходники ещё не получили",
    }
    result = classify_postproduction_evidence(waiting)
    assert result["state"] == "waiting_materials"
    assert result["waiting_materials"] is True
    assert result["user_action"] is None
    assert activation_candidates([waiting])[0] is waiting


def test_waiting_client_without_started_post_and_future_version_do_not_activate():
    waiting_only = {"kind": "private", "_evidence_preview": "Ждём клиента, потом решим что делать"}
    future_version = {"kind": "private", "_evidence_preview": "V1 отправим клиенту завтра после съёмки"}
    assert classify_postproduction_evidence(waiting_only)["state"] == "ambiguous"
    assert activation_candidates([waiting_only]) == []
    assert classify_postproduction_evidence(future_version)["state"] != "post_started"
    assert activation_candidates([future_version]) == []


class Query:
    def __init__(self, db):
        self.db = db
        self.payload = None

    def upsert(self, payload, on_conflict=None):
        self.payload = dict(payload)
        self.db.calls.append((self.payload, on_conflict))
        return self

    def execute(self):
        return SimpleNamespace(data=[self.payload])


class ActivationDb:
    def __init__(self):
        self.calls = []

    def table(self, name):
        assert name == "telegram_chat_group_targets"
        return Query(self)


class ActivationConfig:
    def __init__(self):
        self.raw = {"targets": [], "members": []}
        self.by_chat = {}
        self.groups = {24: {
            "id": 24,
            "enabled": True,
            "monitoring_enabled": True,
            "rules_profile": "postproduction",
        }}
        self.chats = {}

    def monitored_groups(self):
        return list(self.groups.values())


def test_post_started_private_source_activation_is_guarded_and_idempotent():
    db = ActivationDb()
    collector = Collector(db, None, SimpleNamespace())
    collector.config = ActivationConfig()
    collector.source_exclusions = {"excluded_names": set(), "excluded_chat_ids": set()}
    collector.discovery_rows = [{
        "chat_id": 404,
        "name": "Private post",
        "kind": "private",
        "_classification": {
            "state": "post_started",
            "signals": ["editing_started"],
            "waiting_feedback": False,
            "user_action": None,
        },
    }]

    async def no_refresh():
        return collector.config

    collector.refresh = no_refresh
    assert asyncio.run(collector.sync_postproduction_sources()) == 1
    assert db.calls == [({
        "group_id": 24,
        "selector_kind": "title",
        "selector_value": "dynamic:404",
        "display_name": "Private post",
        "chosen_chat_id": 404,
        "enabled": True,
    }, "group_id,selector_kind,selector_value")]

    collector.config.by_chat[404] = [24]
    assert asyncio.run(collector.sync_postproduction_sources()) == 0
    assert len(db.calls) == 1


def test_ambiguous_source_never_auto_activates():
    db = ActivationDb()
    collector = Collector(db, None, SimpleNamespace())
    collector.config = ActivationConfig()
    collector.source_exclusions = {"excluded_names": set(), "excluded_chat_ids": set()}
    collector.discovery_rows = [{
        "chat_id": 405,
        "name": "Maybe post",
        "kind": "private",
        "_classification": {"state": "ambiguous", "signals": []},
    }]
    assert asyncio.run(collector.sync_postproduction_sources()) == 0
    assert db.calls == []


def dynamic_config():
    return GroupConfig({
        "schema_version": 7,
        "groups": [{
            "id": 24,
            "group_key": "postproduction",
            "name": "Postproduction",
            "enabled": True,
            "monitoring_enabled": True,
            "rules_profile": "postproduction",
        }],
        "members": [{
            "group_id": 24,
            "chat_id": 500,
            "enabled": True,
            "managed_by_targets": True,
        }],
        "targets": [{
            "id": 700,
            "group_id": 24,
            "selector_kind": "title",
            "selector_value": "dynamic:500",
            "enabled": True,
            "resolution_status": "resolved",
            "resolved_chat_id": 500,
            "chosen_chat_id": 500,
            "request_revision": 2,
        }],
        "chats": [{"chat_id": 500, "chat_name": "Post room", "enabled": True}],
    })


def test_dynamic_backfill_marker_is_idempotent(tmp_path):
    outbox = Outbox(tmp_path / "outbox.sqlite")
    collector = Collector(None, None, outbox)
    collector.config = dynamic_config()
    collector.config_seen = time.monotonic()
    collector.source_exclusions = {"excluded_names": set(), "excluded_chat_ids": set()}
    assert len(collector._pending_postproduction_backfills()) == 1
    outbox.mark_dynamic_backfill_done(700, 2, "2026-10-01T07:00:00+00:00")
    assert collector._pending_postproduction_backfills() == []


def test_multi_chat_identity_binds_existing_umbrella_instead_of_duplicate():
    rows = [{
        "id": "umbrella-1",
        "data": {
            "title": "RCC HARD x GAMEBRED 12.09",
            "client": "RCC",
            "source_chats": ["RCC HARD"],
        },
    }]
    assert umbrella_binding_hint(rows, "RCC HARD x GAMEBRED 12.09") == {
        "status": "bind_existing",
        "entity_id": "umbrella-1",
    }
    ambiguous = rows + [{
        "id": "umbrella-2",
        "data": {"title": "RCC HARD x GAMEBRED 12.09"},
    }]
    assert umbrella_binding_hint(ambiguous, "RCC HARD x GAMEBRED 12.09") == {
        "status": "ambiguous",
        "entity_id": None,
    }


def test_waiting_feedback_keeps_project_active_without_user_reminder():
    result = classify_postproduction_evidence({
        "kind": "private",
        "_evidence_preview": "V2 отправил клиенту на согласование, ждём фидбек",
    })
    assert result["state"] == "post_started"
    assert result["waiting_feedback"] is True
    assert result["user_action"] is None


def test_concrete_owner_action_maps_to_concrete_reminder_title_and_description():
    result = classify_postproduction_evidence({
        "kind": "private",
        "_evidence_preview": (
            "Первый драфт готов. Владелец, поправь первый кадр, лицо слишком тёмное"
        ),
    })
    assert result["state"] == "post_started"
    assert result["user_action"] == {
        "title": "Поправить первый кадр",
        "description": "Лицо слишком тёмное",
    }
    reminder = Reminder(**result["user_action"])
    assert reminder.title == "Поправить первый кадр"
    assert reminder.description == "Лицо слишком тёмное"


class ForumGuardClient(DialogClient):
    def __init__(self, *dialogs):
        super().__init__(*dialogs)
        self.topic_reads = 0

    async def get_input_entity(self, entity_value):
        self.topic_reads += 1
        raise AssertionError("excluded forum topics must not be read")


def test_excluded_forum_is_rejected_before_topic_or_message_access():
    dialog = Dialog(-100700, "Даша Колташёва", forbid_message=True)
    dialog.entity = type("Channel", (), {
        "broadcast": False,
        "megagroup": True,
        "forum": True,
        "username": None,
        "usernames": [],
    })()
    client = ForumGuardClient(dialog)
    rows, topics, skipped = asyncio.run(read_candidates(
        client,
        excluded_names={" даша   колташева "},
        excluded_chat_ids=set(),
    ))
    assert rows == []
    assert topics == {}
    assert len(skipped) == 1
    assert client.topic_reads == 0
    assert dialog.message_reads == 0


def test_exclusion_by_string_chat_id_is_fail_closed_before_message_read():
    dialog = Dialog(707, "Renamed", forbid_message=True)
    rows, _, skipped = asyncio.run(read_candidates(
        DialogClient(dialog),
        excluded_names=set(),
        excluded_chat_ids={"707"},
    ))
    assert rows == []
    assert skipped[0]["matched_by"] == "chat_id"
    assert dialog.message_reads == 0


def test_plain_waiting_for_client_at_shoot_is_not_post_feedback():
    result = classify_postproduction_evidence({
        "_evidence_preview": "Ждём клиента на съёмку, камера уже в рентале."
    })
    assert result["state"] == "not_started"
    assert result["waiting_feedback"] is False


def test_estimate_or_payment_approval_does_not_activate_post():
    result = classify_postproduction_evidence({
        "_evidence_preview": "Смета на монтаж согласована, оплату проведём после съёмки."
    })
    assert result["state"] == "not_started"
    assert "estimate_only" in result["non_start_signals"]
    assert "payment_only" in result["non_start_signals"]


def test_concrete_owner_revision_alone_is_strong_post_start_evidence():
    result = classify_postproduction_evidence({
        "_evidence_preview": "Владелец, поправь первый кадр, он слишком тёмный."
    })
    assert result["state"] == "post_started"
    assert "concrete_revision_action" in result["signals"]
    assert result["user_action"] == {
        "title": "Поправить первый кадр",
        "description": "Он слишком тёмный",
    }


def test_chat_name_variant_reuses_existing_umbrella_when_identity_is_unique():
    rows = [{
        "id": "umbrella-1",
        "data": {
            "title": "RCC HARD x GAMEBRED 12.09",
            "source_chats": ["RCC HARD x GAMEBRED 12.09 Челябинск // SHOW"],
        },
    }]
    assert umbrella_binding_hint(rows, "RCC HARD x GAMEBRED 12.09 / монтаж") == {
        "status": "bind_existing",
        "entity_id": "umbrella-1",
    }


class BackfillReadTrap:
    async def get_messages(self, chat_id, limit):
        raise AssertionError("excluded source history must not be read")


def test_excluded_resolved_dynamic_source_never_enters_backfill(tmp_path):
    collector = Collector(None, BackfillReadTrap(), Outbox(tmp_path / "excluded.sqlite"))
    collector.config = dynamic_config()
    collector.config_seen = time.monotonic()
    collector.source_exclusions = {
        "excluded_names": {"post room"},
        "excluded_chat_ids": set(),
    }
    assert collector._pending_postproduction_backfills() == []
    asyncio.run(collector.hydrate_postproduction_sources())
