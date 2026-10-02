import copy

import pytest

from tm_api.postproduction_sheet_write import PostproductionSheetBusiness
from tm_api.v24.common import Rejected


WORKSPACE = "b39f14f1-3d21-440e-ab14-bc63db4ae304"
ASSET_ID = "a6ffd76f-6ef0-4bf8-9de7-79dbc4f5ead0"
ENTITY_ID = "8d9e0763-2352-4bcf-ba2a-4b9de3d2d2eb"
SHEET_ID = 1378800391


def canonical_asset(*, approved=True, cloud=False, asset_class="pov", content_type=None):
    return {
        "id": ENTITY_ID,
        "revision": 1,
        "status": "active",
        "data": {
            "title": content_type or "1_Участник Один VS Участник Два",
            "state": "active",
            "asset_id": ASSET_ID,
            "sheet_id": SHEET_ID,
            "event_key": "2026.09.12_RCC HARD_GameBred+POV",
            "asset_class": asset_class,
            "content_type": content_type or "1_Участник Один VS Участник Два",
            "layout_number": 1 if asset_class == "pov" else None,
            "approval_actor": "user" if approved else "",
            "approval_basis": "user_override" if approved else "required_external_approval",
            "approval_state": "approved" if approved else "pending",
            "frame_share_id": "jAiI8ZQ1",
            "approval_reason": "no_further_approval_required",
            "sheet_row_number": 153,
            "approval_evidence": ["explicit user override"] if approved else [],
            "edit_source_status": "Нужны правки",
            "color_source_status": "Не готово" if asset_class == "non_pov" else None,
            "publication_managed": False,
            "sheet_spreadsheet_id": "1ih2tzVTI71tYu9k2CBY9Z1Eki6i0i4nOy8-w5vIfaj4",
            "sheet_row_fingerprint": "bootstrap-row",
            "requires_user_confirmation": False,
            "sheet_presentation_fingerprint": "bootstrap-presentation",
        },
    }


def workstream(kind, state, *, progress=None, action=None):
    data = {
        "asset_id": ASSET_ID,
        "kind": kind,
        "state": state,
        "completion_policy": "explicit_approval",
    }
    if progress is not None:
        data["progress_status"] = progress
    if action is not None:
        data["next_action"] = action
    return {
        "id": "11111111-1111-4111-8111-111111111111",
        "revision": 1,
        "status": "active",
        "data": data,
    }


def sheet_row(*, edit="Нужны правки", color=None, row_fp="live-row", presentation_fp="live-presentation", pov=True):
    return {
        "tab": "Бои" if pov else "Шоу",
        "sheet_id": SHEET_ID,
        "row_number": 153,
        "event": "2026.09.12_RCC HARD_GameBred+POV",
        "content_type": "1_Участник Один VS Участник Два" if pov else "Intro",
        "is_pov": pov,
        "layout_number": 1 if pov else None,
        "frame_share_id": "jAiI8ZQ1",
        "edit_status": edit,
        "color_status": color,
        "unsupported_statuses": [],
        "status_validation_matches_contract": True,
        "row_identity": {
            "spreadsheet_id": "1ih2tzVTI71tYu9k2CBY9Z1Eki6i0i4nOy8-w5vIfaj4",
            "sheet_id": SHEET_ID,
            "tab": "Бои" if pov else "Шоу",
            "row_number": 153,
            "row_fingerprint": row_fp,
        },
        "presentation_fingerprint": presentation_fp,
    }


def snapshot(row):
    return {
        "spreadsheet_id": "1ih2tzVTI71tYu9k2CBY9Z1Eki6i0i4nOy8-w5vIfaj4",
        "snapshot_fingerprint": "snapshot-" + str(row["edit_status"]) + "-" + str(row["color_status"]),
        "assets": [row],
    }


class Tx:
    def __init__(self, asset, items):
        self.asset = asset
        self.items = items

    def one(self, sql, args):
        if "tm_config.workspaces" in sql:
            return {
                "id": WORKSPACE,
                "workspace_key": "postproduction",
                "status": "active",
                "settings": {"analysis": {"profile": "postproduction"}},
                "revision": 6,
            }
        raise AssertionError(sql)

    def all(self, sql, args):
        if "entity_type='postproduction-asset'" in sql:
            return [self.asset]
        if "entity_type='postproduction-item'" in sql:
            return self.items
        raise AssertionError(sql)


def mutation():
    return {
        "operation": "update_postproduction_sheet_projection",
        "target_id": WORKSPACE,
        "workspace_id": WORKSPACE,
        "expected_revision": 6,
        "changes": {"asset_ids": [ASSET_ID]},
    }


def business(*, asset=None, items=None, row=None):
    value = PostproductionSheetBusiness("macbook-owner", "/fake/credentials.json")
    live = row or sheet_row()
    value._snapshot_rows = lambda: snapshot(live)
    return value, Tx(asset or canonical_asset(), items or [])


def test_completed_canonical_edit_projects_to_ready():
    b, tx = business(items=[workstream("edit", "completed", progress="done")])
    plan = b.plan(tx, mutation())
    op = plan["_delivery"]["operations"][0]
    assert op["range"] == "'Бои'!E153"
    assert op["before"] == "Нужны правки"
    assert op["after"] == "Готово"
    assert plan["before"]["authority"] == "canonical_server_state"


