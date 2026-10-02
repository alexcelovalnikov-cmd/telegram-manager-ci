"""Deterministic contextual Postproduction approval resolution (TM-022).

Natural-language interpretation stays with the calling assistant. This module
only resolves already-structured evidence and never infers approval from a weak
phrase by itself.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TRUSTED_CANONICAL_BASES = {
    "user_override",
    "reaction",
    "dual_reaction",
    "user_confirmation",
}


class ContextualDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source: Literal["user", "telegram", "whatsapp"]
    decision: Literal[
        "proceed_without_additional_approval",
        "explicit_edit_approval",
        "observation_only",
    ]
    evidence_ref: str = Field(min_length=3, max_length=500)
    text: str = Field(min_length=1, max_length=4000)


def _required_roles(asset_class: str) -> list[str]:
    return ["approver_a"] if asset_class == "pov" else ["approver_a", "approver_b"]


def _base(asset: dict) -> dict:
    asset_class = asset.get("asset_class")
    if asset_class not in ("pov", "non_pov"):
        raise ValueError("asset_class must be pov or non_pov")
    return {
        "contract": "postproduction_contextual_approval_v1",
        "asset_id": asset.get("asset_id"),
        "asset_class": asset_class,
        "required_approvers": _required_roles(asset_class),
        "color_independent": True,
        "upload_is_separate_user_action": True,
    }


def resolve_contextual_approval(
    *,
    asset: dict,
    reaction_result: dict | None = None,
    contextual_decisions: list[ContextualDecision | dict] | None = None,
    workstream: str = "edit",
) -> dict:
    """Resolve approval precedence without mutating canonical state."""
    if workstream not in ("edit", "color"):
        raise ValueError("workstream must be edit or color")
    base = _base(asset)

    if workstream == "color":
        return {
            **base,
            "workstream": "color",
            "state": "independent",
            "fully_approved": False,
            "approval_basis": "user_confirmation",
            "approval_actor": "user",
            "approval_reason": "color_is_user_owned",
            "evidence": [],
            "requires_user_confirmation": True,
            "can_project_edit_ready": False,
        }

    # Existing canonical trusted approval always wins and preserves its basis.
    if (
        asset.get("approval_state") == "approved"
        and asset.get("approval_basis") in TRUSTED_CANONICAL_BASES
    ):
        return {
            **base,
            "workstream": "edit",
            "state": "approved",
            "fully_approved": True,
            "approval_basis": asset.get("approval_basis"),
            "approval_actor": asset.get("approval_actor") or "",
            "approval_reason": asset.get("approval_reason") or "canonical_trusted_approval",
            "evidence": list(asset.get("approval_evidence") or []),
            "requires_user_confirmation": False,
            "can_project_edit_ready": True,
            "precedence": "canonical_trusted_approval",
        }

    decisions = [
        item if isinstance(item, ContextualDecision)
        else ContextualDecision.model_validate(item)
        for item in (contextual_decisions or [])
    ]

    # Only an explicit structured user decision may bypass further external approval.
    overrides = [
        item for item in decisions
        if item.source == "user"
        and item.decision == "proceed_without_additional_approval"
    ]
    if overrides:
        latest = overrides[-1]
        return {
            **base,
            "workstream": "edit",
            "state": "approved",
            "fully_approved": True,
            "approval_basis": "user_override",
            "approval_actor": "user",
            "approval_reason": "no_further_approval_required",
            "evidence": [{
                "source": latest.source,
                "decision": latest.decision,
                "evidence_ref": latest.evidence_ref,
                "text": latest.text,
            }],
            "requires_user_confirmation": False,
            "can_project_edit_ready": True,
            "precedence": "explicit_user_override",
        }

    confirmations = [
        item for item in decisions
        if item.source == "user"
        and item.decision == "explicit_edit_approval"
    ]
    if confirmations:
        latest = confirmations[-1]
        return {
            **base,
            "workstream": "edit",
            "state": "approved",
            "fully_approved": True,
            "approval_basis": "user_confirmation",
            "approval_actor": "user",
            "approval_reason": "explicit_user_edit_approval",
            "evidence": [{
                "source": latest.source,
                "decision": latest.decision,
                "evidence_ref": latest.evidence_ref,
                "text": latest.text,
            }],
            "requires_user_confirmation": False,
            "can_project_edit_ready": True,
            "precedence": "explicit_user_confirmation",
        }

    # Reaction evidence is authoritative only when the reaction evaluator itself
    # says the required gate is fully approved.
    if reaction_result:
        state = reaction_result.get("state")
        if reaction_result.get("fully_approved") is True and state == "approved":
            basis = "reaction" if asset.get("asset_class") == "pov" else "dual_reaction"
            return {
                **base,
                "workstream": "edit",
                "state": "approved",
                "fully_approved": True,
                "approval_basis": basis,
                "approval_actor": "approver_a" if basis == "reaction" else "approver_a+approver_b",
                "approval_reason": "verified_whatsapp_reaction_gate",
                "evidence": list(reaction_result.get("evidence") or []),
                "requires_user_confirmation": False,
                "can_project_edit_ready": True,
                "precedence": "verified_reaction_gate",
            }
        if state == "partial":
            return {
                **base,
                "workstream": "edit",
                "state": "partial",
                "fully_approved": False,
                "approval_basis": "dual_reaction",
                "approval_actor": "",
                "approval_reason": "partial_verified_reaction_gate",
                "evidence": list(reaction_result.get("evidence") or []),
                "requires_user_confirmation": False,
                "can_project_edit_ready": False,
                "precedence": "partial_reaction_gate",
            }
        if state in ("identity_unverified", "identity_conflict", "evidence_truncated"):
            return {
                **base,
                "workstream": "edit",
                "state": state,
                "fully_approved": False,
                "approval_basis": "required_external_approval",
                "approval_actor": "",
                "approval_reason": state,
                "evidence": list(reaction_result.get("evidence") or []),
                "requires_user_confirmation": False,
                "can_project_edit_ready": False,
                "precedence": "reaction_fail_closed",
            }

    # Observation-only context is retained as evidence but can never authorize.
    observations = [
        {
            "source": item.source,
            "decision": item.decision,
            "evidence_ref": item.evidence_ref,
            "text": item.text,
        }
        for item in decisions
        if item.decision == "observation_only"
    ]
    return {
        **base,
        "workstream": "edit",
        "state": "pending",
        "fully_approved": False,
        "approval_basis": "required_external_approval",
        "approval_actor": "",
        "approval_reason": "approval_evidence_not_sufficient",
        "evidence": observations,
        "requires_user_confirmation": False,
        "can_project_edit_ready": False,
        "precedence": "pending_fail_closed",
    }
