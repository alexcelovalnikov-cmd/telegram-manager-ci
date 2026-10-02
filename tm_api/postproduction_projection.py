"""Shared canonical Postproduction projection semantics (TM-032).

Chat evidence is normalized into postproduction-item workstreams. Google Sheet
and Reminders consume those canonical workstreams independently; neither
projection authorizes the other.
"""
from __future__ import annotations

from .v24.common import Rejected

TRUSTED_EDIT_APPROVAL_BASES = {
    "user_override", "reaction", "dual_reaction", "user_confirmation",
}
WORKSTREAMS = ("edit", "color")
CANONICAL_STATES = {
    "planned", "waiting_materials", "active", "waiting_feedback",
    "completed", "cancelled",
}
PROGRESS_STATES = {
    "not_started", "in_progress", "corrections", "waiting_feedback",
    "done", "assigned_elsewhere",
}


def _approval_complete(asset):
    return (
        asset.get("approval_state") == "approved"
        and asset.get("approval_basis") in TRUSTED_EDIT_APPROVAL_BASES
    )


def _cloud_present(asset):
    return any(
        isinstance(value, str) and value.strip()
        for value in (asset.get("cloud_urls") or [])
    )


def _subject(asset, parent_title):
    if asset.get("asset_class") == "pov" and asset.get("layout_number"):
        label = f"POV №{asset['layout_number']}"
    else:
        label = asset.get("content_type") or asset.get("title") or "ролик"
    return f"{parent_title} / {label}" if parent_title else label


def validate_item(item, asset_id, workstream):
    if item is None:
        return None
    data = item.get("data") if isinstance(item, dict) and "data" in item else item
    if not isinstance(data, dict):
        raise Rejected("invalid_postproduction_workstream")
    if data.get("asset_id") != asset_id or data.get("kind") != workstream:
        raise Rejected("postproduction_workstream_identity_mismatch")
    state = data.get("state")
    if state not in CANONICAL_STATES:
        raise Rejected("invalid_postproduction_workstream_state")
    progress = data.get("progress_status")
    if progress is not None and progress not in PROGRESS_STATES:
        raise Rejected("invalid_postproduction_progress_status")
    return data


def reminder_decision(asset, item, parent_title, workstream):
    if workstream not in WORKSTREAMS:
        raise Rejected("invalid_postproduction_workstream")
    if workstream == "color" and asset.get("asset_class") == "pov":
        return {"mode": "complete", "state": "not_applicable"}

    data = validate_item(item, asset.get("asset_id"), workstream)
    if data is None:
        return {"mode": "preserve", "state": "canonical_workstream_missing"}

    # TM-029 generic tasks remain owned by their explicit reminder_task_id.
    # TM-023 must never create a second deterministic VTODO for the same action.
    if data.get("reminder_task_id") is not None:
        return {"mode": "preserve", "state": "generic_task_owned"}

    state = data["state"]
    if state == "active":
        action = data.get("next_action")
        if not isinstance(action, str) or not action.strip():
            return {"mode": "preserve", "state": "active_without_next_action"}
        description = (
            "Есть конкретное действие по монтажу."
            if workstream == "edit"
            else "Цвет ещё не готов."
        )
        return {
            "mode": "open",
            "state": "active",
            "title": action.strip(),
            "description": description,
        }

    if workstream == "edit" and state == "waiting_feedback" and _approval_complete(asset):
        if _cloud_present(asset):
            return {"mode": "complete", "state": "uploaded"}
        return {
            "mode": "open",
            "state": "upload",
            "title": f"Согласовано, можно загружать — {_subject(asset, parent_title)}",
            "description": "Монтаж согласован; нужно загрузить готовый файл.",
        }

    if state in ("planned", "waiting_materials", "waiting_feedback", "completed", "cancelled"):
        return {"mode": "complete", "state": state}
    raise Rejected("invalid_postproduction_workstream_state")


def sheet_status(asset, item, workstream):
    if workstream not in WORKSTREAMS:
        raise Rejected("invalid_postproduction_workstream")
    if workstream == "color" and asset.get("asset_class") == "pov":
        return None

    data = validate_item(item, asset.get("asset_id"), workstream)
    if data is None:
        return None

    state = data["state"]
    progress = data.get("progress_status")

    if workstream == "edit" and state == "waiting_feedback" and _approval_complete(asset):
        return "Готово"
    if state in ("planned", "waiting_materials"):
        return "Не готово"
    if state == "active":
        if progress == "assigned_elsewhere":
            return "Делает Майя"
        if progress == "not_started":
            return "Не готово"
        if progress in ("in_progress", "corrections"):
            return "В работе"
        # Active without a clear progress phase is incomplete canonical data.
        return None
    if state == "waiting_feedback":
        # Existing client-dashboard convention: a sent version waiting for
        # feedback/approval is displayed as «Нужны правки».
        return "Нужны правки"
    if state == "completed":
        return "Готово"
    if state == "cancelled":
        return None
    raise Rejected("invalid_postproduction_workstream_state")
