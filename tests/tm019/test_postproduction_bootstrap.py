import pytest

from tm_api.postproduction_bootstrap import (
    BOOTSTRAP_CONTRACT,
    PostproductionBootstrapOverride,
    build_bootstrap_proposal,
)
from tm_api.postproduction_sheet import PostproductionSheetError, TARGET_SPREADSHEET_ID


def row(sheet_id, row_number, event, title, *, pov=False, edit="Нужны правки", color=None,
        frame="token", cloud=None):
    return {
        "tab": "Бои" if pov else "Шоу",
        "sheet_id": sheet_id,
        "row_number": row_number,
        "event": event,
        "content_type": title,
        "is_pov": pov,
        "layout_number": 1 if pov else None,
        "frame_share_id": frame,
        "cloud": [{"variant": "default", "label": "MOV" if cloud else "", "url": cloud or ""}],
        "edit_status": edit,
        "color_status": color,
        "unsupported_statuses": [],
        "row_identity": {
            "spreadsheet_id": TARGET_SPREADSHEET_ID,
            "sheet_id": sheet_id,
            "tab": "Бои" if pov else "Шоу",
            "row_number": row_number,
            "row_fingerprint": f"row-{sheet_id}-{row_number}",
        },
        "presentation_fingerprint": f"presentation-{sheet_id}-{row_number}",
    }


def current_rows():
    return [
        row(0, 35, "2024.02_RCC_BOXING", "Intro", edit=None, color=None, frame=None),
        row(0, 67, "2026.05.30_RCC_BOXING", "Intro", color="Не готово"),
        row(0, 68, "2026.07.04_RCC_MMA_25", "Backstage", color="Готово"),
        row(0, 69, "2026.07.04_RCC_MMA_25", "Intro", color="Не готово"),
        row(1378800391, 153, "2026.09.12_RCC HARD_GameBred+POV",
            "1_Участник Один VS Участник Два Рефери Участник Три",
            pov=True, cloud="https://disk.example/pov1"),
        row(1378800391, 155, "2026.09.12_RCC HARD_GameBred+POV",
            "3_Виталий Байкал Ананин VS Олег Фомич Фомичёв Рефери Негатин Виктор",
            pov=True, cloud="https://disk.example/pov3"),
    ]


def by_row(result, sheet_id, row_number):
    return next(
        x for x in result["candidates"]
        if x["sheet_binding"]["sheet_id"] == sheet_id
        and x["sheet_binding"]["row_number"] == row_number
    )


def test_bootstrap_is_registered_read_only():
    from tm_api.v24.register import WRITE_NAMES
    from tm_api.v25.register import READ_NAMES
    assert "get_postproduction_bootstrap_proposal" in READ_NAMES
    assert "get_postproduction_bootstrap_proposal" not in WRITE_NAMES


def test_historical_2024_intro_is_ignored_and_five_active_assets_remain():
    result = build_bootstrap_proposal(current_rows())
    assert result["contract_version"] == BOOTSTRAP_CONTRACT
    assert result["read_only"] is True
    assert result["apply_available"] is False
    assert result["candidate_asset_count"] == 5
    assert [
        (x["row_identity"]["sheet_id"], x["row_identity"]["row_number"])
        for x in result["ignored_rows"]
    ] == [(0, 35)]


def test_pov_user_override_is_approved_but_never_fabricates_approver_a_approval():
    override = PostproductionBootstrapOverride(
        sheet_id=1378800391,
        row_number=153,
        decision="proceed_without_additional_approval",
        evidence_text="Правка не существенная, выгружаю без дополнительного согласования.",
    )
    result = build_bootstrap_proposal(current_rows(), overrides=[override])
    asset = by_row(result, 1378800391, 153)
    edit = next(x for x in asset["workstreams"] if x["kind"] == "edit")
    assert edit["state"] == "approved"
    assert edit["approval"]["basis"] == "user_override"
    assert edit["approval"]["actor"] == "user"
    assert edit["approval"]["required_roles"] == ["approver_a"]
    assert edit["approval"]["evidence"] == [override.evidence_text]
    assert edit["concrete_user_action"] is None


