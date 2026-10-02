"""Legacy read-only Postproduction Sheet import planning (TM-019).

This adapter exists only to preserve/diagnose historical rows imported before
chat-first canonical state. New work must originate from chat evidence and
canonical server workstreams; this module must not be used as a live business
source.
"""
from __future__ import annotations

from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field

from .postproduction_sheet import (
    TARGET_SPREADSHEET_ID,
    PostproductionSheetError,
    PostproductionSheetsReadOnly,
    _cloud_present,
    _digest,
    _server_context,
    parse_snapshot,
    unresolved_inventory,
)

BOOTSTRAP_CONTRACT = "tm-postproduction-canonical-bootstrap/v1"
IGNORED_ROWS = (
    {
        "sheet_id": 0,
        "row_number": 35,
        "event": "2024.02_RCC_BOXING",
        "content_type": "Intro",
        "reason": "explicit_user_ignore",
    },
)

ASSET_FIELD_REQUIREMENTS = {
    "asset_id": {"type": "text", "max_length": 200},
    "asset_class": {"type": "text", "enum": ["pov", "non_pov"]},
    "event_key": {"type": "text", "max_length": 500},
    "content_type": {"type": "text", "max_length": 1000},
    "layout_number": {"type": "integer"},
    "sheet_spreadsheet_id": {"type": "text", "max_length": 200},
    "sheet_id": {"type": "integer"},
    "sheet_row_number": {"type": "integer"},
    "sheet_row_fingerprint": {"type": "text", "max_length": 128},
    "sheet_presentation_fingerprint": {"type": "text", "max_length": 128},
    "frame_share_id": {"type": "text", "max_length": 200},
    "cloud_urls": {"type": "text_list"},
    "edit_source_status": {"type": "text", "max_length": 100},
    "color_source_status": {"type": "text", "max_length": 100},
    "approval_state": {"type": "text", "enum": ["pending", "partial", "approved", "independent"]},
    "approval_basis": {
        "type": "text",
        "enum": [
            "required_external_approval",
            "reaction",
            "dual_reaction",
            "user_override",
            "user_confirmation",
        ],
    },
    "approval_actor": {"type": "text", "max_length": 200},
    "approval_reason": {"type": "text", "max_length": 1000},
    "approval_evidence": {"type": "text_list"},
    "requires_user_confirmation": {"type": "boolean"},
    "publication_managed": {"type": "boolean"},
}


