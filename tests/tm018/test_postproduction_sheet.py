import copy

import pytest

from tm_api import postproduction_sheet as sheets


def validation():
    return {
        "condition": {
            "type": "ONE_OF_LIST",
            "values": [{"userEnteredValue": value} for value in sheets.STATUS_VALUES],
        },
        "strict": True,
        "showCustomUi": True,
    }


def cell(value=None, *, link=None, status=False, background=None):
    result = {}
    if value is not None:
        result["formattedValue"] = str(value)
        result["userEnteredValue"] = {"stringValue": str(value)}
    if link:
        result["hyperlink"] = link
        result.setdefault("userEnteredFormat", {}).setdefault("textFormat", {})["link"] = {"uri": link}
    if status:
        result["dataValidation"] = validation()
    if background:
        result.setdefault("userEnteredFormat", {})["backgroundColor"] = background
    return result


def row(*values):
    return {"values": list(values)}


def sample_payload():
    show_header = [
        cell("Название шоу"), cell("Тип контента"), cell("Ссылка Frame"), cell("Ссылка Яндекс "),
        cell(), cell("Статус цвета"), cell("Статус правок"), cell("Публикация"), cell("Ждет публикации"),
    ]
    fights_header = [
        cell("Название шоу"), cell("Тип контента"), cell("Ссылка Frame"), cell("Ссылка Яндекс "),
        cell("Статус правок"), cell("Публикация"), cell("Ждет публикации"),
    ]
    gray = {"red": 0.95, "green": 0.95, "blue": 0.95}
    return {
        "spreadsheetId": sheets.TARGET_SPREADSHEET_ID,
        "properties": {"title": sheets.TARGET_TITLE, "locale": "ru_RU", "timeZone": "Asia/Yekaterinburg"},
        "sheets": [
            {
                "properties": {"sheetId": 10, "title": sheets.SHOW_TAB, "gridProperties": {"rowCount": 100}},
                "data": [{
                    "startRow": 0,
                    "rowData": [
                        row(),
                        row(*show_header),
                        row(cell(), cell(), cell(), cell("SDR"), cell("HDR"), cell(), cell(), cell(), cell()),
                        row(
                            cell("2026.07.04_RCC_MMA_25"), cell("Backstage"),
                            cell("https://f.io/backstage-token", link="https://f.io/backstage-token"),
                            cell("ProRes 422", link="https://disk.example/sdr"),
                            cell("HEVC", link="https://disk.example/hdr"),
                            cell("Готово", status=True), cell("Готово", status=True),
                            cell("https://youtu.be/passive"), cell(),
                        ),
                        row(
                            cell(), cell("Intro"),
                            cell("https://f.io/intro-token", link="https://f.io/intro-token"),
                            cell(), cell(),
                            cell("Не готово", status=True, background=gray),
                            cell("Нужны правки", status=True, background=gray),
                            cell(), cell("да"),
                        ),
                    ],
                }],
            },
            {
                "properties": {"sheetId": 20, "title": sheets.FIGHTS_TAB, "gridProperties": {"rowCount": 100}},
                "data": [{
                    "startRow": 0,
                    "rowData": [
                        row(),
                        row(*fights_header),
                        row(),
                        row(
                            cell("2026.09.12_RCC HARD_GameBred+POV"),
                            cell("1_Участник Один VS Участник Два Рефери Участник Три"),
                            cell("https://f.io/fight-token", link="https://f.io/fight-token"),
                            cell("MOV", link="https://disk.example/fight"),
                            cell("Нужны правки", status=True), cell(), cell(),
                        ),
                        row(
                            cell(), cell("2_Тор Амиров VS Леонид Чирков Рефери Негатин Виктор"),
                            cell("https://f.io/legacy-token", link="https://f.io/legacy-token"),
                            cell(), cell("Отмена"), cell(), cell(),
                        ),
                    ],
                }],
            },
        ],
    }


def parsed_assets():
    return sheets.parse_snapshot(sample_payload())["assets"]


def by_name(rows, name):
    return next(row for row in rows if row["content_type"] == name)


def test_diagnostic_is_registered_only_on_read_surface():
    from tm_api.v24.register import WRITE_NAMES
    from tm_api.v25.register import READ_NAMES

    assert "get_postproduction_sheet_diagnostic" in READ_NAMES
    assert "get_postproduction_sheet_diagnostic" not in WRITE_NAMES


def test_read_only_adapter_has_exact_target_and_no_sheet_write_surface():
    assert sheets.READ_SCOPE.endswith("spreadsheets.readonly")
    assert sheets.TARGET_SPREADSHEET_ID == "1ih2tzVTI71tYu9k2CBY9Z1Eki6i0i4nOy8-w5vIfaj4"
    public = {name for name in dir(sheets.PostproductionSheetsReadOnly) if not name.startswith("_")}
    assert {"snapshot", "token"}.issubset(public)
    assert not any(name.startswith(("write", "update", "append", "delete", "insert")) for name in public)


