"""Deterministic Postproduction approval evaluation from WhatsApp reaction evidence."""
from __future__ import annotations

BLACK_CHECK = "✔"


def _reaction_value(value: str | None) -> str:
    return (value or "").replace("\ufe0f", "").replace("\ufe0e", "").strip()


def evaluate_postproduction_approval(
    *,
    jid: str,
    message_id: str,
    asset_class: str,
    workstream: str,
    current_reactions: list[dict],
    identity_map: dict[str, dict],
    reactions_complete: bool = True,
) -> dict:
    """Evaluate edit approval without mutating Postproduction state.

    Roles are resolved only through a previously verified stable identity mapping.
    Display names are deliberately ignored for authorization.
    """
    if asset_class not in ("pov", "non_pov"):
        raise ValueError("asset_class must be pov or non_pov")
    if workstream not in ("edit", "color"):
        raise ValueError("workstream must be edit or color")

    base = {
        "contract": "whatsapp_postproduction_approval_v1",
        "chat_id": jid,
        "target_message_id": message_id,
        "asset_class": asset_class,
        "workstream": workstream,
        "approval_reaction": "✔️",
        "color_independent": True,
        "upload_is_separate_user_action": True,
    }
    if workstream == "color":
        return {
            **base,
            "state": "independent",
            "fully_approved": False,
            "partial_approval": False,
            "approval_applicable": False,
            "edit_status_ready": False,
            "can_mark_corrections_ready": False,
            "requires_user_confirmation": True,
            "required_approvers": [],
            "approvals": {},
            "evidence": [],
            "missing_identity_roles": [],
        }

    required = ["approver_a"] if asset_class == "pov" else ["approver_a", "approver_b"]
    missing = [role for role in required if not identity_map.get(role, {}).get("identity_key")]
    if missing:
        return {
            **base,
            "state": "identity_unverified",
            "fully_approved": False,
            "partial_approval": False,
            "approval_applicable": True,
            "edit_status_ready": False,
            "can_mark_corrections_ready": False,
            "requires_user_confirmation": False,
            "required_approvers": required,
            "approvals": {role: False for role in required},
            "evidence": [],
            "missing_identity_roles": missing,
        }
    identity_keys=[identity_map[role]["identity_key"] for role in required]
    if len(set(identity_keys)) != len(identity_keys):
        return {
            **base,
            "state": "identity_conflict",
            "fully_approved": False,
            "partial_approval": False,
            "approval_applicable": True,
            "edit_status_ready": False,
            "can_mark_corrections_ready": False,
            "requires_user_confirmation": False,
            "required_approvers": required,
            "approvals": {role: False for role in required},
            "evidence": [],
            "missing_identity_roles": [],
        }
    if not reactions_complete:
        return {
            **base,
            "state": "evidence_truncated",
            "fully_approved": False,
            "partial_approval": False,
            "approval_applicable": True,
            "edit_status_ready": False,
            "can_mark_corrections_ready": False,
            "requires_user_confirmation": False,
            "required_approvers": required,
            "approvals": {role: False for role in required},
            "evidence": [],
            "missing_identity_roles": [],
        }

    by_identity = {
        item.get("actor_identity_key"): item
        for item in current_reactions
        if item.get("actor_identity_key")
    }
    approvals = {}
    evidence = []
    for role in required:
        mapping = identity_map[role]
        reaction = by_identity.get(mapping["identity_key"])
        approved = bool(
            reaction
            and not reaction.get("is_removed")
            and _reaction_value(reaction.get("reaction_value")) == BLACK_CHECK
        )
        approvals[role] = approved
        if reaction:
            evidence.append({
                "role": role,
                "actor_identity_key": mapping["identity_key"],
                "actor_jid": reaction.get("actor_jid"),
                "actor_lid": reaction.get("actor_lid"),
                "actor_display_name": reaction.get("actor_display_name") or "",
                "reaction_value": reaction.get("reaction_value") or "",
                "is_removed": bool(reaction.get("is_removed")),
                "reacted_at": reaction.get("reacted_at"),
                "evidence_token": reaction.get("evidence_token"),
                "identity_verified_source": mapping.get("verified_source"),
                "identity_verified_at": mapping.get("verified_at"),
            })

    count = sum(1 for value in approvals.values() if value)
    fully = count == len(required)
    partial = 0 < count < len(required)
    state = "approved" if fully else ("partial" if partial else "pending")
    return {
        **base,
        "state": state,
        "fully_approved": fully,
        "partial_approval": partial,
        "approval_applicable": True,
        "edit_status_ready": fully,
        "can_mark_corrections_ready": fully,
        "requires_user_confirmation": False,
        "required_approvers": required,
        "approvals": approvals,
        "evidence": evidence,
        "missing_identity_roles": [],
    }
