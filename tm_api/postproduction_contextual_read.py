"""Read facade for TM-022 contextual Postproduction approval."""
from __future__ import annotations

from .postproduction_contextual_approval import ContextualDecision, resolve_contextual_approval
from .v24.common import Rejected


def read_contextual_approval(
    database,
    instance,
    whatsapp_reader,
    workspace_id,
    asset_id,
    *,
    workstream="edit",
    whatsapp_chat_id=None,
    whatsapp_message_id=None,
    contextual_decisions=None,
):
    if bool(whatsapp_chat_id) != bool(whatsapp_message_id):
        raise Rejected("whatsapp_reaction_target_incomplete")
    with database.transaction(read_only=True) as tx:
        workspace = tx.one(
            "SELECT id,status,settings FROM tm_config.workspaces "
            "WHERE instance_id=%s AND id=%s::uuid",
            (instance, workspace_id),
        )
        if not workspace or workspace.get("status") != "active":
            raise Rejected("postproduction_workspace_not_found")
        profile = ((workspace.get("settings") or {}).get("analysis") or {}).get("profile")
        if profile != "postproduction":
            raise Rejected("postproduction_workspace_required")
        rows = tx.all(
            "SELECT id,data,revision FROM tm_config.entities "
            "WHERE instance_id=%s AND workspace_id=%s::uuid "
            "AND entity_type='postproduction-asset' AND status='active' "
            "AND data->>'asset_id'=%s ORDER BY id LIMIT 2",
            (instance, workspace_id, asset_id),
        )
        if not rows:
            raise Rejected("canonical_postproduction_asset_not_found")
        if len(rows) != 1:
            raise Rejected("duplicate_canonical_postproduction_asset_id")
        entity = rows[0]
        asset = entity.get("data") or {}

    reaction_result = None
    if whatsapp_chat_id and whatsapp_message_id:
        reaction_result = whatsapp_reader.postproduction_approval(
            whatsapp_chat_id,
            whatsapp_message_id,
            asset.get("asset_class"),
            workstream,
        )

    decisions = [
        item
        if isinstance(item, ContextualDecision)
        else ContextualDecision.model_validate(item)
        for item in (contextual_decisions or [])
    ]
    result = resolve_contextual_approval(
        asset=asset,
        reaction_result=reaction_result,
        contextual_decisions=decisions,
        workstream=workstream,
    )
    return {
        **result,
        "canonical_entity_id": str(entity["id"]),
        "canonical_entity_revision": entity["revision"],
        "whatsapp_reaction_evaluated": reaction_result is not None,
        "whatsapp_reaction_result": reaction_result,
        "read_only": True,
        "mutates_canonical_state": False,
        "mutates_sheet": False,
        "mutates_reminders": False,
    }