def test_parser_keeps_exact_provider_row_identity_and_never_uses_name_as_identity():
    parsed = sheets.parse_snapshot(sample_payload())
    rows = parsed["assets"]
    assert len(rows) == 4
    intro = by_name(rows, "Intro")
    assert intro["event"] == "2026.07.04_RCC_MMA_25"
    assert intro["row_identity"]["sheet_id"] == 10
    assert intro["row_identity"]["row_number"] == 5
    assert len(intro["row_identity"]["row_fingerprint"]) == 64
    assert intro["frame_share_id"] == "intro-token"
    assert "content_type" not in intro["row_identity"]
    fight = by_name(rows, "1_Участник Один VS Участник Два Рефери Участник Три")
    assert fight["layout_number"] == 1
    assert fight["row_identity"]["sheet_id"] == 20
    assert fight["row_identity"]["row_number"] == 4


def test_cloud_handoff_uses_actual_hyperlink_not_display_label():
    rows = parsed_assets()
    backstage = by_name(rows, "Backstage")
    assert backstage["cloud"] == [
        {"variant": "SDR", "label": "ProRes 422", "url": "https://disk.example/sdr"},
        {"variant": "HDR", "label": "HEVC", "url": "https://disk.example/hdr"},
    ]
    fight = by_name(rows, "1_Участник Один VS Участник Два Рефери Участник Три")
    assert fight["cloud"][0]["label"] == "MOV"
    assert fight["cloud"][0]["url"] == "https://disk.example/fight"


def test_publication_is_passive_context_and_never_blocks_inventory():
    rows = parsed_assets()
    backstage = by_name(rows, "Backstage")
    assert backstage["publication_context"]["publication"] == "https://youtu.be/passive"
    assert backstage["publication_context"]["managed"] is False
    assert backstage["publication_context"]["blocks_completion"] is False
    inventory = sheets.unresolved_inventory(rows)
    assert backstage not in inventory["items"]


def test_inventory_separates_active_unresolved_from_legacy_status():
    inventory = sheets.unresolved_inventory(parsed_assets())
    assert {row["content_type"] for row in inventory["items"]} == {
        "Intro",
        "1_Участник Один VS Участник Два Рефери Участник Три",
    }
    assert [row["unsupported_statuses"] for row in inventory["legacy_exceptions"]] == [["Отмена"]]


def test_binding_uses_explicit_row_or_frame_identity_but_never_name():
    rows = parsed_assets()
    assets = [
        {
            "asset_id": "asset-intro",
            "frame_share_id": "intro-token",
            "frameio_file_id": "frame-intro",
            "content_type": "something renamed",
        },
        {
            "asset_id": "asset-fight",
            "sheet_binding": {
                "spreadsheet_id": sheets.TARGET_SPREADSHEET_ID,
                "sheet_id": 20,
                "row_number": 4,
            },
            "content_type": "old name",
        },
        {
            "asset_id": "asset-name-only",
            "content_type": "Backstage",
        },
    ]
    bound, _ = sheets.bind_rows(rows, assets)
    intro = by_name(bound, "Intro")
    fight = by_name(bound, "1_Участник Один VS Участник Два Рефери Участник Три")
    backstage = by_name(bound, "Backstage")
    assert intro["asset_id"] == "asset-intro"
    assert fight["asset_id"] == "asset-fight"
    assert backstage["binding_state"] == "unbound"
    assert backstage["asset_id"] is None


def test_duplicate_frame_bridge_never_binds_one_asset_to_two_rows():
    rows = parsed_assets()
    intro = copy.deepcopy(by_name(rows, "Intro"))
    intro["row_number"] = 99
    intro["row_identity"]["row_number"] = 99
    intro["row_identity"]["row_fingerprint"] = "duplicate-row"
    rows.append(intro)
    bound, _ = sheets.bind_rows(rows, [{
        "asset_id": "asset-intro",
        "frame_share_id": "intro-token",
        "frameio_file_id": "frame-intro",
    }])
    matches = [row for row in bound if row.get("frame_share_id") == "intro-token"]
    assert len(matches) == 2
    assert {row["binding_state"] for row in matches} == {"ambiguous"}
    assert all(row["asset_id"] is None for row in matches)


