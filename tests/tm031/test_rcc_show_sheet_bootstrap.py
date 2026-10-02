import pytest

from tm_api.postproduction_show_sheet import build_insert_requests, plan_show_rows
from tm_api.postproduction_sheet_write import PostproductionSheetBusiness
from tm_api.v24.common import Rejected


RULE = {
    "sheet_project_bootstrap": {
        "default_rows": ["Backstage", "Intro"],
        "required_projection": "show_default",
        "required_shoot_state": "completed",
        "canonical_content_field": "sheet_content_types",
        "eligible_production_line": "rcc_rmk",
        "historical_taxonomy": {
            "aliases": {"Inrto": "Intro", "Performance": "Perfomance"},
            "canonical": [
                "Backstage", "Intro", "Perfomance", "Comic", "Intro_Red",
                "Intro_Blue", "Backstage с телефона/гоупро", "Intro + Backstage",
            ],
        },
    }
}


def row(number, event, content, *, edit="Готово", color="Готово", fp=None):
    return {
        "tab": "Шоу",
        "sheet_id": 0,
        "row_number": number,
        "event": event,
        "content_type": content,
        "is_pov": False,
        "edit_status": edit,
        "color_status": color,
        "presentation_fingerprint": fp or f"presentation-{number}",
        "row_identity": {"row_fingerprint": f"row-{number}"},
    }


def parsed(*extra, last_used=70):
    base = [
        row(60, "2026.01.01_RCC_TEST", "Perfomance"),
        row(68, "2026.07.04_RCC_MMA_25", "Backstage"),
        row(69, "2026.07.04_RCC_MMA_25", "Intro"),
    ]
    return {
        "spreadsheet_id": "1ih2tzVTI71tYu9k2CBY9Z1Eki6i0i4nOy8-w5vIfaj4",
        "snapshot_fingerprint": "snapshot-before",
        "assets": base + list(extra),
        "sheets": {"Шоу": {"sheet_id": 0, "last_used_row": last_used}},
    }


def job(**extra):
    data = {
        "title": "RCC HARD x GAMEBRED 12.09",
        "production_line": "rcc_rmk",
        "shoot_state": "completed",
        "sheet_projection": "show_default",
        "sheet_event_key": "2026.09.12_RCC HARD_GameBred",
        "sheet_content_types": ["Backstage", "Intro"],
    }
    data.update(extra)
    return {"id": "a7e9282d-3b10-407e-b45e-76614430f9d0", "data": data}


def backstage_item(state="active"):
    return {
        "id": "f9505c8d-723f-4cf6-b425-07a1ddbcecc8",
        "data": {
            "kind": "edit",
            "state": state,
            "title": "RCC HARD x GAMEBRED 12.09 / Backstage / Монтаж",
        },
    }


def test_new_rcc_show_gets_backstage_and_intro_at_tail():
    value = plan_show_rows(
        parsed(), [job()], {job()["id"]: [backstage_item()]}, RULE
    )
    ops = value["operations"]
    assert [(op["row_number"], op["content_type"]) for op in ops] == [
        (71, "Backstage"), (72, "Intro")
    ]
    assert ops[0]["event_cell"] == "2026.09.12_RCC HARD_GameBred"
    assert ops[1]["event_cell"] == ""
    assert ops[0]["template_row_number"] == 68
    assert ops[1]["template_row_number"] == 69
    assert ops[0]["edit_status"] == "В работе"
    assert ops[0]["color_status"] == "Не готово"
    assert ops[1]["edit_status"] == "Не готово"


def test_non_rcc_project_is_forbidden_even_with_same_content_types():
    with pytest.raises(Rejected, match="non_rcc_project_forbidden"):
        plan_show_rows(parsed(), [job(production_line="external")], {}, RULE)


def test_planned_or_unshot_rcc_project_does_not_create_dashboard_rows():
    with pytest.raises(Rejected, match="shoot_not_completed"):
        plan_show_rows(parsed(), [job(shoot_state="planned")], {}, RULE)


