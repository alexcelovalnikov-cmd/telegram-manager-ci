"""Canonical Postproduction workstream lifecycle (TM-029).

Canonical work existence is independent from Reminder projection.
Only active has an executable user action.
"""
from __future__ import annotations

from copy import deepcopy

from .v24.common import Rejected

LIFECYCLE_STATES = (
    "planned", "waiting_materials", "active", "waiting_feedback", "completed", "cancelled",
)
REMINDER_STATES = {"active"}
NO_REMINDER_STATES = {"planned", "waiting_materials", "waiting_feedback", "completed", "cancelled"}

EVENTS = {
    "assignment_confirmed",
    "materials_received",
    "action_started",
    "result_sent",
    "approval_received",
    "corrections_received",
    "cancelled",
}

AUTO_CLOSE_POLICIES = {
    "delivery_auto_close",
    "delivery_auto_close_reopen_on_corrections",
}


def transition(current, event, *, next_action=None, blocker=None,
               completion_policy="explicit_approval", evidence_ref=None):
    if current not in LIFECYCLE_STATES:
        raise Rejected("invalid_postproduction_lifecycle_state")
    if event not in EVENTS:
        raise Rejected("invalid_postproduction_lifecycle_event")

    state = current
    action = next_action
    block = blocker

    if event == "assignment_confirmed":
        state = "waiting_materials"
        action = None
    elif event == "materials_received":
        if current not in ("planned", "waiting_materials"):
            raise Rejected("materials_received_invalid_transition")
        if not next_action:
            raise Rejected("materials_received_requires_next_action")
        state = "active"
        block = None
    elif event == "action_started":
        if not next_action:
            raise Rejected("active_work_requires_next_action")
        state = "active"
        block = None
    elif event == "result_sent":
        action = None
        block = None
        state = "completed" if completion_policy in AUTO_CLOSE_POLICIES else "waiting_feedback"
    elif event == "approval_received":
        state = "completed"
        action = None
        block = None
    elif event == "corrections_received":
        if not next_action:
            raise Rejected("corrections_require_next_action")
        state = "active"
        block = None
    elif event == "cancelled":
        state = "cancelled"
        action = None
        block = None

    result = {
        "state": state,
        "next_action": action,
        "blocker": block,
        "reminder_required": state in REMINDER_STATES,
        "completion_policy": completion_policy,
    }
    if evidence_ref:
        result["evidence_ref"] = evidence_ref
    return result


def apply_to_item(data, event, **kwargs):
    current = data.get("state") or "planned"
    decision = transition(current, event, **kwargs)
    result = deepcopy(data)
    result["state"] = decision["state"]
    if decision["next_action"]:
        result["next_action"] = decision["next_action"]
    else:
        result.pop("next_action", None)
    if decision["blocker"]:
        result["blocker"] = decision["blocker"]
    else:
        result.pop("blocker", None)
    result["completion_policy"] = decision["completion_policy"]
    return result, decision
