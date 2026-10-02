"""Canonical POV accounting block projection (TM-024).

The accounting counter is deliberately independent from edit approval and payment.
On first activation the current RCC Sheet counter is adopted as authoritative and
all already-existing canonical POV assets become the activation anchor. Only
assets appearing after that anchor can increment the counter.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from .v24.common import Rejected, check_revision, encoded, strict_fields
from .v75.sheets import GoogleSheets, SheetSyncError, _number, _norm, _total_layout

OPS = {"update_pov_accounting_projection"}
CONTRACT = "tm-pov-accounting/v1"
STATE_ENTITY_TYPE = "postproduction-pov-accounting-state"
RULE_KEY = "rcc.pov.six_fight_block"
MONTHS_RU = {
    1: "январь", 2: "февраль", 3: "март", 4: "апрель",
    5: "май", 6: "июнь", 7: "июль", 8: "август",
    9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь",
}


class PovAccountingOverride(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    asset_id: str = Field(min_length=36, max_length=36)
    kind: Literal["double_count", "close_short"]
    evidence_text: str = Field(min_length=3, max_length=2000)

    def validate_uuid(self):
        try:
            UUID(self.asset_id)
        except (ValueError, AttributeError):
            raise ValueError("asset_id must be UUID") from None
        return self


def state_entity_id(instance, workspace_id):
    return str(uuid5(NAMESPACE_URL, f"tm-pov-accounting:{instance}:{workspace_id}"))


def _validate_uuid(value, error):
    if not isinstance(value, str):
        raise Rejected(error)
    try:
        UUID(value)
    except (ValueError, AttributeError):
        raise Rejected(error) from None
    return value


def _pov_block_rows(rows):
    result = []
    for i, row in enumerate(rows):
        values = (list(row) + [""] * 6)[:6]
        if _norm(values[0]).startswith("монтаж 6 роликов pov"):
            raw = values[5]
            if isinstance(raw, bool):
                raise Rejected("invalid_pov_accounting_counter")
            try:
                count = int(raw)
            except (TypeError, ValueError):
                raise Rejected("invalid_pov_accounting_counter") from None
            if raw not in (count, float(count), str(count)):
                raise Rejected("invalid_pov_accounting_counter")
            result.append({"row": i + 1, "title": str(values[0]).strip(), "counter": count})
    return result


def _next_title(current_title, now):
    month = MONTHS_RU[now.month]
    base = f"Монтаж 6 роликов POV {month}"
    normalized = _norm(current_title)
    if not normalized.startswith(_norm(base)):
        return base
    suffix = str(current_title).rsplit("_", 1)
    if len(suffix) == 2 and suffix[1].isdigit():
        return base + "_" + str(int(suffix[1]) + 1)
    return base + "_2"


def _cell_value(value):
    if isinstance(value, bool):
        return {"userEnteredValue": {"boolValue": value}}
    if isinstance(value, (int, float)):
        return {"userEnteredValue": {"numberValue": value}}
    return {"userEnteredValue": {"stringValue": str(value or "")}}


def _rows_payload(values):
    return [{"values": [_cell_value(value) for value in row]} for row in values]


def _has_user_evidence(m):
    return any(
        isinstance(item, dict) and item.get("kind") == "user"
        and isinstance(item.get("statement"), str) and item.get("statement").strip()
        for item in (m.get("evidence") or [])
    )


class PovAccountingBusiness:
    def __init__(self, instance, credentials_path):
        self.instance = instance
        self.credentials_path = credentials_path

    def _workspaces(self, tx, m):
        post = tx.one(
            "SELECT id,workspace_key,status,settings,revision FROM tm_config.workspaces "
            "WHERE instance_id=%s AND id=%s::uuid",
            (self.instance, m["target_id"]),
        )
        if not post or post.get("status") != "active":
            raise Rejected("postproduction_workspace_not_found")
        if str(post["id"]) != str(m.get("workspace_id")):
            raise Rejected("postproduction_workspace_target_mismatch")
        if ((post.get("settings") or {}).get("analysis") or {}).get("profile") != "postproduction":
            raise Rejected("postproduction_workspace_required")
        check_revision(post["revision"], m["expected_revision"])

        strict_fields(m["changes"], ("rcc_workspace_id", "overrides"), ("rcc_workspace_id",))
        rcc_id = _validate_uuid(m["changes"]["rcc_workspace_id"], "invalid_rcc_workspace_id")
        rcc = tx.one(
            "SELECT id,workspace_key,status,settings,revision FROM tm_config.workspaces "
            "WHERE instance_id=%s AND id=%s::uuid",
            (self.instance, rcc_id),
        )
        if not rcc or rcc.get("status") != "active":
            raise Rejected("rcc_workspace_not_found")
        if ((rcc.get("settings") or {}).get("analysis") or {}).get("profile") != "rcc_settlement":
            raise Rejected("rcc_settlement_workspace_required")
        integration = ((rcc.get("settings") or {}).get("integrations") or {}).get("google_sheets")
        if (
            not isinstance(integration, dict)
            or integration.get("mode") != "rcc_settlement"
            or not integration.get("enabled", True)
        ):
            raise Rejected("rcc_google_sheet_required")
        if integration.get("write_mode") != "managed":
            raise Rejected("rcc_google_sheet_managed_mode_required")
        return post, rcc, integration

    def _rule(self, tx, rcc_workspace_id):
        row = tx.one(
            "SELECT id,body,revision FROM tm_config.documents "
            "WHERE instance_id=%s AND workspace_id=%s::uuid AND kind='rule' "
            "AND status='active' AND body->>'decision_key'=%s "
            "ORDER BY revision DESC,id DESC LIMIT 1",
            (self.instance, rcc_workspace_id, RULE_KEY),
        )
        if not row:
            raise Rejected("active_pov_accounting_rule_required")
        value = (row.get("body") or {}).get("value") or {}
        capacity = value.get("completion_threshold")
        if type(capacity) is not int or not 1 <= capacity <= 20:
            raise Rejected("invalid_pov_accounting_capacity_rule")
        if value.get("counter_column") != "F" or value.get("counter_semantics") != "edited_fights":
            raise Rejected("unsupported_pov_accounting_counter_rule")
        rows = value.get("rows")
        if not isinstance(rows, list) or len(rows) != 2:
            raise Rejected("invalid_pov_accounting_settlement_rows")
        normalized_rows = []
        for item in rows:
            if not isinstance(item, dict):
                raise Rejected("invalid_pov_accounting_settlement_rows")
            label = item.get("label")
            amount = item.get("amount_rub")
            if not isinstance(label, str) or not label.strip() or type(amount) not in (int, float) or amount <= 0:
                raise Rejected("invalid_pov_accounting_settlement_rows")
            normalized_rows.append({
                "label": label.strip(), "payer": item.get("payer") or "", "amount_rub": amount,
            })
        return {
            "id": str(row["id"]), "revision": row["revision"], "capacity": capacity,
            "rows": normalized_rows,
            "next_block_counter": int(value.get("next_block_counter", 0)),
            "include_in_total_immediately": value.get("include_in_total_immediately") is True,
            "create_next_block_before_total": value.get("create_next_block_before_total") is True,
        }

    def _assets(self, tx, workspace_id):
        rows = tx.all(
            "SELECT id,data,revision FROM tm_config.entities "
            "WHERE instance_id=%s AND workspace_id=%s::uuid AND status='active' "
            "AND entity_type='postproduction-asset' ORDER BY id LIMIT 501",
            (self.instance, workspace_id),
        )
        if len(rows) > 500:
            raise Rejected("postproduction_asset_limit")
        result = []
        seen = set()
        for row in rows:
            data = row.get("data") or {}
            if data.get("asset_class") != "pov":
                continue
            asset_id = _validate_uuid(data.get("asset_id"), "invalid_postproduction_asset_id")
            if asset_id in seen:
                raise Rejected("duplicate_canonical_postproduction_asset_id")
            seen.add(asset_id)
            result.append({
                "asset_id": asset_id,
                "entity_id": str(row["id"]),
                "sheet_id": data.get("sheet_id"),
                "sheet_row_number": data.get("sheet_row_number"),
                "event_key": data.get("event_key"),
                "layout_number": data.get("layout_number"),
            })
        result.sort(key=lambda x: (
            x["sheet_id"] if type(x["sheet_id"]) is int else 10**12,
            x["sheet_row_number"] if type(x["sheet_row_number"]) is int else 10**12,
            x["asset_id"],
        ))
        return result

    def _state(self, tx, workspace_id, lock):
        ident = state_entity_id(self.instance, workspace_id)
        suffix = " FOR UPDATE" if lock else ""
        row = tx.one(
            "SELECT id,data,revision,status FROM tm_config.entities "
            "WHERE id=%s::uuid AND instance_id=%s AND workspace_id=%s::uuid" + suffix,
            (ident, self.instance, workspace_id),
        )
        if row and (row.get("status") != "active" or (row.get("data") or {}).get("contract_version") != CONTRACT):
            raise Rejected("invalid_pov_accounting_state")
        return ident, row

    def _live_sheet(self, integration):
        client = GoogleSheets(self.credentials_path)
        spreadsheet_id = integration["spreadsheet_id"]
        sheet_name = integration.get("sheet_name", "Лист1")
        metadata = client.metadata(spreadsheet_id)
        matches = [
            (sheet.get("properties") or {}) for sheet in metadata.get("sheets") or []
            if (sheet.get("properties") or {}).get("title") == sheet_name
        ]
        if len(matches) != 1:
            raise Rejected("rcc_sheet_tab_not_found")
        rows = client.values(spreadsheet_id, sheet_name)
        blocks = _pov_block_rows(rows)
        if not blocks:
            raise Rejected("pov_accounting_sheet_block_missing")
        total_index, total_label_col, total_amount_col = _total_layout(
            [(list(row) + [""] * 10)[:10] for row in rows]
        )
        if total_index >= len(rows):
            raise Rejected("rcc_sheet_total_missing")
        total_row = (list(rows[total_index]) + [""] * 10)[:10]
        total_amount = _number(total_row[total_amount_col])
        if total_amount is None:
            raise Rejected("rcc_sheet_total_invalid")
        return {
            "client": client, "spreadsheet_id": spreadsheet_id, "sheet_name": sheet_name,
            "sheet_id": matches[0]["sheetId"], "rows": rows, "blocks": blocks,
            "current": blocks[-1], "total_index": total_index,
            "total_amount_col": total_amount_col, "total_amount": total_amount,
            "total_label_col": total_label_col,
        }

    def _overrides(self, m, assets, state):
        raw = m["changes"].get("overrides") or []
        if not isinstance(raw, list) or len(raw) > 20:
            raise Rejected("invalid_pov_accounting_overrides")
        if raw and not _has_user_evidence(m):
            raise Rejected("pov_accounting_override_requires_user_evidence")
        known = {item["asset_id"] for item in assets}
        processed = set((state or {}).get("processed_asset_ids") or [])
        anchored = set((state or {}).get("activation_anchor_asset_ids") or [])
        out = {}
        for value in raw:
            try:
                model = PovAccountingOverride.model_validate(value)
                model.validate_uuid()
            except Exception:
                raise Rejected("invalid_pov_accounting_override") from None
            if model.asset_id not in known:
                raise Rejected("pov_accounting_override_asset_not_found")
            if model.asset_id in processed or model.asset_id in anchored:
                raise Rejected("pov_accounting_override_asset_already_accounted")
            if model.asset_id in out:
                raise Rejected("duplicate_pov_accounting_override")
            out[model.asset_id] = model.model_dump()
        return out

    def plan(self, tx, m, lock=False):
        if m["operation"] not in OPS:
            raise Rejected("unsupported_pov_accounting_operation")
        post, rcc, integration = self._workspaces(tx, m)
        rule = self._rule(tx, str(rcc["id"]))
        if not rule["include_in_total_immediately"] or not rule["create_next_block_before_total"]:
            raise Rejected("unsupported_pov_accounting_projection_rule")
        assets = self._assets(tx, str(post["id"]))
        ident, state_row = self._state(tx, str(post["id"]), lock)
        live = self._live_sheet(integration)
        current = live["current"]

        if not 0 <= current["counter"] <= rule["capacity"]:
            raise Rejected("pov_accounting_counter_out_of_range")
        if state_row is None and current["counter"] >= rule["capacity"]:
            raise Rejected("pov_accounting_baseline_requires_open_block")

        before = None if state_row is None else deepcopy(state_row["data"])
        if state_row is None:
            state = {
                "contract_version": CONTRACT,
                "rcc_workspace_id": str(rcc["id"]),
                "rule_id": rule["id"],
                "rule_revision": rule["revision"],
                "capacity": rule["capacity"],
                "activation_anchor_asset_ids": [item["asset_id"] for item in assets],
                "processed_asset_ids": [],
                "active_block": {
                    "title": current["title"], "sheet_row": current["row"],
                    "baseline_counter": current["counter"], "counter": current["counter"],
                    "contributions": [],
                },
                "closed_blocks": [],
                "overrides": [],
            }
            after = deepcopy(state)
            delivery = None
            action = "activate_baseline"
        else:
            state = deepcopy(state_row["data"])
            if str(state.get("rcc_workspace_id")) != str(rcc["id"]):
                raise Rejected("pov_accounting_rcc_workspace_changed")
            if state.get("capacity") != rule["capacity"]:
                raise Rejected("pov_accounting_capacity_changed")
            active = state.get("active_block") or {}
            if active.get("title") != current["title"] or active.get("sheet_row") != current["row"]:
                raise Rejected("pov_accounting_active_block_identity_changed")
            if active.get("counter") != current["counter"]:
                raise Rejected("pov_accounting_sheet_counter_changed")
            overrides = self._overrides(m, assets, state)
            anchored = set(state.get("activation_anchor_asset_ids") or [])
            processed = set(state.get("processed_asset_ids") or [])
            pending = [item for item in assets if item["asset_id"] not in anchored | processed]
            if not pending:
                if overrides:
                    raise Rejected("pov_accounting_override_target_not_new")
                raise Rejected("pov_accounting_projection_no_changes")
            contributions = list(active.get("contributions") or [])
            count = int(active.get("counter") or 0)
            close_short = False
            closing_asset_id = None
            applied_overrides = []
            for asset in pending:
                override = overrides.get(asset["asset_id"])
                units = 2 if override and override["kind"] == "double_count" else 1
                if count + units > rule["capacity"]:
                    raise Rejected("pov_accounting_block_overflow")
                count += units
                contribution = {
                    "asset_id": asset["asset_id"], "units": units,
                    "event_key": asset.get("event_key"), "layout_number": asset.get("layout_number"),
                }
                if override:
                    contribution["override"] = override["kind"]
                    contribution["override_evidence"] = override["evidence_text"]
                    applied_overrides.append(override)
                contributions.append(contribution)
                processed.add(asset["asset_id"])
                if override and override["kind"] == "close_short":
                    close_short = True
                    closing_asset_id = asset["asset_id"]
                    if count >= rule["capacity"]:
                        raise Rejected("short_block_override_not_short")
                    break
                if count == rule["capacity"]:
                    closing_asset_id = asset["asset_id"]
                    break

            consumed = {item["asset_id"] for item in contributions}
            not_consumed_pending = [
                item for item in pending if item["asset_id"] not in consumed
            ]
            # A single guarded transition never spills assets into a newly-created block.
            # Re-run after the close so the new block identity is verified from the Sheet.
            active["contributions"] = contributions
            active["counter"] = count
            state["processed_asset_ids"] = sorted(processed)
            state["overrides"] = list(state.get("overrides") or []) + applied_overrides
            closed = count == rule["capacity"] or close_short
            if closed:
                now = datetime.now(ZoneInfo((post.get("settings") or {}).get("timezone") or "Asia/Yekaterinburg"))
                next_title = _next_title(active["title"], now)
                closed_snapshot = deepcopy(active)
                closed_snapshot.update({
                    "closed": True,
                    "close_reason": "explicit_short_block" if close_short else "capacity_reached",
                    "closed_by_asset_id": closing_asset_id,
                })
                state["closed_blocks"] = list(state.get("closed_blocks") or []) + [closed_snapshot]
                if len(state["closed_blocks"]) > 50:
                    raise Rejected("pov_accounting_history_limit")
                new_row = live["total_index"] + 1
                state["active_block"] = {
                    "title": next_title, "sheet_row": new_row,
                    "baseline_counter": rule["next_block_counter"],
                    "counter": rule["next_block_counter"], "contributions": [],
                }
                delivery = {
                    "mode": "close_and_open",
                    "spreadsheet_id": live["spreadsheet_id"], "sheet_name": live["sheet_name"],
                    "sheet_id": live["sheet_id"],
                    "current_title": current["title"], "current_row": current["row"],
                    "before_counter": current["counter"], "final_counter": count,
                    "next_title": next_title, "next_row": new_row,
                    "total_index": live["total_index"],
                    "total_amount_col": live["total_amount_col"],
                    "before_total": float(live["total_amount"]),
                    "after_total": float(live["total_amount"] + sum(
                        (_number(item["amount_rub"]) for item in rule["rows"]), start=_number(0) or 0
                    )),
                    "rows": rule["rows"],
                }
                action = "close_block_and_open_next"
            else:
                state["active_block"] = active
                delivery = {
                    "mode": "update_counter",
                    "spreadsheet_id": live["spreadsheet_id"], "sheet_name": live["sheet_name"],
                    "sheet_id": live["sheet_id"],
                    "current_title": current["title"], "current_row": current["row"],
                    "before_counter": current["counter"], "after_counter": count,
                }
                action = "increment_counter"
            after = deepcopy(state)
            after["pending_assets_deferred_to_next_pass"] = [
                item["asset_id"] for item in not_consumed_pending
            ]

        return {
            "entity": "postproduction_pov_accounting",
            "operation": m["operation"],
            "target_id": str(post["id"]),
            "before": before,
            "after": after,
            "_state_entity_id": ident,
            "_state_revision": None if state_row is None else state_row["revision"],
            "_state_data": state,
            "_delivery": delivery,
            "_diagnostics": {
                "action": action,
                "canonical_pov_asset_count": len(assets),
                "activation_anchor_count": len(state.get("activation_anchor_asset_ids") or []),
                "processed_asset_count": len(state.get("processed_asset_ids") or []),
                "current_block": state.get("active_block"),
                "override_count": len(state.get("overrides") or []),
                "guards": {
                    "duration_inference_forbidden": True,
                    "edit_approval_affects_count": False,
                    "payment_state_affects_count": False,
                    "accepted_history_is_authoritative": True,
                    "first_activation_adopts_sheet_counter": True,
                },
            },
        }

    def execute(self, tx, m, plan):
        ident = plan["_state_entity_id"]
        revision = plan["_state_revision"]
        next_revision = 1 if revision is None else revision + 1
        tx.execute(
            "INSERT INTO tm_config.entities(id,instance_id,workspace_id,entity_type,data,status,revision) "
            "VALUES(%s::uuid,%s,%s::uuid,%s,%s::jsonb,'active',%s) "
            "ON CONFLICT(id) DO UPDATE SET data=EXCLUDED.data,status='active',revision=EXCLUDED.revision,"
            "updated_at=clock_timestamp()",
            (ident, self.instance, m["workspace_id"], STATE_ENTITY_TYPE, encoded(plan["_state_data"]), next_revision),
        )
        return {
            "entity": "postproduction_pov_accounting",
            "id": ident,
            "revision": next_revision,
            "diagnostics": plan["_diagnostics"],
            "sheet_delivery_required": plan.get("_delivery") is not None,
        }

    def _read_delivery_sheet(self, delivery):
        client = GoogleSheets(self.credentials_path)
        rows = client.values(delivery["spreadsheet_id"], delivery["sheet_name"])
        blocks = _pov_block_rows(rows)
        total_index, _label_col, amount_col = _total_layout(
            [(list(row) + [""] * 10)[:10] for row in rows]
        )
        if total_index >= len(rows):
            raise Rejected("rcc_sheet_total_missing")
        total = _number((list(rows[total_index]) + [""] * 10)[amount_col])
        return client, rows, blocks, total_index, amount_col, total

    def post_commit(self, m, plan):
        delivery = plan.get("_delivery")
        if not delivery:
            return {"status": "baseline_adopted", "idempotent": True}
        try:
            client, rows, blocks, total_index, amount_col, total = self._read_delivery_sheet(delivery)
        except SheetSyncError as exc:
            raise Rejected("pov_accounting_sheet_read_failed:" + str(exc)) from None

        current_matches = [item for item in blocks if item["title"] == delivery["current_title"]]
        current = next((item for item in current_matches if item["row"] == delivery["current_row"]), None)
        if not current:
            raise Rejected("pov_accounting_delivery_current_block_missing")

        if delivery["mode"] == "update_counter":
            if current["counter"] == delivery["after_counter"]:
                return {"status": "already_applied", "idempotent": True, "counter": current["counter"]}
            if current["counter"] != delivery["before_counter"]:
                raise Rejected("pov_accounting_delivery_counter_changed")
            request = {
                "updateCells": {
                    "range": {
                        "sheetId": delivery["sheet_id"],
                        "startRowIndex": delivery["current_row"] - 1,
                        "endRowIndex": delivery["current_row"],
                        "startColumnIndex": 5, "endColumnIndex": 6,
                    },
                    "rows": [{"values": [_cell_value(delivery["after_counter"])]}],
                    "fields": "userEnteredValue",
                }
            }
            try:
                client.structural(delivery["spreadsheet_id"], [request])
            except SheetSyncError as exc:
                raise Rejected("pov_accounting_sheet_delivery_failed:" + str(exc)) from None
            _client, _rows, blocks2, _ti, _ac, _total = self._read_delivery_sheet(delivery)
            verified = next((item for item in blocks2 if item["row"] == delivery["current_row"]), None)
            if not verified or verified["counter"] != delivery["after_counter"]:
                raise Rejected("pov_accounting_sheet_postwrite_verification_failed")
            return {"status": "applied", "idempotent": False, "counter": verified["counter"]}

        if delivery["mode"] != "close_and_open":
            raise Rejected("unknown_pov_accounting_delivery_mode")
        last = blocks[-1] if blocks else None
        if (
            current["counter"] == delivery["final_counter"]
            and last and last["title"] == delivery["next_title"] and last["counter"] == 0
            and total is not None and float(total) == delivery["after_total"]
        ):
            return {"status": "already_applied", "idempotent": True, "next_block": last["title"]}
        if current["counter"] != delivery["before_counter"]:
            raise Rejected("pov_accounting_delivery_counter_changed")
        if total_index != delivery["total_index"] or total is None or float(total) != delivery["before_total"]:
            raise Rejected("pov_accounting_delivery_total_changed")
        if last and last["row"] != current["row"]:
            raise Rejected("pov_accounting_delivery_newer_block_exists")

        insert = total_index
        source_start = delivery["current_row"] - 1
        new_values = [
            [delivery["next_title"], "", "", False, "", 0],
            ["", delivery["rows"][0]["label"], delivery["rows"][0]["amount_rub"], False, "", ""],
            ["", delivery["rows"][1]["label"], delivery["rows"][1]["amount_rub"], False, "", ""],
        ]
        requests = [
            {
                "updateCells": {
                    "range": {
                        "sheetId": delivery["sheet_id"],
                        "startRowIndex": source_start, "endRowIndex": source_start + 1,
                        "startColumnIndex": 5, "endColumnIndex": 6,
                    },
                    "rows": [{"values": [_cell_value(delivery["final_counter"])]}],
                    "fields": "userEnteredValue",
                }
            },
            {
                "insertDimension": {
                    "range": {
                        "sheetId": delivery["sheet_id"], "dimension": "ROWS",
                        "startIndex": insert, "endIndex": insert + 3,
                    },
                    "inheritFromBefore": True,
                }
            },
            {
                "copyPaste": {
                    "source": {
                        "sheetId": delivery["sheet_id"],
                        "startRowIndex": source_start, "endRowIndex": source_start + 3,
                        "startColumnIndex": 0, "endColumnIndex": 6,
                    },
                    "destination": {
                        "sheetId": delivery["sheet_id"],
                        "startRowIndex": insert, "endRowIndex": insert + 3,
                        "startColumnIndex": 0, "endColumnIndex": 6,
                    },
                    "pasteType": "PASTE_NORMAL",
                    "pasteOrientation": "NORMAL",
                }
            },
            {
                "updateCells": {
                    "range": {
                        "sheetId": delivery["sheet_id"],
                        "startRowIndex": insert, "endRowIndex": insert + 3,
                        "startColumnIndex": 0, "endColumnIndex": 6,
                    },
                    "rows": _rows_payload(new_values),
                    "fields": "userEnteredValue",
                }
            },
            {
                "updateCells": {
                    "range": {
                        "sheetId": delivery["sheet_id"],
                        "startRowIndex": insert + 3, "endRowIndex": insert + 4,
                        "startColumnIndex": amount_col, "endColumnIndex": amount_col + 1,
                    },
                    "rows": [{"values": [_cell_value(delivery["after_total"])]}],
                    "fields": "userEnteredValue",
                }
            },
        ]
        try:
            client.structural(delivery["spreadsheet_id"], requests)
        except SheetSyncError as exc:
            raise Rejected("pov_accounting_sheet_delivery_failed:" + str(exc)) from None

        _client, _rows, blocks2, _ti, _ac, total2 = self._read_delivery_sheet(delivery)
        current2 = next((item for item in blocks2 if item["row"] == delivery["current_row"]), None)
        last2 = blocks2[-1] if blocks2 else None
        if (
            not current2 or current2["counter"] != delivery["final_counter"]
            or not last2 or last2["title"] != delivery["next_title"] or last2["counter"] != 0
            or total2 is None or float(total2) != delivery["after_total"]
        ):
            raise Rejected("pov_accounting_sheet_postwrite_verification_failed")
        return {
            "status": "applied", "idempotent": False,
            "closed_counter": current2["counter"], "next_block": last2["title"],
            "total_rub": str(total2),
        }


def read_status(database, instance, credentials_path, postproduction_workspace_id, rcc_workspace_id):
    business = PovAccountingBusiness(instance, credentials_path)
    with database.transaction(read_only=True) as tx:
        post = tx.one(
            "SELECT id,workspace_key,status,settings,revision FROM tm_config.workspaces "
            "WHERE instance_id=%s AND id=%s::uuid",
            (instance, postproduction_workspace_id),
        )
        if not post or post.get("status") != "active":
            raise Rejected("postproduction_workspace_not_found")
        if ((post.get("settings") or {}).get("analysis") or {}).get("profile") != "postproduction":
            raise Rejected("postproduction_workspace_required")
        fake = {
            "target_id": str(post["id"]), "workspace_id": str(post["id"]),
            "expected_revision": post["revision"],
            "changes": {"rcc_workspace_id": rcc_workspace_id},
        }
        _post, rcc, integration = business._workspaces(tx, fake)
        rule = business._rule(tx, str(rcc["id"]))
        assets = business._assets(tx, str(post["id"]))
        ident, state_row = business._state(tx, str(post["id"]), False)
    live = business._live_sheet(integration)
    state = None if state_row is None else state_row["data"]
    return {
        "contract_version": CONTRACT,
        "read_only": True,
        "state_entity_id": ident,
        "activated": state is not None,
        "state_revision": None if state_row is None else state_row["revision"],
        "canonical_pov_asset_count": len(assets),
        "current_sheet_block": live["current"],
        "capacity": rule["capacity"],
        "rule_id": rule["id"],
        "rule_revision": rule["revision"],
        "state": state,
        "guards": {
            "duration_inference_forbidden": True,
            "edit_approval_affects_count": False,
            "payment_state_affects_count": False,
            "accepted_history_is_authoritative": True,
            "first_activation_adopts_sheet_counter": True,
        },
    }