def test_weak_context_is_not_an_accepted_override_shape():
    with pytest.raises(Exception):
        PostproductionBootstrapOverride(
            sheet_id=1378800391,
            row_number=153,
            decision="small_correction",
            evidence_text="Правка маленькая",
        )


def test_non_pov_requires_approver_a_and_approver_b_and_color_is_separate():
    result = build_bootstrap_proposal(current_rows())
    asset = by_row(result, 0, 68)
    edit = next(x for x in asset["workstreams"] if x["kind"] == "edit")
    color = next(x for x in asset["workstreams"] if x["kind"] == "color")
    assert edit["approval"]["required_roles"] == ["approver_a", "approver_b"]
    assert edit["approval"]["state"] == "pending"
    assert color["approval"]["state"] == "independent"
    assert color["requires_user_confirmation"] is True
    assert color["concrete_user_action"] == "confirm_color"
    assert color["auto_reminder"] is False


def test_unknown_override_target_fails_closed():
    with pytest.raises(PostproductionSheetError, match="override_target_not_active"):
        build_bootstrap_proposal(
            current_rows(),
            overrides=[{
                "sheet_id": 1378800391,
                "row_number": 999,
                "decision": "proceed_without_additional_approval",
                "evidence_text": "Выгружаю без дополнительного согласования.",
            }],
        )


def test_existing_exact_binding_is_not_bootstrapped_twice():
    result = build_bootstrap_proposal(
        current_rows(),
        existing_assets=[{
            "asset_id": "existing",
            "sheet_binding": {
                "spreadsheet_id": TARGET_SPREADSHEET_ID,
                "sheet_id": 0,
                "row_number": 67,
            },
        }],
    )
    assert result["candidate_asset_count"] == 4
    assert not any(
        x["sheet_binding"]["sheet_id"] == 0
        and x["sheet_binding"]["row_number"] == 67
        for x in result["candidates"]
    )


def test_canonical_entity_is_asset_not_workstream():
    result = build_bootstrap_proposal(current_rows())
    asset = by_row(result, 0, 68)
    canonical = asset["canonical_entity"]
    assert result["entity_type_extension"]["entity_type"] == "postproduction-asset"
    assert canonical["entity_type"] == "postproduction-asset"
    assert canonical["data"]["asset_id"] == asset["asset_id"]
    assert canonical["data"]["content_type"] == "Backstage"
    assert canonical["data"]["edit_source_status"] == "Нужны правки"
    assert canonical["data"]["color_source_status"] == "Готово"
    assert {x["kind"] for x in asset["workstreams"]} == {"edit", "color"}


def test_one_asset_id_per_sheet_row_even_with_multiple_workstreams():
    result = build_bootstrap_proposal(current_rows())
    ids = [x["asset_id"] for x in result["candidates"]]
    assert len(ids) == len(set(ids)) == 5
    assert all(x["canonical_entity"]["data"]["asset_id"] == x["asset_id"] for x in result["candidates"])


def test_normalized_asset_fields_restore_exact_sheet_binding():
    from tm_api.postproduction_sheet import _canonical_sheet_binding
    binding = _canonical_sheet_binding({
        "sheet_spreadsheet_id": TARGET_SPREADSHEET_ID,
        "sheet_id": 1378800391,
        "sheet_row_number": 153,
    })
    assert binding == {
        "spreadsheet_id": TARGET_SPREADSHEET_ID,
        "sheet_id": 1378800391,
        "row_number": 153,
    }


def test_bootstrap_skips_existing_asset_after_normalized_binding_reconstruction():
    from tm_api.postproduction_sheet import _canonical_sheet_binding
    raw = {
        "asset_id": "existing",
        "sheet_spreadsheet_id": TARGET_SPREADSHEET_ID,
        "sheet_id": 0,
        "sheet_row_number": 67,
    }
    result = build_bootstrap_proposal(
        current_rows(),
        existing_assets=[{
            "asset_id": raw["asset_id"],
            "sheet_binding": _canonical_sheet_binding(raw),
        }],
    )
    assert result["candidate_asset_count"] == 4
    assert all(x["sheet_binding"]["row_number"] != 67 for x in result["candidates"])
