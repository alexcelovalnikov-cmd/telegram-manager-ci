import pytest

from tm_api.postproduction_contextual_approval import (
    ContextualDecision,
    resolve_contextual_approval,
)


def asset(*, asset_class="pov", state="pending", basis="required_external_approval"):
    return {
        "asset_id": "a6ffd76f-6ef0-4bf8-9de7-79dbc4f5ead0",
        "asset_class": asset_class,
        "approval_state": state,
        "approval_basis": basis,
        "approval_actor": "user" if basis == "user_override" else "",
        "approval_reason": "stored",
        "approval_evidence": ["stored evidence"] if state == "approved" else [],
    }


def verified_reaction(*, asset_class="pov", state="approved"):
    if state == "approved":
        evidence = [{"role": "approver_a", "evidence_token": "rx-approver_a"}]
        if asset_class == "non_pov":
            evidence.append({"role": "approver_b", "evidence_token": "rx-approver_b"})
        return {
            "state": "approved",
            "fully_approved": True,
            "partial_approval": False,
            "evidence": evidence,
        }
    if state == "partial":
        return {
            "state": "partial",
            "fully_approved": False,
            "partial_approval": True,
            "evidence": [{"role": "approver_a", "evidence_token": "rx-approver_a"}],
        }
    return {
        "state": state,
        "fully_approved": False,
        "partial_approval": False,
        "evidence": [],
    }


def test_existing_canonical_user_override_wins_and_preserves_basis():
    result = resolve_contextual_approval(
        asset=asset(state="approved", basis="user_override"),
        reaction_result=verified_reaction(state="identity_unverified"),
    )
    assert result["state"] == "approved"
    assert result["approval_basis"] == "user_override"
    assert result["precedence"] == "canonical_trusted_approval"
    assert result["can_project_edit_ready"] is True


def test_explicit_user_override_beats_verified_reaction_gate():
    result = resolve_contextual_approval(
        asset=asset(),
        reaction_result=verified_reaction(),
        contextual_decisions=[{
            "source": "user",
            "decision": "proceed_without_additional_approval",
            "evidence_ref": "chat-current-turn",
            "text": "Правка несущественная, выгружаю без дополнительного согласования.",
        }],
    )
    assert result["state"] == "approved"
    assert result["approval_basis"] == "user_override"
    assert result["precedence"] == "explicit_user_override"


def test_weak_observation_never_bypasses_approval():
    result = resolve_contextual_approval(
        asset=asset(),
        contextual_decisions=[{
            "source": "user",
            "decision": "observation_only",
            "evidence_ref": "chat-current-turn",
            "text": "Правка небольшая.",
        }],
    )
    assert result["state"] == "pending"
    assert result["fully_approved"] is False
    assert result["can_project_edit_ready"] is False
    assert result["approval_basis"] == "required_external_approval"


def test_verified_pov_reaction_is_sufficient():
    result = resolve_contextual_approval(
        asset=asset(asset_class="pov"),
        reaction_result=verified_reaction(asset_class="pov"),
    )
    assert result["state"] == "approved"
    assert result["approval_basis"] == "reaction"
    assert result["approval_actor"] == "approver_a"


def test_verified_non_pov_dual_reaction_is_sufficient():
    result = resolve_contextual_approval(
        asset=asset(asset_class="non_pov"),
        reaction_result=verified_reaction(asset_class="non_pov"),
    )
    assert result["state"] == "approved"
    assert result["approval_basis"] == "dual_reaction"
    assert result["approval_actor"] == "approver_a+approver_b"
    assert result["required_approvers"] == ["approver_a", "approver_b"]


def test_non_pov_partial_reaction_stays_partial():
    result = resolve_contextual_approval(
        asset=asset(asset_class="non_pov"),
        reaction_result=verified_reaction(asset_class="non_pov", state="partial"),
    )
    assert result["state"] == "partial"
    assert result["fully_approved"] is False
    assert result["can_project_edit_ready"] is False


@pytest.mark.parametrize("state", ["identity_unverified", "identity_conflict", "evidence_truncated"])
def test_reaction_ambiguity_fails_closed(state):
    result = resolve_contextual_approval(
        asset=asset(),
        reaction_result=verified_reaction(state=state),
    )
    assert result["state"] == state
    assert result["fully_approved"] is False
    assert result["can_project_edit_ready"] is False
    assert result["precedence"] == "reaction_fail_closed"


def test_color_is_always_independent_user_owned_workstream():
    result = resolve_contextual_approval(
        asset=asset(asset_class="non_pov"),
        reaction_result=verified_reaction(asset_class="non_pov"),
        workstream="color",
    )
    assert result["state"] == "independent"
    assert result["approval_basis"] == "user_confirmation"
    assert result["requires_user_confirmation"] is True
    assert result["can_project_edit_ready"] is False


def test_contextual_decision_shape_is_strict():
    with pytest.raises(Exception):
        ContextualDecision(
            source="user",
            decision="small_fix_means_approved",
            evidence_ref="chat-current-turn",
            text="Правка маленькая",
        )
