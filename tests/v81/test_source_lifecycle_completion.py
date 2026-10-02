import asyncio
import time
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path("worker").resolve()))
import telegram_supabase
from rr_groups import GroupConfig
from telegram_supabase import Collector, Outbox
from tm_source_discovery import postproduction_topic_allowed


def postproduction_config(*, managed_by_targets=False, targets=None):
    raw = {
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
            "chat_id": -1009421666050320,
            "enabled": True,
            "managed_by_targets": managed_by_targets,
        }],
        "targets": targets or [],
        "chats": [{
            "chat_id": -1009421666050320,
            "chat_name": "Nomad games / RENDER",
            "enabled": True,
        }],
    }
    return GroupConfig(raw)


class BackfillClient:
    def __init__(self):
        self.calls = []

    async def get_messages(self, chat_id, limit):
        self.calls.append((chat_id, limit))
        return [SimpleNamespace(id=i, reply_to=None) for i in range(1, limit + 1)]


def collector_for(tmp_path, config, client=None):
    outbox = Outbox(tmp_path / "outbox.sqlite")
    collector = Collector(None, client or BackfillClient(), outbox)
    collector.config = config
    collector.config_seen = time.monotonic()
    collector.source_exclusions = {"excluded_names": set(), "excluded_chat_ids": set()}
    return collector


def test_explicit_workspace_member_gets_one_bounded_initial_backfill(tmp_path):
    client = BackfillClient()
    collector = collector_for(tmp_path, postproduction_config(), client)
    queued = []

    async def fake_on_message(event):
        queued.append(event.message.id)

    collector.on_message = fake_on_message
    asyncio.run(collector.hydrate_postproduction_sources(max_sources=1, message_limit=40))

    assert client.calls == [(-1009421666050320, 40)]
    assert len(queued) == 40
    assert collector.outbox.explicit_backfill_done(24, -1009421666050320) is True
    assert collector._pending_postproduction_backfills() == []

    asyncio.run(collector.hydrate_postproduction_sources(max_sources=1, message_limit=40))
    assert client.calls == [(-1009421666050320, 40)]


def test_explicit_source_selector_uses_existing_target_backfill_marker(tmp_path):
    target = {
        "id": 700,
        "group_id": 24,
        "selector_kind": "title",
        "selector_value": "Nomad games / RENDER",
        "enabled": True,
        "resolution_status": "resolved",
        "resolved_chat_id": -1009421666050320,
        "request_revision": 3,
    }
    collector = collector_for(
        tmp_path,
        postproduction_config(managed_by_targets=True, targets=[target]),
    )
    pending = collector._pending_postproduction_backfills()
    assert pending == [{
        "source_kind": "target",
        "target_id": 700,
        "revision": 3,
        "group_id": 24,
        "chat_id": -1009421666050320,
    }]


def test_discovery_publish_limit_does_not_lose_forum_routing_metadata(tmp_path, monkeypatch):
    rows = [
        {"chat_id": i, "kind": "group", "name": f"group-{i}", "forum": False}
        for i in range(1, 101)
    ]
    rows.append({
        "chat_id": -1009421666050320,
        "kind": "forum",
        "name": "Nomad games / RENDER",
        "forum": True,
    })

    async def fake_read_candidates(client, limit, **kwargs):
        assert limit == telegram_supabase.DIALOG_DISCOVERY_SCAN_LIMIT
        assert kwargs["excluded_names"] == set()
        assert kwargs["excluded_chat_ids"] == set()
        return rows, {(-1009421666050320, 11): "General"}, []

    monkeypatch.setattr(telegram_supabase, "read_candidates", fake_read_candidates)
    collector = collector_for(tmp_path, postproduction_config())
    asyncio.run(collector.refresh_dialog_candidates())

    assert len(collector.dialog_candidates) == telegram_supabase.DIALOG_DISCOVERY_PUBLISH_LIMIT
    assert -1009421666050320 not in {row["chat_id"] for row in collector.dialog_candidates}
    assert -1009421666050320 in collector.dialog_forum_ids
    assert collector.dialog_topic_index[(-1009421666050320, 11)] == "General"


@pytest.mark.parametrize("title", [
    "General",
    "Color",
    "Edit",
    "Cleanup",
    "VFX",
    "Sound",
    "Delivery",
])
def test_required_postproduction_forum_topics_remain_allowed(title):
    assert postproduction_topic_allowed(title) is True


def test_explicit_source_configuration_retires_legacy_auto_dynamic_targets():
    text = open("tm_api/v25/business.py", encoding="utf-8").read()
    assert "'chat_ids' in m['changes'] or 'source_selectors' in m['changes']" in text
    assert "selector_value LIKE 'dynamic:%%'" in text
    assert "ON CONFLICT(group_id,chat_id) DO UPDATE SET enabled=true,managed_by_targets=false" in text
