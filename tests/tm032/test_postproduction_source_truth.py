from tm_api.postproduction_projection import reminder_decision, sheet_status


ASSET = {
    "asset_id": "1fd021f1-d436-4745-8401-732186a460bf",
    "asset_class": "non_pov",
    "content_type": "Intro",
    "approval_state": "pending",
    "approval_basis": "required_external_approval",
    "cloud_urls": [],
}


def item(kind, state, progress=None, reminder_task_id=None):
    data = {
        "asset_id": ASSET["asset_id"],
        "kind": kind,
        "state": state,
    }
    if progress is not None:
        data["progress_status"] = progress
    if reminder_task_id is not None:
        data["reminder_task_id"] = reminder_task_id
    return {"data": data}


def test_sheet_status_mapping_is_canonical_not_ui_input():
    assert sheet_status(ASSET, item("edit", "waiting_materials"), "edit") == "Не готово"
    assert sheet_status(ASSET, item("edit", "active", "not_started"), "edit") == "Не готово"
    assert sheet_status(ASSET, item("edit", "active", "corrections"), "edit") == "В работе"
    assert sheet_status(ASSET, item("edit", "waiting_feedback", "waiting_feedback"), "edit") == "Нужны правки"
    assert sheet_status(ASSET, item("edit", "completed", "done"), "edit") == "Готово"
    assert sheet_status(ASSET, item("edit", "active", "assigned_elsewhere"), "edit") == "Делает Майя"


def test_server_approval_can_advance_waiting_feedback_projection():
    approved = dict(ASSET)
    approved.update(approval_state="approved", approval_basis="user_confirmation")
    assert sheet_status(approved, item("edit", "waiting_feedback", "waiting_feedback"), "edit") == "Готово"
    decision = reminder_decision(
        approved,
        {"data": {
            "asset_id": ASSET["asset_id"],
            "kind": "edit",
            "state": "waiting_feedback",
            "progress_status": "waiting_feedback",
        }},
        "RCC BOXING 30.05.2026",
        "edit",
    )
    assert decision["mode"] == "open"
    assert decision["state"] == "upload"


def test_generic_task_and_deterministic_projection_do_not_duplicate():
    decision = reminder_decision(
        ASSET,
        item("edit", "active", "corrections", reminder_task_id=86),
        "RCC BOXING 30.05.2026",
        "edit",
    )
    assert decision == {"mode": "preserve", "state": "generic_task_owned"}


def test_missing_item_is_fail_closed_for_both_projections():
    assert sheet_status(ASSET, None, "edit") is None
    assert reminder_decision(ASSET, None, "RCC BOXING", "edit") == {
        "mode": "preserve", "state": "canonical_workstream_missing"
    }
