from worker.tm_source_discovery import dialog_kind, qualifies_postproduction, postproduction_topic_allowed
from worker.rr_groups import GroupConfig
from tm_api.sync_gateway import TABLES
from tm_api.v25.register import READ_NAMES


def entity(name, **attrs):
    return type(name, (), attrs)()


def test_dialog_kind_never_treats_private_as_group():
    assert dialog_kind(entity("User", bot=False)) == "private"
    assert dialog_kind(entity("User", bot=True)) == "bot"
    assert dialog_kind(entity("Chat")) == "group"
    assert dialog_kind(entity("Channel", broadcast=False, megagroup=True, forum=True)) == "forum"


def test_postproduction_candidate_requires_group_and_signal():
    private = {"kind": "private", "name": "Color", "last_message_preview": "", "topic_titles": []}
    assert qualifies_postproduction(private) is False
    group = {"kind": "supergroup", "name": "Project", "last_message_preview": "Монтаж V2 готов", "topic_titles": []}
    assert qualifies_postproduction(group) is True
    forum = {"kind": "forum", "name": "Big client chat", "last_message_preview": "", "topic_titles": ["General", "CC"]}
    assert qualifies_postproduction(forum) is True
    general_only = {"kind": "forum", "name": "Random", "last_message_preview": "", "topic_titles": ["General"]}
    assert qualifies_postproduction(general_only) is False
    assert postproduction_topic_allowed("Color 🎨") is True
    assert postproduction_topic_allowed("CC — grading") is True
    assert postproduction_topic_allowed("Delivery") is True


def test_postproduction_dynamic_member_requires_resolved_exact_target():
    raw = {
        "schema_version": 7,
        "groups": [{"id": 1, "group_key": "post", "name": "Postproduction", "enabled": True,
                    "monitoring_enabled": True, "rules_profile": "postproduction"}],
        "members": [{"group_id": 1, "chat_id": -100, "enabled": True, "managed_by_targets": True}],
        "targets": [{"group_id": 1, "selector_kind": "title", "selector_value": "dynamic:-100",
                     "enabled": True, "resolution_status": "resolved", "resolved_chat_id": -100}],
        "chats": [{"chat_id": -100, "chat_name": "Edit room", "enabled": True}],
    }
    assert -100 in GroupConfig(raw).whitelist


def test_default_dynamic_member_still_requires_resolved_target():
    raw = {
        "schema_version": 7,
        "groups": [{"id": 1, "group_key": "normal", "name": "Normal", "enabled": True,
                    "monitoring_enabled": True, "rules_profile": "default"}],
        "members": [{"group_id": 1, "chat_id": -100, "enabled": True, "managed_by_targets": True}],
        "targets": [],
        "chats": [{"chat_id": -100, "chat_name": "Room", "enabled": True}],
    }
    assert -100 not in GroupConfig(raw).whitelist


def test_sync_gateway_only_adds_narrow_dynamic_source_target_table():
    assert TABLES["telegram_chat_group_targets"] == {"POST", "PATCH"}
    assert "telegram_chats" not in TABLES
    assert "telegram_chat_group_members" not in TABLES


def test_candidate_read_is_public_mcp_capability():
    assert "list_telegram_dialog_candidates" in READ_NAMES