def test_known_additional_type_uses_historical_template():
    value = plan_show_rows(
        parsed(), [job(sheet_content_types=["Backstage", "Intro", "Performance"])], {}, RULE
    )
    op = next(item for item in value["operations"] if item["content_type"] == "Perfomance")
    assert op["template_row_number"] == 60
    assert op["row_number"] == 73


def test_unknown_content_type_fails_closed():
    with pytest.raises(Rejected, match="unknown_content_type"):
        plan_show_rows(
            parsed(), [job(sheet_content_types=["Backstage", "Новый непонятный ролик"])], {}, RULE
        )


def test_existing_tail_block_only_appends_missing_type_without_repeating_event():
    tail = row(70, "2026.09.12_RCC HARD_GameBred", "Backstage", edit="В работе", color="Не готово")
    value = plan_show_rows(parsed(tail), [job()], {}, RULE)
    assert len(value["operations"]) == 1
    op = value["operations"][0]
    assert op["content_type"] == "Intro"
    assert op["row_number"] == 71
    assert op["event_cell"] == ""


def test_existing_non_tail_block_refuses_to_shift_established_rows():
    old = row(55, "2026.09.12_RCC HARD_GameBred", "Backstage")
    with pytest.raises(Rejected, match="existing_block_not_tail"):
        plan_show_rows(parsed(old), [job()], {}, RULE)


def test_repeat_after_rows_exist_is_idempotent_no_new_operations():
    r1 = row(71, "2026.09.12_RCC HARD_GameBred", "Backstage", edit="В работе", color="Не готово")
    r2 = row(72, "2026.09.12_RCC HARD_GameBred", "Intro", edit="Не готово", color="Не готово")
    value = plan_show_rows(parsed(r1, r2, last_used=72), [job()], {}, RULE)
    assert value["operations"] == []
    assert value["selected_jobs"][0]["missing_content_types"] == []


def test_structural_request_copies_only_format_and_validation_then_writes_values():
    value = plan_show_rows(parsed(), [job()], {}, RULE)
    delivery = {
        "sheet_id": value["sheet_id"],
        "expected_last_used_row": value["expected_last_used_row"],
        "operations": value["operations"],
    }
    requests = build_insert_requests(delivery)
    assert requests[0]["insertDimension"]["range"]["startIndex"] == 70
    paste_types = [request["copyPaste"]["pasteType"] for request in requests if "copyPaste" in request]
    assert paste_types == [
        "PASTE_FORMAT", "PASTE_DATA_VALIDATION",
        "PASTE_FORMAT", "PASTE_DATA_VALIDATION",
    ]
    updates = [request["updateCells"] for request in requests if "updateCells" in request]
    assert len(updates) == 2
    first = updates[0]["rows"][0]["values"]
    assert first[0]["userEnteredValue"]["stringValue"] == "2026.09.12_RCC HARD_GameBred"
    assert first[1]["userEnteredValue"]["stringValue"] == "Backstage"
    assert first[5]["userEnteredValue"]["stringValue"] == "Не готово"
    assert first[6]["userEnteredValue"]["stringValue"] == "Не готово"


def test_post_commit_retry_detects_already_inserted_rows_without_write(monkeypatch):
    business = PostproductionSheetBusiness("macbook-owner", "/unused.json")
    inserted = parsed(
        row(71, "2026.09.12_RCC HARD_GameBred", "Backstage", edit="В работе", color="Не готово"),
        row(72, "2026.09.12_RCC HARD_GameBred", "Intro", edit="Не готово", color="Не готово"),
        last_used=72,
    )
    business._live_state = lambda: inserted
    plan = plan_show_rows(parsed(), [job()], {job()["id"]: [backstage_item()]}, RULE)
    delivery = {
        "kind": "show_row_insert",
        "sheet_id": 0,
        "expected_snapshot_fingerprint": "snapshot-before",
        "expected_last_used_row": 70,
        "operations": plan["operations"],
    }
    class Writer:
        def __init__(self, path):
            raise AssertionError("writer must not be created on idempotent retry")
    monkeypatch.setattr("tm_api.postproduction_sheet_write.GoogleSheets", Writer)
    result = business._post_commit_show_insert(delivery)
    assert result["status"] == "already_applied"
    assert result["idempotent"] is True
