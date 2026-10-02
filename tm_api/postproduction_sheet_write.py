"""Guarded existing-row Postproduction Google Sheet projection (TM-020).

The universal mutation layer owns preview/apply/receipt semantics. This module
only plans exact minimal existing-cell updates and performs the external Google
Sheets delivery after the DB transaction commits.
"""
from __future__ import annotations

from uuid import UUID

from .postproduction_sheet import (
    TARGET_SPREADSHEET_ID,
    PostproductionSheetError,
    PostproductionSheetsReadOnly,
    parse_snapshot,
)
from .postproduction_projection import WORKSTREAMS, sheet_status
from .postproduction_show_sheet import build_insert_requests, plan_show_rows
from .v24.common import Rejected, check_revision, strict_fields
from .v75.sheets import GoogleSheets, SheetSyncError


OPS = {"update_postproduction_sheet_projection"}
def _quote_sheet(value):
    return "'" + str(value).replace("'", "''") + "'"


def _row_key(row):
    return int(row["sheet_id"]), int(row["row_number"])


def _asset_row_key(data):
    return int(data["sheet_id"]), int(data["sheet_row_number"])


def _validate_asset_id(value):
    if not isinstance(value, str):
        raise Rejected("invalid_postproduction_asset_id")
    try:
        UUID(value)
    except (ValueError, AttributeError):
        raise Rejected("invalid_postproduction_asset_id") from None
    return value


def _validate_job_id(value):
    if not isinstance(value, str):
        raise Rejected("invalid_postproduction_job_id")
    try:
        UUID(value)
    except (ValueError, AttributeError):
        raise Rejected("invalid_postproduction_job_id") from None
    return value


def _anchor_matches(row, data):
    if row.get("event") != data.get("event_key"):
        return False
    if row.get("content_type") != data.get("content_type"):
        return False
    canonical_frame = data.get("frame_share_id") or ""
    if canonical_frame and row.get("frame_share_id") != canonical_frame:
        return False
    return True


