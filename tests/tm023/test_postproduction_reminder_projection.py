import pytest

from tm_api.postproduction_identity import postproduction_reminder_id
from tm_api.postproduction_reminder_projection import (
    PostproductionReminderBusiness,
    desired_action,
)
from tm_api.postproduction_sheet import normalize_projected_reminders
from tm_api.v24.common import Rejected


WORKSPACE = "b39f14f1-3d21-440e-ab14-bc63db4ae304"
LIST_ID = "e5e340b1-fdfa-4210-bdd7-72859194d906"
ASSET_ID = "a6ffd76f-6ef0-4bf8-9de7-79dbc4f5ead0"
PARENT_TITLE = "RCC HARD x GAMEBRED 12.09"


def asset(*, approved=False, cloud=False, asset_class="pov"):
    return {
        "title": "1_Участник Один VS Участник Два",
        "state": "active",
        "asset_id": ASSET_ID,
        "parent_id": "a7e9282d-3b10-407e-b45e-76614430f9d0",
        "cloud_urls": ["https://disk.example/a"] if cloud else [],
        "asset_class": asset_class,
        "content_type": "Intro" if asset_class == "non_pov" else "1_Участник Один VS Участник Два",
        "layout_number": None if asset_class == "non_pov" else 1,
        "approval_actor": "user" if approved else "",
        "approval_basis": "user_override" if approved else "required_external_approval",
        "approval_state": "approved" if approved else "pending",
    }


def item(kind="edit", state="waiting_feedback", *, progress=None, action=None, reminder_task_id=None):
    data = {
        "kind": kind,
        "state": state,
        "asset_id": ASSET_ID,
        "completion_policy": "explicit_approval",
    }
    if progress is not None:
        data["progress_status"] = progress
    if action is not None:
        data["next_action"] = action
    if reminder_task_id is not None:
        data["reminder_task_id"] = reminder_task_id
    return {"entity_id": "11111111-1111-4111-8111-111111111111", "revision": 1, "data": data}


def test_waiting_feedback_creates_no_user_action():
    decision = desired_action(asset(), item(), PARENT_TITLE, "edit")
    assert decision == {"mode": "complete", "state": "waiting_feedback"}


def test_active_corrections_use_canonical_next_action():
    decision = desired_action(
        asset(),
        item("edit", "active", progress="corrections", action="Внести правки — Бивол / Intro"),
        PARENT_TITLE,
        "edit",
    )
    assert decision == {
        "mode": "open",
        "state": "active",
        "title": "Внести правки — Бивол / Intro",
        "description": "Есть конкретное действие по монтажу.",
    }


def test_active_without_next_action_fails_closed():
    decision = desired_action(
        asset(), item("edit", "active", progress="in_progress"), PARENT_TITLE, "edit"
    )
    assert decision == {"mode": "preserve", "state": "active_without_next_action"}


def test_generic_task_owned_workstream_is_not_duplicated():
    decision = desired_action(
        asset(),
        item("edit", "active", progress="corrections", action="Внести правки", reminder_task_id=86),
        PARENT_TITLE,
        "edit",
    )
    assert decision == {"mode": "preserve", "state": "generic_task_owned"}


def test_approved_waiting_feedback_becomes_upload_action():
    decision = desired_action(asset(approved=True), item(), PARENT_TITLE, "edit")
    assert decision["mode"] == "open"
    assert decision["state"] == "upload"
    assert decision["title"].startswith("Согласовано, можно загружать")


def test_uploaded_approved_asset_completes_reminder():
    decision = desired_action(asset(approved=True, cloud=True), item(), PARENT_TITLE, "edit")
    assert decision == {"mode": "complete", "state": "uploaded"}


def test_completed_workstream_completes_reminder():
    decision = desired_action(
        asset(), item("edit", "completed", progress="done"), PARENT_TITLE, "edit"
    )
    assert decision == {"mode": "complete", "state": "completed"}


def test_non_pov_active_color_uses_canonical_action():
    decision = desired_action(
        asset(asset_class="non_pov"),
        item("color", "active", progress="not_started", action="Сделать цвет — RCC MMA 25 / Intro"),
        "RCC MMA 25",
        "color",
    )
    assert decision["mode"] == "open"
    assert decision["title"] == "Сделать цвет — RCC MMA 25 / Intro"
    assert decision["description"] == "Цвет ещё не готов."


def test_pov_color_is_not_applicable_even_without_item():
    decision = desired_action(asset(), None, PARENT_TITLE, "color")
    assert decision == {"mode": "complete", "state": "not_applicable"}


def test_missing_canonical_workstream_never_invents_action():
    decision = desired_action(asset(), None, PARENT_TITLE, "edit")
    assert decision == {"mode": "preserve", "state": "canonical_workstream_missing"}


def test_reminder_projection_has_no_sheet_reader():
    business = PostproductionReminderBusiness("macbook-owner", "/fake.json")
    assert not hasattr(business, "_snapshot_rows")


def test_manual_caldav_edit_is_never_overwritten():
    business = PostproductionReminderBusiness("macbook-owner", "/fake.json")
    current = {
        "id": "11111111-1111-4111-8111-111111111111",
        "calendar_id": LIST_ID,
        "source": "caldav",
        "revision": 3,
        "caldav_uid": "manual@telegram-manager",
        "href": "manual.ics",
        "icalendar": "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
        "fields": {
            "title": "Моё название",
            "description": "Моя заметка",
            "due_at": None,
            "all_day": False,
            "timezone": "Asia/Yekaterinburg",
            "status": "open",
            "categories": [],
            "priority": 0,
            "recurrence": None,
        },
    }
    decision = {
        "mode": "open",
        "state": "active",
        "title": "Внести правки — RCC HARD / POV №1",
        "description": "По монтажу нужны правки.",
    }
    workspace = {"id": WORKSPACE, "settings": {"timezone": "Asia/Yekaterinburg"}}
    with pytest.raises(Rejected, match="manual_override"):
        business._plan_one(
            workspace, {"id": LIST_ID}, asset(), "edit",
            current["id"], current, decision,
        )


def test_operation_is_exposed_by_universal_mutation_schema():
    from tm_api.v24.models import Operation
    from tm_api.v24.register import MUTATION_NAMES, WRITE_NAMES

    assert "update_postproduction_reminder_projection" in Operation.__args__
    assert "update_postproduction_reminder_projection" in MUTATION_NAMES
    assert "update_postproduction_reminder_projection" in WRITE_NAMES


def test_diagnostic_maps_projected_reminder_without_hidden_vtodo_fields():
    rid = postproduction_reminder_id("macbook-owner", WORKSPACE, ASSET_ID, "edit")
    reminders = [{
        "id": rid,
        "fields": {
            "title": "Согласовано, можно загружать — RCC HARD / POV №1",
            "status": "open",
        },
    }]
    normalized = normalize_projected_reminders(
        "macbook-owner", WORKSPACE, [{"asset_id": ASSET_ID}], reminders
    )
    assert normalized == [{
        "id": rid,
        "asset_id": ASSET_ID,
        "kind": "cloud_upload",
        "state": "open",
        "completed": False,
    }]
