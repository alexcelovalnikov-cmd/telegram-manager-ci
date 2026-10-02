from uuid import NAMESPACE_URL, uuid5


def postproduction_reminder_id(instance, workspace_id, asset_id, workstream):
    key = f"tm-postproduction-reminder:{instance}:{workspace_id}:{asset_id}:{workstream}"
    return str(uuid5(NAMESPACE_URL, key))