class PostproductionBootstrapOverride(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    sheet_id: int
    row_number: int = Field(ge=1)
    decision: Literal["proceed_without_additional_approval"]
    evidence_text: str = Field(min_length=3, max_length=2000)


def stable_asset_id(row):
    key = (
        "tm-postproduction-asset:"
        + TARGET_SPREADSHEET_ID
        + ":"
        + str(row["sheet_id"])
        + ":"
        + str(row["row_number"])
    )
    return str(uuid5(NAMESPACE_URL, key))


def _row_key(row):
    return int(row["sheet_id"]), int(row["row_number"])


def _ignored(row):
    for rule in IGNORED_ROWS:
        if (
            row.get("sheet_id") == rule["sheet_id"]
            and row.get("row_number") == rule["row_number"]
            and row.get("event") == rule["event"]
            and row.get("content_type") == rule["content_type"]
        ):
            return rule
    return None


def _cloud_urls(row):
    return [x["url"] for x in row.get("cloud") or [] if x.get("url")]


def _edit_state(source_status, override):
    if override or source_status == "Готово":
        return "approved"
    if source_status in ("В работе", "Делает Майя"):
        return "in_progress"
    return "todo"


def _color_state(source_status):
    if source_status == "Готово":
        return "waiting_feedback"
    if source_status in ("В работе", "Делает Майя"):
        return "in_progress"
    return "todo"


def _approval(row, override):
    required = ["approver_a"] if row.get("is_pov") else ["approver_a", "approver_b"]
    if override:
        return {
            "state": "approved",
            "basis": "user_override",
            "actor": "user",
            "reason": "no_further_approval_required",
            "evidence": [override.evidence_text],
            "required_roles": required,
            "reaction_gate_sufficient": True,
        }
    if row.get("edit_status") == "Готово":
        return {
            "state": "approved",
            "basis": "required_external_approval",
            "actor": "",
            "reason": "sheet_observed_ready_identity_unresolved",
            "evidence": [],
            "required_roles": required,
            "reaction_gate_sufficient": True,
        }
    return {
        "state": "pending",
        "basis": "required_external_approval",
        "actor": "",
        "reason": "approval_evidence_not_yet_bound",
        "evidence": [],
        "required_roles": required,
        "reaction_gate_sufficient": True,
    }


def _workstreams(row, override):
    approval = _approval(row, override)
    edit = {
        "kind": "edit",
        "state": _edit_state(row.get("edit_status"), override),
        "source_status": row.get("edit_status"),
        "approval": approval,
        "concrete_user_action": None,
        "auto_reminder": False,
    }
    if approval["state"] == "approved" and not _cloud_present(row):
        edit["concrete_user_action"] = "upload_to_cloud"
    result = [edit]
    if not row.get("is_pov"):
        color_status = row.get("color_status")
        result.append(
            {
                "kind": "color",
                "state": _color_state(color_status),
                "source_status": color_status,
                "approval": {
                    "state": "independent",
                    "basis": "user_confirmation",
                    "actor": "user",
                    "reason": "color_is_user_owned",
                    "evidence": [],
                    "required_roles": [],
                    "reaction_gate_sufficient": False,
                },
                "requires_user_confirmation": color_status == "Готово",
                "concrete_user_action": "confirm_color" if color_status == "Готово" else None,
                "auto_reminder": False,
            }
        )
    return result


def build_bootstrap_proposal(rows, *, existing_assets=None, overrides=None):
    overrides = [
        item
        if isinstance(item, PostproductionBootstrapOverride)
        else PostproductionBootstrapOverride.model_validate(item)
        for item in (overrides or [])
    ]
    override_map = {}
    for override in overrides:
        key = (override.sheet_id, override.row_number)
        if key in override_map:
            raise PostproductionSheetError("duplicate_postproduction_bootstrap_override")
        override_map[key] = override

    existing_bindings = {
        (
            (asset.get("sheet_binding") or {}).get("sheet_id"),
            (asset.get("sheet_binding") or {}).get("row_number"),
        )
        for asset in (existing_assets or [])
        if isinstance(asset.get("sheet_binding"), dict)
    }

    inventory = unresolved_inventory(rows)
    active = []
    ignored = []
    for row in inventory["items"]:
        rule = _ignored(row)
        if rule:
            ignored.append(
                {
                    "row_identity": row["row_identity"],
                    "event": row["event"],
                    "content_type": row["content_type"],
                    "reason": rule["reason"],
                }
            )
            continue
        if _row_key(row) in existing_bindings:
            continue
        active.append(row)

    active_keys = {_row_key(row) for row in active}
    if set(override_map) - active_keys:
        raise PostproductionSheetError("postproduction_bootstrap_override_target_not_active")

    candidates = []
    for row in active:
        override = override_map.get(_row_key(row))
        candidates.append(
            {
                "asset_id": stable_asset_id(row),
                "identity_basis": "exact_sheet_row_bootstrap",
                "asset_class": "pov" if row.get("is_pov") else "non_pov",
                "event_key": row.get("event"),
                "title": row.get("content_type"),
                "layout_number": row.get("layout_number"),
                "sheet_binding": {
                    **row["row_identity"],
                    "presentation_fingerprint": row["presentation_fingerprint"],
                },
                "frame_share_id": row.get("frame_share_id"),
                "cloud_urls": _cloud_urls(row),
                "cloud_observed": _cloud_present(row),
                "workstreams": _workstreams(row, override),
                "parent_binding_required": True,
                "publication_managed": False,
                "canonical_entity": {
                    "entity_type": "postproduction-asset",
                    "data": {
                        "title": row.get("content_type"),
                        "state": "active",
                        "asset_id": stable_asset_id(row),
                        "asset_class": "pov" if row.get("is_pov") else "non_pov",
                        "event_key": row.get("event"),
                        "content_type": row.get("content_type"),
                        "layout_number": row.get("layout_number"),
                        "sheet_spreadsheet_id": row["row_identity"]["spreadsheet_id"],
                        "sheet_id": row["sheet_id"],
                        "sheet_row_number": row["row_number"],
                        "sheet_row_fingerprint": row["row_identity"]["row_fingerprint"],
                        "sheet_presentation_fingerprint": row["presentation_fingerprint"],
                        "frame_share_id": row.get("frame_share_id") or "",
                        "cloud_urls": _cloud_urls(row),
                        "edit_source_status": row.get("edit_status") or "",
                        "color_source_status": row.get("color_status") or "",
                        "approval_state": _approval(row, override)["state"],
                        "approval_basis": _approval(row, override)["basis"],
                        "approval_actor": _approval(row, override)["actor"],
                        "approval_reason": _approval(row, override)["reason"],
                        "approval_evidence": _approval(row, override)["evidence"],
                        "requires_user_confirmation": any(
                            item.get("requires_user_confirmation") is True
                            for item in _workstreams(row, override)
                        ),
                        "publication_managed": False,
                    },
                },
            }
        )

    source_fingerprint = _digest(
        [
            {
                "row": row["row_identity"],
                "presentation": row["presentation_fingerprint"],
            }
            for row in active
        ]
    )
    proposal_token = _digest(
        {
            "contract": BOOTSTRAP_CONTRACT,
            "source_fingerprint": source_fingerprint,
            "candidates": candidates,
            "ignored_rows": ignored,
        }
    )
    return {
        "contract_version": BOOTSTRAP_CONTRACT,
        "read_only": True,
        "apply_available": False,
        "source_fingerprint": source_fingerprint,
        "proposal_token": proposal_token,
        "candidate_asset_count": len(candidates),
        "candidates": candidates,
        "ignored_rows": ignored,
        "legacy_exceptions": inventory["legacy_exceptions"],
        "entity_type_extension": {
            "entity_type": "postproduction-asset",
            "required_before_apply": True,
            "fields": ASSET_FIELD_REQUIREMENTS,
        },
        "guards": {
            "historical_2024_intro_ignored": True,
            "title_only_identity_forbidden": True,
            "sheet_write_enabled": False,
            "reminder_auto_creation_enabled": False,
            "user_override_is_not_approver_a_approval": True,
            "verified_approver_a_black_check_is_sufficient_for_pov": True,
            "verified_approver_a_and_approver_b_required_for_non_pov_reaction_gate": True,
            "weak_context_never_bypasses_approval": True,
        },
    }


def read_only_bootstrap_proposal(
    database,
    instance,
    credentials_path,
    workspace_id,
    *,
    overrides=None,
):
    server = _server_context(database, instance, workspace_id)
    payload = PostproductionSheetsReadOnly(credentials_path).snapshot()
    parsed = parse_snapshot(payload)
    result = build_bootstrap_proposal(
        parsed["assets"],
        existing_assets=server["canonical_assets"],
        overrides=overrides,
    )
    return {
        **result,
        "workspace": server["workspace"],
        "spreadsheet": {
            "spreadsheet_id": parsed["spreadsheet_id"],
            "snapshot_fingerprint": parsed["snapshot_fingerprint"],
        },
        "existing_canonical_asset_count": len(server["canonical_assets"]),
        "reminder_count": len(server["reminders"]),
    }
