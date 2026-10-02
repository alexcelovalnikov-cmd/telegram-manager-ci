"""Read-only Postproduction Google Sheet adapter and guarded projection planning.

TM-018 milestone 1 deliberately has no Google Sheet write surface. The Sheet is an
observed working projection; canonical Postproduction state must be established
before a future guarded preview/apply path can mutate any cell.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from tm_api.v24.common import Rejected
from .postproduction_identity import postproduction_reminder_id


TARGET_SPREADSHEET_ID = "1ih2tzVTI71tYu9k2CBY9Z1Eki6i0i4nOy8-w5vIfaj4"
TARGET_TITLE = "Правки по бэкстейджам"
SHOW_TAB = "Шоу"
FIGHTS_TAB = "Бои"
TAB_COLUMNS = {SHOW_TAB: 9, FIGHTS_TAB: 7}
STATUS_VALUES = ("Готово", "Нужны правки", "В работе", "Делает Майя", "Не готово")
READ_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
PLAN_CONTRACT = "tm-postproduction-sheet-projection/v2"
MAX_RESPONSE = 4_000_000


class PostproductionSheetError(RuntimeError):
    pass


def _digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(raw).hexdigest()


def _b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _norm(value):
    return " ".join(str(value or "").strip().casefold().replace("ё", "е").split())


def _quote_sheet(value):
    return "'" + str(value).replace("'", "''") + "'"


def _column_name(index):
    out = ""
    value = index + 1
    while value:
        value, rem = divmod(value - 1, 26)
        out = chr(65 + rem) + out
    return out


class PostproductionSheetsReadOnly:
    """Minimal Google Sheets client with a read-only OAuth scope and GET-only API."""

    def __init__(self, credentials_path, *, clock=time.time, opener=urllib.request.urlopen):
        path = Path(credentials_path or "")
        if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_size > 20000:
            raise PostproductionSheetError("google_credentials_unavailable")
        try:
            value = json.loads(path.read_text())
        except Exception:
            raise PostproductionSheetError("google_credentials_invalid") from None
        if (
            value.get("type") != "service_account"
            or value.get("token_uri") != "https://oauth2.googleapis.com/token"
            or not str(value.get("client_email") or "").endswith(".gserviceaccount.com")
            or not str(value.get("private_key") or "").startswith("-----BEGIN PRIVATE KEY-----")
        ):
            raise PostproductionSheetError("google_credentials_invalid")
        self.value = value
        self.clock = clock
        self.opener = opener
        self._token = None
        self._expires = 0

    def _read_json(self, request, *, token_request=False):
        try:
            with self.opener(request, timeout=15) as response:
                raw = response.read(MAX_RESPONSE + 1)
        except urllib.error.HTTPError as exc:
            if token_request:
                raise PostproductionSheetError("google_credentials_rejected") from None
            if exc.code == 403:
                raise PostproductionSheetError("google_sheets_forbidden_or_api_disabled") from None
            if exc.code == 404:
                raise PostproductionSheetError("google_spreadsheet_or_sheet_not_found") from None
            if exc.code == 401:
                raise PostproductionSheetError("google_credentials_rejected") from None
            raise PostproductionSheetError("google_sheets_http_" + str(exc.code)) from None
        except Exception:
            raise PostproductionSheetError("google_network_unavailable") from None
        if len(raw) > MAX_RESPONSE:
            raise PostproductionSheetError("google_response_too_large")
        try:
            return json.loads(raw or b"{}")
        except Exception:
            raise PostproductionSheetError("google_invalid_response") from None

    def token(self):
        now = int(self.clock())
        if self._token and now < self._expires - 60:
            return self._token
        header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
        claim = _b64(json.dumps({
            "iss": self.value["client_email"],
            "scope": READ_SCOPE,
            "aud": "https://oauth2.googleapis.com/token",
            "iat": now,
            "exp": now + 3600,
        }, separators=(",", ":")).encode())
        unsigned = (header + "." + claim).encode()
        try:
            key = serialization.load_pem_private_key(self.value["private_key"].encode(), password=None)
            signature = key.sign(unsigned, padding.PKCS1v15(), hashes.SHA256())
        except Exception:
            raise PostproductionSheetError("google_credentials_invalid") from None
        assertion = unsigned.decode() + "." + _b64(signature)
        body = urllib.parse.urlencode({
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        }).encode()
        request = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        payload = self._read_json(request, token_request=True)
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise PostproductionSheetError("google_token_missing")
        self._token = token
        self._expires = now + int(payload.get("expires_in") or 3600)
        return token

    def _get(self, spreadsheet_id, query):
        if spreadsheet_id != TARGET_SPREADSHEET_ID:
            raise PostproductionSheetError("unexpected_postproduction_spreadsheet")
        url = "https://sheets.googleapis.com/v4/spreadsheets/" + spreadsheet_id
        url += "?" + urllib.parse.urlencode(query, doseq=True)
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"Authorization": "Bearer " + self.token(), "Accept": "application/json"},
        )
        return self._read_json(request)

    def snapshot(self, spreadsheet_id=TARGET_SPREADSHEET_ID):
        ranges = [
            f"{_quote_sheet(SHOW_TAB)}!A:I",
            f"{_quote_sheet(FIGHTS_TAB)}!A:G",
        ]
        fields = (
            "spreadsheetId,properties(title,locale,timeZone),"
            "sheets(properties(sheetId,title,gridProperties),"
            "data(startRow,startColumn,rowData(values("
            "formattedValue,userEnteredValue,hyperlink,dataValidation,userEnteredFormat))))"
        )
        payload = self._get(spreadsheet_id, {
            "includeGridData": "true",
            "ranges": ranges,
            "fields": fields,
        })
        validate_snapshot(payload)
        return payload


def validate_snapshot(payload):
    if payload.get("spreadsheetId") != TARGET_SPREADSHEET_ID:
        raise PostproductionSheetError("unexpected_postproduction_spreadsheet")
    if (payload.get("properties") or {}).get("title") != TARGET_TITLE:
        raise PostproductionSheetError("unexpected_postproduction_sheet_title")
    sheets = payload.get("sheets") or []
    by_title = {(sheet.get("properties") or {}).get("title"): sheet for sheet in sheets}
    if set(by_title) != {SHOW_TAB, FIGHTS_TAB}:
        raise PostproductionSheetError("postproduction_sheet_tabs_changed")
    ids = [(by_title[name].get("properties") or {}).get("sheetId") for name in (SHOW_TAB, FIGHTS_TAB)]
    if any(type(value) is not int for value in ids) or len(set(ids)) != 2:
        raise PostproductionSheetError("postproduction_sheet_ids_invalid")


def _cell_snapshot(cell):
    entered = cell.get("userEnteredValue") or {}
    formula = entered.get("formulaValue")
    if "stringValue" in entered:
        raw = entered["stringValue"]
    elif "numberValue" in entered:
        raw = entered["numberValue"]
    elif "boolValue" in entered:
        raw = entered["boolValue"]
    elif formula is not None:
        raw = formula
    else:
        raw = None
    link = cell.get("hyperlink")
    if not link:
        link = ((((cell.get("userEnteredFormat") or {}).get("textFormat") or {}).get("link") or {}).get("uri"))
    return {
        "value": raw,
        "formatted": cell.get("formattedValue"),
        "formula": formula,
        "hyperlink": link,
        "validation": cell.get("dataValidation"),
        "format": cell.get("userEnteredFormat"),
    }


def _grid_rows(sheet, width):
    result = {}
    for block in sheet.get("data") or []:
        start_row = int(block.get("startRow") or 0)
        start_col = int(block.get("startColumn") or 0)
        for row_offset, row_data in enumerate(block.get("rowData") or []):
            row = result.setdefault(start_row + row_offset, [{} for _ in range(width)])
            for col_offset, cell in enumerate(row_data.get("values") or []):
                col = start_col + col_offset
                if 0 <= col < width:
                    row[col] = _cell_snapshot(cell)
    return result


def _display(cell):
    if not cell:
        return ""
    value = cell.get("formatted")
    if value is None:
        value = cell.get("value")
    return "" if value is None else str(value).strip()


def _link(cell):
    value = str((cell or {}).get("hyperlink") or "").strip()
    if value:
        return value
    displayed = _display(cell)
    return displayed if displayed.startswith(("https://", "http://")) else ""


def _frame_share_id(url):
    if not url:
        return None
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if parsed.hostname not in ("f.io", "www.f.io"):
        return None
    token = parsed.path.strip("/").split("/", 1)[0]
    return token if re.fullmatch(r"[A-Za-z0-9_-]{4,200}", token or "") else None


def _validation_values(cell):
    condition = ((cell or {}).get("validation") or {}).get("condition") or {}
    values = condition.get("values") or []
    return tuple(str(item.get("userEnteredValue")) for item in values if item.get("userEnteredValue") is not None)


def _row_fingerprint(cells):
    return _digest([
        {
            "value": cell.get("value"),
            "formatted": cell.get("formatted"),
            "formula": cell.get("formula"),
            "hyperlink": cell.get("hyperlink"),
        }
        for cell in cells
    ])


def _presentation_fingerprint(cells):
    return _digest([
        {
            "formula": cell.get("formula"),
            "validation": cell.get("validation"),
            "format": cell.get("format"),
        }
        for cell in cells
    ])


def parse_snapshot(payload):
    validate_snapshot(payload)
    spreadsheet_id = payload["spreadsheetId"]
    assets = []
    sheet_summaries = {}
    for sheet in payload.get("sheets") or []:
        props = sheet.get("properties") or {}
        title = props.get("title")
        width = TAB_COLUMNS[title]
        rows = _grid_rows(sheet, width)
        header = [_display(cell) for cell in rows.get(1, [{} for _ in range(width)])]
        expected = (
            ["Название шоу", "Тип контента", "Ссылка Frame", "Ссылка Яндекс", "", "Статус цвета", "Статус правок", "Публикация", "Ждет публикации"]
            if title == SHOW_TAB
            else ["Название шоу", "Тип контента", "Ссылка Frame", "Ссылка Яндекс", "Статус правок", "Публикация", "Ждет публикации"]
        )
        if header != expected:
            raise PostproductionSheetError("postproduction_sheet_header_changed")
        current_event = ""
        parsed = 0
        last_used = 3
        for zero_row in sorted(rows):
            if zero_row < 3:
                continue
            cells = rows[zero_row]
            if any(_display(cell) for cell in cells):
                last_used = zero_row + 1
            event_cell = _display(cells[0])
            asset_name = _display(cells[1])
            if event_cell:
                current_event = event_cell
            if not asset_name:
                continue
            frame_url = _link(cells[2])
            if title == SHOW_TAB:
                color_status = _display(cells[5])
                edit_status = _display(cells[6])
                status_cells = [cells[5], cells[6]]
                cloud = [
                    {"variant": "SDR", "label": _display(cells[3]), "url": _link(cells[3])},
                    {"variant": "HDR", "label": _display(cells[4]), "url": _link(cells[4])},
                ]
                publication = {"publication": _display(cells[7]), "waiting_publication": _display(cells[8])}
                layout_number = None
            else:
                color_status = None
                edit_status = _display(cells[4])
                status_cells = [cells[4]]
                cloud = [{"variant": "default", "label": _display(cells[3]), "url": _link(cells[3])}]
                publication = {"publication": _display(cells[5]), "waiting_publication": _display(cells[6])}
                match = re.match(r"^(\d+)_", asset_name)
                layout_number = int(match.group(1)) if match else None
            invalid = [value for value in (edit_status, color_status) if value and value not in STATUS_VALUES]
            validations = [_validation_values(cell) for cell in status_cells]
            assets.append({
                "tab": title,
                "sheet_id": props["sheetId"],
                "row_number": zero_row + 1,
                "event": current_event,
                "content_type": asset_name,
                "is_pov": title == FIGHTS_TAB,
                "layout_number": layout_number,
                "frame_url": frame_url or None,
                "frame_share_id": _frame_share_id(frame_url),
                "cloud": cloud,
                "edit_status": edit_status or None,
                "color_status": color_status or None,
                "publication_context": {**publication, "managed": False, "blocks_completion": False},
                "status_validation_matches_contract": all(values == STATUS_VALUES for values in validations),
                "unsupported_statuses": invalid,
                "row_identity": {
                    "spreadsheet_id": spreadsheet_id,
                    "sheet_id": props["sheetId"],
                    "tab": title,
                    "row_number": zero_row + 1,
                    "row_fingerprint": _row_fingerprint(cells),
                },
                "presentation_fingerprint": _presentation_fingerprint(cells),
            })
            parsed += 1
        sheet_summaries[title] = {
            "sheet_id": props["sheetId"],
            "asset_rows": parsed,
            "last_used_row": last_used,
            "header_fingerprint": _digest(header),
        }
    return {
        "spreadsheet_id": spreadsheet_id,
        "title": (payload.get("properties") or {}).get("title"),
        "assets": assets,
        "sheets": sheet_summaries,
        "snapshot_fingerprint": _digest([
            {"identity": row["row_identity"], "presentation": row["presentation_fingerprint"]}
            for row in assets
        ]),
    }


def _cloud_present(row):
    return any(item.get("url") for item in row.get("cloud") or [])


def unresolved_inventory(rows):
    items = []
    legacy = []
    for row in rows:
        if row.get("unsupported_statuses"):
            legacy.append(row)
            continue
        edit_unresolved = row.get("edit_status") != "Готово"
        color_unresolved = not row.get("is_pov") and row.get("color_status") != "Готово"
        if edit_unresolved or color_unresolved:
            items.append(row)
    return {"items": items, "legacy_exceptions": legacy}


def _canonical_sheet_binding(data):
    """Read either legacy nested binding or normalized postproduction-asset fields."""
    explicit = data.get("sheet_binding")
    if isinstance(explicit, dict):
        return explicit
    spreadsheet_id = data.get("sheet_spreadsheet_id")
    sheet_id = data.get("sheet_id")
    row_number = data.get("sheet_row_number")
    if (
        spreadsheet_id == TARGET_SPREADSHEET_ID
        and type(sheet_id) is int
        and type(row_number) is int
        and row_number >= 1
    ):
        return {
            "spreadsheet_id": spreadsheet_id,
            "sheet_id": sheet_id,
            "row_number": row_number,
        }
    return None


def _binding_key(binding):
    if not isinstance(binding, dict):
        return None
    spreadsheet_id = binding.get("spreadsheet_id")
    sheet_id = binding.get("sheet_id")
    row_number = binding.get("row_number")
    if spreadsheet_id == TARGET_SPREADSHEET_ID and type(sheet_id) is int and type(row_number) is int:
        return spreadsheet_id, sheet_id, row_number
    return None


def bind_rows(rows, canonical_assets):
    by_position = {}
    by_share = {}
    assets_by_id = {}
    for asset in canonical_assets or []:
        asset_id = asset.get("asset_id")
        if not isinstance(asset_id, str) or not asset_id:
            continue
        assets_by_id[asset_id] = asset
        key = _binding_key(asset.get("sheet_binding"))
        if key:
            by_position.setdefault(key, []).append(asset_id)
        share = asset.get("frame_share_id")
        stable_frame_id = asset.get("frameio_file_id")
        # A short f.io token is observed Sheet evidence, not the canonical Frame identity.
        # It may bridge to a row only after TM-015 resolved it to a stable Frame file id.
        if isinstance(share, str) and share and isinstance(stable_frame_id, str) and stable_frame_id:
            by_share.setdefault(share, []).append(asset_id)

    bound = []
    for row in rows:
        key = (row["row_identity"]["spreadsheet_id"], row["sheet_id"], row["row_number"])
        explicit = set(by_position.get(key, []))
        frame = set(by_share.get(row.get("frame_share_id"), [])) if row.get("frame_share_id") else set()
        candidates = explicit | frame
        if len(explicit) == 1 and (not frame or frame == explicit):
            asset_id = next(iter(explicit))
            state = "bound"
            via = "sheet_binding"
        elif not explicit and len(frame) == 1:
            asset_id = next(iter(frame))
            state = "bound"
            via = "frame_bridge"
        elif candidates:
            asset_id = None
            state = "ambiguous"
            via = None
        else:
            asset_id = None
            state = "unbound"
            via = None
        bound.append({
            **row,
            "asset_id": asset_id,
            "binding_state": state,
            "binding_via": via,
            "binding_candidates": sorted(candidates),
        })

    # A stable canonical asset may project to only one Sheet row. If the same
    # Frame bridge resolves multiple rows, fail closed instead of selecting by title.
    explicit_ids = {
        row["asset_id"] for row in bound
        if row.get("binding_state") == "bound" and row.get("binding_via") == "sheet_binding"
    }
    frame_rows = {}
    for index, row in enumerate(bound):
        if row.get("binding_state") == "bound" and row.get("binding_via") == "frame_bridge":
            frame_rows.setdefault(row["asset_id"], []).append(index)
    for asset_id, indexes in frame_rows.items():
        if len(indexes) > 1 or asset_id in explicit_ids:
            for index in indexes:
                row = bound[index]
                bound[index] = {
                    **row,
                    "asset_id": None,
                    "binding_state": "ambiguous",
                    "binding_via": None,
                    "binding_candidates": [asset_id],
                }
    return bound, assets_by_id


def _open_reminder(reminder):
    state = _norm(reminder.get("state") or reminder.get("status"))
    completed = reminder.get("completed")
    return completed is not True and state not in ("completed", "done", "cancelled", "canceled")


def _approval_state(asset):
    gate = asset.get("approval_gate") or {}
    required = [str(value) for value in gate.get("required") or []]
    approved = [str(value) for value in gate.get("approved") or []]
    if not required:
        return {"available": False, "complete": False, "required": [], "approved": approved}
    return {
        "available": True,
        "complete": set(required).issubset(set(approved)),
        "required": required,
        "approved": approved,
    }


def discrepancy_report(rows, canonical_assets=None, reminders=None, frame_assets=None,
                       *, frameio_ready=False, approvals_ready=False):
    bound_rows, assets_by_id = bind_rows(rows, canonical_assets or [])
    inventory = unresolved_inventory(bound_rows)
    issues = []
    unresolved_keys = {(row["sheet_id"], row["row_number"]) for row in inventory["items"]}
    for row in bound_rows:
        key = (row["sheet_id"], row["row_number"])
        if key in unresolved_keys and row["binding_state"] != "bound":
            issues.append({
                "kind": "row_not_uniquely_bound",
                "severity": "blocker",
                "row_identity": row["row_identity"],
                "event": row["event"],
                "content_type": row["content_type"],
                "binding_state": row["binding_state"],
            })
        if row.get("unsupported_statuses"):
            issues.append({
                "kind": "unsupported_sheet_status",
                "severity": "review",
                "row_identity": row["row_identity"],
                "values": row["unsupported_statuses"],
                "preserve_until_review": True,
            })
        asset = assets_by_id.get(row.get("asset_id"))
        if not asset:
            continue
        approval = _approval_state(asset)
        if approvals_ready and approval["available"]:
            if approval["complete"] and row.get("edit_status") == "Нужны правки":
                issues.append({
                    "kind": "approval_complete_sheet_needs_corrections",
                    "severity": "stale_projection",
                    "asset_id": row["asset_id"],
                    "row_identity": row["row_identity"],
                })
            if not row.get("is_pov") and set(approval["required"]) == {"Fedya", "Gleb"}:
                count = len({"Fedya", "Gleb"} & set(approval["approved"]))
                if count == 1:
                    issues.append({
                        "kind": "non_pov_partial_approval",
                        "severity": "waiting",
                        "asset_id": row["asset_id"],
                        "approved": approval["approved"],
                        "pending": sorted({"Fedya", "Gleb"} - set(approval["approved"])),
                    })
    row_by_asset = {row.get("asset_id"): row for row in bound_rows if row.get("asset_id")}
    for reminder in reminders or []:
        if not _open_reminder(reminder):
            continue
        asset_id = reminder.get("asset_id")
        row = row_by_asset.get(asset_id)
        if not row:
            continue
        kind = reminder.get("kind")
        if kind == "edit_corrections" and row.get("edit_status") == "Готово":
            issues.append({
                "kind": "sheet_ready_edit_reminder_open",
                "severity": "stale_reminder",
                "asset_id": asset_id,
                "reminder_id": reminder.get("id"),
            })
        if kind == "cloud_upload" and _cloud_present(row):
            issues.append({
                "kind": "cloud_link_upload_reminder_open",
                "severity": "stale_reminder",
                "asset_id": asset_id,
                "reminder_id": reminder.get("id"),
            })
    if frameio_ready:
        bound_ids = {row.get("asset_id") for row in bound_rows if row.get("asset_id")}
        for frame in frame_assets or []:
            asset_id = frame.get("asset_id")
            if asset_id and asset_id not in bound_ids:
                issues.append({
                    "kind": "frameio_asset_without_sheet_row",
                    "severity": "missing_projection",
                    "asset_id": asset_id,
                    "frameio_file_id": frame.get("frameio_file_id"),
                })
    return {
        "dependencies": {
            "frameio": "ready" if frameio_ready else "unavailable",
            "approvals": "ready" if approvals_ready else "pending_tm017",
            "layout_naming": "pending_tm016",
        },
        "issues": issues,
        "counts": {
            "issues": len(issues),
            "unresolved_assets": len(inventory["items"]),
            "legacy_exceptions": len(inventory["legacy_exceptions"]),
            "bound_assets": sum(row["binding_state"] == "bound" for row in bound_rows),
            "ambiguous_assets": sum(row["binding_state"] == "ambiguous" for row in bound_rows),
        },
        "unresolved_inventory": inventory,
    }


def build_projection_preview(rows, canonical_assets):
    bound_rows, assets_by_id = bind_rows(rows, canonical_assets or [])
    operations = []
    blockers = []
    for row in bound_rows:
        asset = assets_by_id.get(row.get("asset_id"))
        if not asset:
            continue
        projection = asset.get("projection") or {}
        desired_edit = projection.get("edit_status")
        desired_color = projection.get("color_status")
        if desired_edit is not None and desired_edit not in STATUS_VALUES:
            raise PostproductionSheetError("invalid_projection_edit_status")
        if desired_color is not None and desired_color not in STATUS_VALUES:
            raise PostproductionSheetError("invalid_projection_color_status")
        if desired_edit is not None and desired_edit != row.get("edit_status"):
            col = 4 if row["is_pov"] else 6
            operations.append({
                "kind": "set_cell_value",
                "asset_id": row["asset_id"],
                "field": "edit_status",
                "range": f"{_quote_sheet(row['tab'])}!{_column_name(col)}{row['row_number']}",
                "before": row.get("edit_status"),
                "after": desired_edit,
                "expected_row_identity": row["row_identity"],
                "expected_row_fingerprint": row["row_identity"]["row_fingerprint"],
                "expected_presentation_fingerprint": row["presentation_fingerprint"],
                "preserve": ["data_validation", "formatting", "formula"],
            })
        if not row["is_pov"] and desired_color is not None and desired_color != row.get("color_status"):
            operations.append({
                "kind": "set_cell_value",
                "asset_id": row["asset_id"],
                "field": "color_status",
                "range": f"{_quote_sheet(row['tab'])}!F{row['row_number']}",
                "before": row.get("color_status"),
                "after": desired_color,
                "expected_row_identity": row["row_identity"],
                "expected_row_fingerprint": row["row_identity"]["row_fingerprint"],
                "expected_presentation_fingerprint": row["presentation_fingerprint"],
                "preserve": ["data_validation", "formatting", "formula"],
            })
    for asset in canonical_assets or []:
        if asset.get("asset_id") not in {row.get("asset_id") for row in bound_rows}:
            blockers.append({
                "kind": "asset_row_not_bound",
                "asset_id": asset.get("asset_id"),
                "reason": "use_rcc_job_bootstrap_or_explicit_binding",
            })
    source_fingerprint = _digest([
        row["row_identity"]["row_fingerprint"] for row in bound_rows
    ])
    canonical_fingerprint = _digest(canonical_assets or [])
    plan_token = _digest({
        "contract": PLAN_CONTRACT,
        "source_fingerprint": source_fingerprint,
        "canonical_fingerprint": canonical_fingerprint,
        "operations": operations,
    })
    return {
        "contract_version": PLAN_CONTRACT,
        "mode": "guarded_projection",
        "writes_enabled": True,
        "apply_available": True,
        "source_fingerprint": source_fingerprint,
        "canonical_fingerprint": canonical_fingerprint,
        "plan_token": plan_token,
        "operations": operations,
        "blockers": blockers,
        "guards": {
            "canonical_state_is_source": True,
            "blind_writes_forbidden": True,
            "cas_before_every_write": True,
            "cas_requires_exact_row_fingerprint": True,
            "minimal_cell_updates_only": True,
            "preserve_dropdown_validation": True,
            "preserve_formatting": True,
            "preserve_formulas": True,
            "rcc_show_row_insert_enabled": True,
            "row_insert_requires_canonical_rcc_job": True,
            "row_insert_append_only": True,
            "publication_columns_managed": False,
            "pov_counter_managed_by_asset_registry": False,
            "pov_duration_inference_forbidden": True,
            "existing_pov_numbers_authoritative": True,
        },
    }


def validate_preview_cas(preview, current_rows):
    """Validate an immutable preview against a fresh exact-row re-read."""
    current = {
        (row["row_identity"]["spreadsheet_id"], row["sheet_id"], row["row_number"]): row
        for row in current_rows
    }
    for operation in preview.get("operations") or []:
        expected = operation.get("expected_row_identity") or {}
        key = (expected.get("spreadsheet_id"), expected.get("sheet_id"), expected.get("row_number"))
        row = current.get(key)
        if not row:
            raise PostproductionSheetError("postproduction_sheet_cas_mismatch")
        if row["row_identity"]["row_fingerprint"] != operation.get("expected_row_fingerprint"):
            raise PostproductionSheetError("postproduction_sheet_cas_mismatch")
        if row.get("presentation_fingerprint") != operation.get("expected_presentation_fingerprint"):
            raise PostproductionSheetError("postproduction_sheet_presentation_changed")
    return True



def normalize_projected_reminders(instance, workspace_id, canonical_assets, reminders):
    identity = {}
    for asset in canonical_assets:
        for workstream in ("edit", "color"):
            rid = postproduction_reminder_id(
                instance, workspace_id, asset["asset_id"], workstream
            )
            identity[rid] = {
                "asset_id": asset["asset_id"],
                "workstream": workstream,
            }
    normalized = []
    for reminder in reminders:
        fields = reminder.get("fields") or {}
        binding = identity.get(str(reminder["id"])) or {}
        workstream = binding.get("workstream")
        title = fields.get("title") or ""
        kind = None
        if workstream == "edit":
            kind = (
                "cloud_upload"
                if title.startswith("Согласовано, можно загружать")
                else "edit_corrections"
            )
        elif workstream == "color":
            kind = "color"
        status = fields.get("status")
        normalized.append({
            "id": str(reminder["id"]),
            "asset_id": binding.get("asset_id"),
            "kind": kind,
            "state": status,
            "completed": status in ("completed", "cancelled"),
        })
    return normalized


def _server_context(database, instance, workspace_id):
    with database.transaction(read_only=True) as tx:
        workspace = tx.one(
            "SELECT id,workspace_key,status,settings,revision FROM tm_config.workspaces "
            "WHERE instance_id=%s AND id=%s::uuid",
            (instance, workspace_id),
        )
        if not workspace or workspace.get("status") != "active":
            raise Rejected("postproduction_workspace_not_found")
        analysis = ((workspace.get("settings") or {}).get("analysis") or {})
        if analysis.get("profile") != "postproduction":
            raise Rejected("postproduction_workspace_required")
        entities = tx.all(
            "SELECT id,entity_type,data,revision,status FROM tm_config.entities "
            "WHERE instance_id=%s AND workspace_id=%s::uuid AND status='active' "
            "ORDER BY updated_at DESC,id LIMIT 500",
            (instance, workspace_id),
        )
        destination = ((workspace.get("settings") or {}).get("reminder_list") or {})
        reminders = []
        if destination.get("id"):
            reminders = tx.all(
                "SELECT id,fields,revision FROM tm_calendar.events "
                "WHERE calendar_id=%s::uuid AND deleted_at IS NULL ORDER BY id LIMIT 500",
                (destination["id"],),
            )
    canonical_assets = []
    for entity in entities:
        data = entity.get("data") or {}
        asset_id = data.get("asset_id")
        if not isinstance(asset_id, str) or not asset_id:
            continue
        canonical_assets.append({
            "asset_id": asset_id,
            "entity_id": str(entity["id"]),
            "entity_revision": entity["revision"],
            "sheet_binding": _canonical_sheet_binding(data),
            "frame_share_id": data.get("frame_share_id"),
            "frameio_file_id": data.get("frameio_file_id"),
            "approval_gate": data.get("approval_gate"),
            "projection": data.get("projection"),
        })
    normalized_reminders = normalize_projected_reminders(
        instance, workspace["id"], canonical_assets, reminders
    )
    return {
        "workspace": {
            "id": str(workspace["id"]),
            "workspace_key": workspace.get("workspace_key"),
            "revision": workspace.get("revision"),
        },
        "managed_entities": len(entities),
        "canonical_assets": canonical_assets,
        "reminders": normalized_reminders,
    }


def read_only_diagnostic(database, instance, credentials_path, workspace_id, *, frameio_status=None):
    server = _server_context(database, instance, workspace_id)
    payload = PostproductionSheetsReadOnly(credentials_path).snapshot()
    parsed = parse_snapshot(payload)
    frameio_status = frameio_status or {}
    frameio_ready = bool(frameio_status.get("enabled") and frameio_status.get("authorized"))
    # TM-017 approval contracts are intentionally not inferred from legacy managed entities.
    approvals_ready = bool(
        server["canonical_assets"]
        and all((asset.get("approval_gate") or {}).get("contract_version") for asset in server["canonical_assets"])
    )
    report = discrepancy_report(
        parsed["assets"],
        server["canonical_assets"],
        server["reminders"],
        frame_assets=[
            asset for asset in server["canonical_assets"]
            if asset.get("frameio_file_id")
        ],
        frameio_ready=frameio_ready,
        approvals_ready=approvals_ready,
    )
    preview = build_projection_preview(parsed["assets"], server["canonical_assets"])
    inventory = report["unresolved_inventory"]
    return {
        "read_only": True,
        "spreadsheet": {
            "spreadsheet_id": parsed["spreadsheet_id"],
            "title": parsed["title"],
            "sheets": parsed["sheets"],
            "snapshot_fingerprint": parsed["snapshot_fingerprint"],
        },
        "workspace": server["workspace"],
        "managed_entities": server["managed_entities"],
        "canonical_asset_count": len(server["canonical_assets"]),
        "reminder_count": len(server["reminders"]),
        "asset_count": len(parsed["assets"]),
        "row_identity": {
            "exact_provider_coordinates": True,
            "row_fingerprint_cas": True,
            "name_only_binding_forbidden": True,
            "frame_share_rows": sum(bool(row.get("frame_share_id")) for row in parsed["assets"]),
        },
        "current_unresolved_asset_inventory": inventory["items"],
        "legacy_sheet_exceptions": inventory["legacy_exceptions"],
        "discrepancy_report": {
            "dependencies": report["dependencies"],
            "issues": report["issues"],
            "counts": report["counts"],
        },
        "guarded_projection": preview,
    }