class PostproductionSheetBusiness:
    """Planner + idempotent post-commit Sheet delivery."""

    def __init__(self, instance, credentials_path):
        self.instance = instance
        self.credentials_path = credentials_path

    def _snapshot_rows(self):
        payload = PostproductionSheetsReadOnly(self.credentials_path).snapshot()
        return parse_snapshot(payload)

    def _workspace_and_assets(self, tx, m):
        workspace = tx.one(
            "SELECT id,workspace_key,status,settings,revision "
            "FROM tm_config.workspaces WHERE instance_id=%s AND id=%s::uuid",
            (self.instance, m["target_id"]),
        )
        if not workspace or workspace.get("status") != "active":
            raise Rejected("postproduction_workspace_not_found")
        if str(workspace["id"]) != str(m.get("workspace_id")):
            raise Rejected("postproduction_workspace_target_mismatch")
        profile = ((workspace.get("settings") or {}).get("analysis") or {}).get("profile")
        if profile != "postproduction":
            raise Rejected("postproduction_workspace_required")
        check_revision(workspace["revision"], m["expected_revision"])

        strict_fields(m["changes"], ("asset_ids",), ("asset_ids",))
        asset_ids = m["changes"]["asset_ids"]
        if (
            not isinstance(asset_ids, list)
            or not 1 <= len(asset_ids) <= 50
            or len(set(asset_ids)) != len(asset_ids)
        ):
            raise Rejected("invalid_postproduction_asset_set")
        asset_ids = [_validate_asset_id(value) for value in asset_ids]

        rows = tx.all(
            "SELECT id,data,revision,status FROM tm_config.entities "
            "WHERE instance_id=%s AND workspace_id=%s::uuid "
            "AND entity_type='postproduction-asset' AND status='active' "
            "ORDER BY id LIMIT 501",
            (self.instance, m["workspace_id"]),
        )
        if len(rows) > 500:
            raise Rejected("postproduction_asset_limit")
        by_asset = {}
        for row in rows:
            data = row.get("data") or {}
            asset_id = data.get("asset_id")
            if isinstance(asset_id, str):
                if asset_id in by_asset:
                    raise Rejected("duplicate_canonical_postproduction_asset_id")
                by_asset[asset_id] = {
                    "entity_id": str(row["id"]),
                    "revision": row["revision"],
                    "data": data,
                }
        missing = [asset_id for asset_id in asset_ids if asset_id not in by_asset]
        if missing:
            raise Rejected("canonical_postproduction_asset_not_found")

        item_rows = tx.all(
            "SELECT id,data,revision,status FROM tm_config.entities "
            "WHERE instance_id=%s AND workspace_id=%s::uuid "
            "AND entity_type='postproduction-item' AND status='active' "
            "ORDER BY id LIMIT 801",
            (self.instance, m["workspace_id"]),
        )
        if len(item_rows) > 800:
            raise Rejected("postproduction_item_limit")
        items = {}
        for row in item_rows:
            data = row.get("data") or {}
            asset_id = data.get("asset_id")
            kind = data.get("kind")
            if not isinstance(asset_id, str) or kind not in WORKSTREAMS:
                continue
            key = (asset_id, kind)
            if key in items:
                raise Rejected("duplicate_canonical_postproduction_workstream")
            items[key] = {
                "entity_id": str(row["id"]),
                "revision": row["revision"],
                "data": data,
            }
        return workspace, [by_asset[asset_id] for asset_id in asset_ids], items

    def _workspace_and_jobs(self, tx, m):
        workspace = tx.one(
            "SELECT id,workspace_key,status,settings,revision "
            "FROM tm_config.workspaces WHERE instance_id=%s AND id=%s::uuid",
            (self.instance, m["target_id"]),
        )
        if not workspace or workspace.get("status") != "active":
            raise Rejected("postproduction_workspace_not_found")
        if str(workspace["id"]) != str(m.get("workspace_id")):
            raise Rejected("postproduction_workspace_target_mismatch")
        profile = ((workspace.get("settings") or {}).get("analysis") or {}).get("profile")
        if profile != "postproduction":
            raise Rejected("postproduction_workspace_required")
        check_revision(workspace["revision"], m["expected_revision"])

        strict_fields(m["changes"], ("job_ids",), ("job_ids",))
        job_ids = m["changes"]["job_ids"]
        if (
            not isinstance(job_ids, list)
            or not 1 <= len(job_ids) <= 10
            or len(set(job_ids)) != len(job_ids)
        ):
            raise Rejected("invalid_postproduction_job_set")
        job_ids = [_validate_job_id(value) for value in job_ids]

        rows = tx.all(
            "SELECT id,entity_type,data,revision,status FROM tm_config.entities "
            "WHERE instance_id=%s AND workspace_id=%s::uuid AND status='active' "
            "AND entity_type IN ('postproduction-job','postproduction-item') "
            "ORDER BY id LIMIT 1001",
            (self.instance, m["workspace_id"]),
        )
        if len(rows) > 1000:
            raise Rejected("postproduction_entity_limit")
        by_job = {}
        items_by_parent = {}
        for row in rows:
            data = row.get("data") or {}
            if row.get("entity_type") == "postproduction-job":
                by_job[str(row["id"])] = row
                continue
            parent_id = data.get("parent_id")
            if isinstance(parent_id, str):
                items_by_parent.setdefault(parent_id, []).append(row)
        missing = [job_id for job_id in job_ids if job_id not in by_job]
        if missing:
            raise Rejected("canonical_postproduction_job_not_found")
        rule = tx.one(
            "SELECT id,body,revision FROM tm_config.documents "
            "WHERE instance_id=%s AND workspace_id=%s::uuid AND kind='rule' "
            "AND status='active' AND body->>'decision_key'='postproduction.tracking' "
            "ORDER BY revision DESC,id DESC LIMIT 1",
            (self.instance, m["workspace_id"]),
        )
        if not rule:
            raise Rejected("postproduction_tracking_rule_required")
        return (
            workspace, [by_job[job_id] for job_id in job_ids],
            items_by_parent, rule,
        )

    def _plan_show_jobs(self, tx, m):
        workspace, jobs, items_by_parent, rule = self._workspace_and_jobs(tx, m)
        parsed = self._snapshot_rows()
        if parsed["spreadsheet_id"] != TARGET_SPREADSHEET_ID:
            raise Rejected("unexpected_postproduction_spreadsheet")
        plan = plan_show_rows(
            parsed, jobs, items_by_parent, (rule.get("body") or {}).get("value") or {}
        )
        operations = plan["operations"]
        if not operations:
            raise Rejected("postproduction_sheet_projection_no_changes")
        before = {
            "spreadsheet_id": TARGET_SPREADSHEET_ID,
            "workspace_revision": workspace["revision"],
            "snapshot_fingerprint": parsed["snapshot_fingerprint"],
            "show_last_used_row": plan["expected_last_used_row"],
            "selected_jobs": plan["selected_jobs"],
            "authority": "canonical_server_state",
            "sheet_role": "client_dashboard_projection",
            "tracking_rule": {"id": str(rule["id"]), "revision": rule["revision"]},
        }
        after = {
            **before,
            "projection_operations": operations,
            "status": "approved_for_delivery",
        }
        return {
            "entity": "postproduction_sheet_projection",
            "operation": m["operation"],
            "target_id": str(workspace["id"]),
            "before": before,
            "after": after,
            "_delivery": {
                "spreadsheet_id": TARGET_SPREADSHEET_ID,
                "kind": "show_row_insert",
                "sheet_id": plan["sheet_id"],
                "expected_snapshot_fingerprint": parsed["snapshot_fingerprint"],
                "expected_last_used_row": plan["expected_last_used_row"],
                "operations": operations,
            },
        }

    def plan(self, tx, m, lock=False):
        if m["operation"] not in OPS:
            raise Rejected("unsupported_postproduction_sheet_operation")
        changes = m.get("changes") or {}
        if "job_ids" in changes:
            if "asset_ids" in changes:
                raise Rejected("postproduction_sheet_choose_asset_or_job_projection")
            return self._plan_show_jobs(tx, m)
        workspace, assets, items = self._workspace_and_assets(tx, m)
        parsed = self._snapshot_rows()
        if parsed["spreadsheet_id"] != TARGET_SPREADSHEET_ID:
            raise Rejected("unexpected_postproduction_spreadsheet")
        rows = {_row_key(row): row for row in parsed["assets"]}

        operations = []
        selected = []
        for asset in assets:
            data = asset["data"]
            required = (
                "asset_id",
                "sheet_spreadsheet_id",
                "sheet_id",
                "sheet_row_number",
                "event_key",
                "content_type",
            )
            if any(data.get(key) in (None, "") for key in required):
                raise Rejected("postproduction_asset_sheet_binding_incomplete")
            if data["sheet_spreadsheet_id"] != TARGET_SPREADSHEET_ID:
                raise Rejected("postproduction_asset_wrong_spreadsheet")
            row = rows.get(_asset_row_key(data))
            if not row:
                raise Rejected("postproduction_sheet_bound_row_missing")
            if not _anchor_matches(row, data):
                raise Rejected("postproduction_sheet_bound_row_identity_changed")
            if row.get("unsupported_statuses"):
                raise Rejected("postproduction_sheet_unsupported_status")
            if row.get("status_validation_matches_contract") is False:
                raise Rejected("postproduction_sheet_status_validation_changed")

            edit_item = items.get((data["asset_id"], "edit"))
            color_item = items.get((data["asset_id"], "color"))
            desired_edit = sheet_status(data, edit_item, "edit")
            desired_color = sheet_status(data, color_item, "color")
            selected.append(
                {
                    "asset_id": data["asset_id"],
                    "entity_id": asset["entity_id"],
                    "entity_revision": asset["revision"],
                    "row_identity": row["row_identity"],
                    "current_edit_status": row.get("edit_status"),
                    "desired_edit_status": desired_edit,
                    "current_color_status": row.get("color_status"),
                    "desired_color_status": desired_color,
                    "edit_item_id": edit_item["entity_id"] if edit_item else None,
                    "color_item_id": color_item["entity_id"] if color_item else None,
                    "authority": "canonical_server_state",
                }
            )
            projections = [
                ("edit_status", desired_edit, "E" if row.get("is_pov") else "G"),
            ]
            if not row.get("is_pov"):
                projections.append(("color_status", desired_color, "F"))
            for field, desired, column in projections:
                if desired is None or desired == row.get(field):
                    continue
                operations.append(
                    {
                        "asset_id": data["asset_id"],
                        "field": field,
                        "range": f"{_quote_sheet(row['tab'])}!{column}{row['row_number']}",
                        "sheet_id": row["sheet_id"],
                        "row_number": row["row_number"],
                        "event_key": row["event"],
                        "content_type": row["content_type"],
                        "frame_share_id": row.get("frame_share_id") or "",
                        "before": row.get(field),
                        "after": desired,
                        "expected_row_fingerprint": row["row_identity"]["row_fingerprint"],
                        "expected_presentation_fingerprint": row["presentation_fingerprint"],
                    }
                )

        if not operations:
            raise Rejected("postproduction_sheet_projection_no_changes")

        before = {
            "spreadsheet_id": TARGET_SPREADSHEET_ID,
            "workspace_revision": workspace["revision"],
            "snapshot_fingerprint": parsed["snapshot_fingerprint"],
            "authority": "canonical_server_state",
            "sheet_role": "client_dashboard_projection",
            "selected_assets": selected,
        }
        after = {
            **before,
            "projection_operations": operations,
            "status": "approved_for_delivery",
        }
        return {
            "entity": "postproduction_sheet_projection",
            "operation": m["operation"],
            "target_id": str(workspace["id"]),
            "before": before,
            "after": after,
            "_delivery": {
                "spreadsheet_id": TARGET_SPREADSHEET_ID,
                "operations": operations,
            },
        }

    def execute(self, tx, m, plan):
        return {
            "entity": "postproduction_sheet_projection",
            "id": plan["target_id"],
            "status": "approved_for_delivery",
            "operation_count": len((plan.get("_delivery") or {}).get("operations") or []),
        }

    @staticmethod
    def _find_rows(parsed):
        return {_row_key(row): row for row in parsed["assets"]}

    def _live_state(self):
        return self._snapshot_rows()

    @staticmethod
    def _insert_rows_match(parsed, operations):
        by_row = {
            (row["sheet_id"], row["row_number"]): row
            for row in parsed.get("assets") or []
        }
        states = []
        for operation in operations:
            row = by_row.get((operation["sheet_id"], operation["row_number"]))
            matched = bool(
                row
                and row.get("event") == operation["event_key"]
                and row.get("content_type") == operation["content_type"]
                and row.get("edit_status") == operation["edit_status"]
                and row.get("color_status") == operation["color_status"]
            )
            states.append((operation, row, matched))
        return states

    def _post_commit_show_insert(self, delivery):
        operations = delivery.get("operations") or []
        if not operations:
            raise Rejected("postproduction_sheet_delivery_missing")
        parsed = self._live_state()
        states = self._insert_rows_match(parsed, operations)
        if all(matched for _operation, _row, matched in states):
            return {
                "status": "already_applied",
                "idempotent": True,
                "operation_count": len(operations),
                "snapshot_fingerprint": parsed["snapshot_fingerprint"],
                "rows": [operation["row_number"] for operation in operations],
            }
        if any(row is not None for _operation, row, _matched in states):
            raise Rejected("postproduction_sheet_partial_external_state")
        show = (parsed.get("sheets") or {}).get("Шоу") or {}
        if (
            parsed.get("snapshot_fingerprint") != delivery.get("expected_snapshot_fingerprint")
            or show.get("last_used_row") != delivery.get("expected_last_used_row")
        ):
            raise Rejected("postproduction_sheet_insert_cas_changed")
        by_row = {
            (row["sheet_id"], row["row_number"]): row
            for row in parsed.get("assets") or []
        }
        for operation in operations:
            template = by_row.get((operation["sheet_id"], operation["template_row_number"]))
            if (
                not template
                or template.get("presentation_fingerprint")
                != operation["expected_template_presentation_fingerprint"]
            ):
                raise Rejected("postproduction_sheet_template_changed")
        writer = GoogleSheets(self.credentials_path)
        try:
            writer.structural(TARGET_SPREADSHEET_ID, build_insert_requests(delivery))
        except SheetSyncError as exc:
            raise Rejected("postproduction_sheet_delivery_failed:" + str(exc)) from None
        verified = self._live_state()
        verified_states = self._insert_rows_match(verified, operations)
        if not all(matched for _operation, _row, matched in verified_states):
            raise Rejected("postproduction_sheet_postwrite_verification_failed")
        return {
            "status": "applied",
            "idempotent": False,
            "operation_count": len(operations),
            "snapshot_fingerprint": verified["snapshot_fingerprint"],
            "rows": [operation["row_number"] for operation in operations],
            "content_types": [operation["content_type"] for operation in operations],
        }

    def post_commit(self, m, plan):
        delivery = plan.get("_delivery") or {}
        if delivery.get("kind") == "show_row_insert":
            return self._post_commit_show_insert(delivery)
        operations = delivery.get("operations") or []
        if not operations:
            raise Rejected("postproduction_sheet_delivery_missing")

        parsed = self._live_state()
        rows = self._find_rows(parsed)
        states = []
        for operation in operations:
            row = rows.get((operation["sheet_id"], operation["row_number"]))
            if not row:
                raise Rejected("postproduction_sheet_cas_row_missing")
            if (
                row.get("event") != operation["event_key"]
                or row.get("content_type") != operation["content_type"]
                or (row.get("frame_share_id") or "") != operation["frame_share_id"]
            ):
                raise Rejected("postproduction_sheet_cas_identity_mismatch")
            current = row.get(operation["field"])
            states.append((operation, row, current))

        if all(current == operation["after"] for operation, _row, current in states):
            return {
                "status": "already_applied",
                "idempotent": True,
                "operation_count": len(operations),
                "snapshot_fingerprint": parsed["snapshot_fingerprint"],
            }

        if any(
            current not in (operation["before"], operation["after"])
            for operation, _row, current in states
        ):
            raise Rejected("postproduction_sheet_cas_value_changed")
        if any(current == operation["after"] for operation, _row, current in states):
            raise Rejected("postproduction_sheet_partial_external_state")

        for operation, row, current in states:
            if row["row_identity"]["row_fingerprint"] != operation["expected_row_fingerprint"]:
                raise Rejected("postproduction_sheet_cas_row_changed")
            if row["presentation_fingerprint"] != operation["expected_presentation_fingerprint"]:
                raise Rejected("postproduction_sheet_presentation_changed")
            if current != operation["before"]:
                raise Rejected("postproduction_sheet_cas_value_changed")

        writer = GoogleSheets(self.credentials_path)
        try:
            writer.write_values(
                TARGET_SPREADSHEET_ID,
                [
                    {"range": operation["range"], "values": [[operation["after"]]]}
                    for operation in operations
                ],
            )
        except SheetSyncError as exc:
            raise Rejected("postproduction_sheet_delivery_failed:" + str(exc)) from None

        verified = self._live_state()
        verified_rows = self._find_rows(verified)
        for operation in operations:
            row = verified_rows.get((operation["sheet_id"], operation["row_number"]))
            if not row or not _anchor_matches(
                row,
                {
                    "event_key": operation["event_key"],
                    "content_type": operation["content_type"],
                    "frame_share_id": operation["frame_share_id"],
                },
            ):
                raise Rejected("postproduction_sheet_postwrite_identity_mismatch")
            if row.get(operation["field"]) != operation["after"]:
                raise Rejected("postproduction_sheet_postwrite_verification_failed")

        return {
            "status": "applied",
            "idempotent": False,
            "operation_count": len(operations),
            "snapshot_fingerprint": verified["snapshot_fingerprint"],
            "ranges": [operation["range"] for operation in operations],
        }