def test_discrepancy_engine_is_asset_id_bound_and_dependency_aware():
    rows = parsed_assets()
    assets = [
        {
            "asset_id": "asset-backstage",
            "frame_share_id": "backstage-token",
            "frameio_file_id": "frame-backstage",
            "approval_gate": {
                "contract_version": "tm017-v1",
                "required": ["Fedya", "Gleb"],
                "approved": ["Fedya", "Gleb"],
            },
        },
        {
            "asset_id": "asset-intro",
            "frame_share_id": "intro-token",
            "frameio_file_id": "frame-intro",
            "approval_gate": {
                "contract_version": "tm017-v1",
                "required": ["Fedya", "Gleb"],
                "approved": ["Fedya"],
            },
        },
        {
            "asset_id": "asset-fight",
            "frame_share_id": "fight-token",
            "frameio_file_id": "frame-fight",
            "approval_gate": {
                "contract_version": "tm017-v1",
                "required": ["Fedya"],
                "approved": ["Fedya"],
            },
        },
    ]
    reminders = [
        {"id": "r-edit", "asset_id": "asset-backstage", "kind": "edit_corrections", "state": "open"},
        {"id": "r-upload", "asset_id": "asset-backstage", "kind": "cloud_upload", "state": "open"},
        # Same title would be unsafe; a different asset_id must not trigger a discrepancy.
        {"id": "r-other", "asset_id": "different-asset", "kind": "edit_corrections", "state": "open"},
    ]
    frame_assets = [
        {"asset_id": "asset-missing-row", "frameio_file_id": "frame-file-missing"},
    ]
    result = sheets.discrepancy_report(
        rows, assets, reminders, frame_assets,
        frameio_ready=True, approvals_ready=True,
    )
    kinds = [issue["kind"] for issue in result["issues"]]
    assert "approval_complete_sheet_needs_corrections" in kinds
    assert "non_pov_partial_approval" in kinds
    assert "sheet_ready_edit_reminder_open" in kinds
    assert "cloud_link_upload_reminder_open" in kinds
    assert "frameio_asset_without_sheet_row" in kinds
    assert not any(issue.get("reminder_id") == "r-other" for issue in result["issues"])
    assert result["dependencies"] == {
        "frameio": "ready",
        "approvals": "ready",
        "layout_naming": "pending_tm016",
    }


def test_frameio_and_approvals_fail_closed_when_dependencies_are_not_ready():
    result = sheets.discrepancy_report(
        parsed_assets(), [], [], [],
        frameio_ready=False, approvals_ready=False,
    )
    kinds = {issue["kind"] for issue in result["issues"]}
    assert "frameio_asset_without_sheet_row" not in kinds
    assert "approval_complete_sheet_needs_corrections" not in kinds
    assert "non_pov_partial_approval" not in kinds
    assert result["dependencies"]["frameio"] == "unavailable"
    assert result["dependencies"]["approvals"] == "pending_tm017"


def test_projection_preview_is_guarded_and_preserves_sheet_presentation():
    rows = parsed_assets()
    intro = by_name(rows, "Intro")
    assets = [
        {
            "asset_id": "asset-intro",
            "frame_share_id": "intro-token",
            "frameio_file_id": "frame-intro",
            "projection": {"edit_status": "В работе", "color_status": "Готово"},
        },
        {
            "asset_id": "asset-new",
            "frame_share_id": "not-in-sheet",
            "projection": {"edit_status": "Не готово"},
        },
    ]
    preview = sheets.build_projection_preview(rows, assets)
    assert preview["mode"] == "guarded_projection"
    assert preview["writes_enabled"] is True
    assert preview["apply_available"] is True
    assert preview["guards"]["cas_before_every_write"] is True
    assert preview["guards"]["publication_columns_managed"] is False
    assert preview["guards"]["rcc_show_row_insert_enabled"] is True
    assert preview["guards"]["row_insert_requires_canonical_rcc_job"] is True
    assert preview["guards"]["pov_counter_managed_by_asset_registry"] is False
    assert preview["guards"]["pov_duration_inference_forbidden"] is True
    assert preview["guards"]["existing_pov_numbers_authoritative"] is True
    assert {op["field"] for op in preview["operations"]} == {"edit_status", "color_status"}
    assert all(op["expected_row_identity"]["row_number"] == intro["row_number"] for op in preview["operations"])
    assert all(op["preserve"] == ["data_validation", "formatting", "formula"] for op in preview["operations"])
    assert preview["blockers"] == [{
        "kind": "asset_row_not_bound",
        "asset_id": "asset-new",
        "reason": "use_rcc_job_bootstrap_or_explicit_binding",
    }]


def test_projection_cas_rejects_concurrent_row_or_presentation_change():
    rows = parsed_assets()
    preview = sheets.build_projection_preview(rows, [{
        "asset_id": "asset-intro",
        "frame_share_id": "intro-token",
        "frameio_file_id": "frame-intro",
        "projection": {"edit_status": "В работе"},
    }])
    assert sheets.validate_preview_cas(preview, rows) is True

    changed = copy.deepcopy(rows)
    target = by_name(changed, "Intro")
    target["row_identity"]["row_fingerprint"] = "changed"
    with pytest.raises(sheets.PostproductionSheetError, match="postproduction_sheet_cas_mismatch"):
        sheets.validate_preview_cas(preview, changed)

    changed = copy.deepcopy(rows)
    target = by_name(changed, "Intro")
    target["presentation_fingerprint"] = "changed"
    with pytest.raises(sheets.PostproductionSheetError, match="postproduction_sheet_presentation_changed"):
        sheets.validate_preview_cas(preview, changed)