def test_waiting_feedback_stays_needs_corrections_without_sheet_authority():
    b, tx = business(
        asset=canonical_asset(approved=False),
        items=[workstream("edit", "waiting_feedback", progress="waiting_feedback")],
    )
    with pytest.raises(Rejected, match="projection_no_changes"):
        b.plan(tx, mutation())


def test_active_corrections_projects_to_in_progress_even_if_sheet_says_waiting():
    b, tx = business(
        asset=canonical_asset(approved=False),
        items=[workstream("edit", "active", progress="corrections")],
    )
    plan = b.plan(tx, mutation())
    op = plan["_delivery"]["operations"][0]
    assert op["before"] == "Нужны правки"
    assert op["after"] == "В работе"


def test_approved_waiting_feedback_projects_to_ready_from_server_approval():
    b, tx = business(
        asset=canonical_asset(approved=True),
        items=[workstream("edit", "waiting_feedback", progress="waiting_feedback")],
    )
    plan = b.plan(tx, mutation())
    assert plan["_delivery"]["operations"][0]["after"] == "Готово"


def test_non_pov_color_and_edit_are_projected_independently():
    asset = canonical_asset(
        approved=False, asset_class="non_pov", content_type="Intro"
    )
    row = sheet_row(edit="Нужны правки", color="Не готово", pov=False)
    b, tx = business(
        asset=asset,
        items=[
            workstream("edit", "waiting_feedback", progress="waiting_feedback"),
            workstream("color", "active", progress="in_progress"),
        ],
        row=row,
    )
    plan = b.plan(tx, mutation())
    operations = plan["_delivery"]["operations"]
    assert len(operations) == 1
    assert operations[0]["field"] == "color_status"
    assert operations[0]["range"] == "'Шоу'!F153"
    assert operations[0]["after"] == "В работе"


def test_sheet_drift_before_preview_is_corrected_not_imported():
    b, tx = business(
        asset=canonical_asset(approved=False),
        items=[workstream("edit", "waiting_feedback", progress="waiting_feedback")],
        row=sheet_row(edit="В работе"),
    )
    plan = b.plan(tx, mutation())
    op = plan["_delivery"]["operations"][0]
    assert op["before"] == "В работе"
    assert op["after"] == "Нужны правки"


def test_missing_workstream_does_not_infer_from_legacy_sheet_status():
    b, tx = business(asset=canonical_asset(approved=True), items=[])
    with pytest.raises(Rejected, match="projection_no_changes"):
        b.plan(tx, mutation())


def test_changed_anchor_fails_closed_before_preview():
    b, tx = business(items=[workstream("edit", "completed", progress="done")])
    changed = sheet_row()
    changed["content_type"] = "renamed or moved"
    b._snapshot_rows = lambda: snapshot(changed)
    with pytest.raises(Rejected, match="identity_changed"):
        b.plan(tx, mutation())


def test_post_commit_rejects_manual_value_change_after_preview():
    b, tx = business(items=[workstream("edit", "completed", progress="done")])
    plan = b.plan(tx, mutation())
    changed = sheet_row(edit="В работе")
    b._live_state = lambda: snapshot(changed)
    with pytest.raises(Rejected, match="cas_value_changed"):
        b.post_commit(mutation(), plan)


def test_post_commit_rejects_presentation_change_before_write():
    b, tx = business(items=[workstream("edit", "completed", progress="done")])
    plan = b.plan(tx, mutation())
    changed = sheet_row(presentation_fp="format-changed")
    b._live_state = lambda: snapshot(changed)
    with pytest.raises(Rejected, match="presentation_changed"):
        b.post_commit(mutation(), plan)


def test_post_commit_is_idempotent_when_value_already_reached():
    b, tx = business(items=[workstream("edit", "completed", progress="done")])
    plan = b.plan(tx, mutation())
    reached = sheet_row(edit="Готово", row_fp="after-row")
    b._live_state = lambda: snapshot(reached)
    result = b.post_commit(mutation(), plan)
    assert result["status"] == "already_applied"
    assert result["idempotent"] is True


def test_post_commit_writes_minimal_value_and_verifies(monkeypatch):
    b, tx = business(items=[workstream("edit", "completed", progress="done")])
    plan = b.plan(tx, mutation())
    states = iter([
        snapshot(sheet_row()),
        snapshot(sheet_row(edit="Готово", row_fp="after-row")),
    ])
    b._live_state = lambda: next(states)
    calls = []

    class Writer:
        def __init__(self, path):
            assert path == "/fake/credentials.json"

        def write_values(self, spreadsheet_id, data):
            calls.append((spreadsheet_id, copy.deepcopy(data)))

    monkeypatch.setattr("tm_api.postproduction_sheet_write.GoogleSheets", Writer)
    result = b.post_commit(mutation(), plan)
    assert result["status"] == "applied"
    assert calls == [(
        "1ih2tzVTI71tYu9k2CBY9Z1Eki6i0i4nOy8-w5vIfaj4",
        [{"range": "'Бои'!E153", "values": [["Готово"]]}],
    )]


def test_invalid_dropdown_contract_fails_before_preview():
    b, tx = business(items=[workstream("edit", "completed", progress="done")])
    changed = sheet_row()
    changed["status_validation_matches_contract"] = False
    b._snapshot_rows = lambda: snapshot(changed)
    with pytest.raises(Rejected, match="status_validation_changed"):
        b.plan(tx, mutation())
