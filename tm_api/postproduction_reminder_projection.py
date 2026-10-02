"""Canonical Postproduction asset -> server Reminder projection (TM-023/TM-032).

Reminders are a user dashboard derived only from canonical server workstreams.
Google Sheet state is never read here and cannot create, close or reopen a
Reminder. Manual CalDAV edits still fail closed.
"""
from copy import deepcopy
from uuid import UUID

from tm_calendar import repository as calendars
from tm_reminders import codec
from tm_reminders.models import Reminder

from .postproduction_identity import postproduction_reminder_id
from .postproduction_projection import WORKSTREAMS, reminder_decision
from .v24.common import Rejected, check_revision, strict_fields

OPS = {"update_postproduction_reminder_projection"}


def _dedupe_key(workspace_id, asset_id, workstream):
    return f"postprod.reminder:{workspace_id}:{asset_id}:{workstream}"


def _validate_asset_id(value):
    if not isinstance(value, str):
        raise Rejected("invalid_postproduction_asset_id")
    try:
        UUID(value)
    except (ValueError, AttributeError):
        raise Rejected("invalid_postproduction_asset_id") from None
    return value


def desired_action(asset, item, parent_title, workstream):
    return reminder_decision(asset, item, parent_title, workstream)


class PostproductionReminderBusiness:
    def __init__(self, instance, credentials_path):
        self.instance = instance
        self.credentials_path = credentials_path

    def _load(self, tx, m):
        workspace = tx.one(
            "SELECT id,workspace_key,status,settings,revision FROM tm_config.workspaces "
            "WHERE instance_id=%s AND id=%s::uuid",
            (self.instance, m["target_id"]),
        )
        if not workspace or workspace.get("status") != "active":
            raise Rejected("postproduction_workspace_not_found")
        if str(workspace["id"]) != str(m.get("workspace_id")):
            raise Rejected("postproduction_workspace_target_mismatch")
        settings = workspace.get("settings") or {}
        if (settings.get("analysis") or {}).get("profile") != "postproduction":
            raise Rejected("postproduction_workspace_required")
        check_revision(workspace["revision"], m["expected_revision"])
        destination = settings.get("reminder_list") or {}
        if destination.get("mode") not in ("server_new", "server_existing") or not destination.get("id"):
            raise Rejected("postproduction_server_reminder_list_required")
        strict_fields(m["changes"], ("asset_ids", "workstreams"), ("asset_ids",))
        asset_ids = m["changes"]["asset_ids"]
        if (
            not isinstance(asset_ids, list) or not 1 <= len(asset_ids) <= 50
            or len(set(asset_ids)) != len(asset_ids)
        ):
            raise Rejected("invalid_postproduction_asset_set")
        asset_ids = [_validate_asset_id(value) for value in asset_ids]
        workstreams = m["changes"].get("workstreams", list(WORKSTREAMS))
        if (
            not isinstance(workstreams, list) or not workstreams
            or len(set(workstreams)) != len(workstreams)
            or any(value not in WORKSTREAMS for value in workstreams)
        ):
            raise Rejected("invalid_postproduction_workstreams")
        rows = tx.all(
            "SELECT id,entity_type,data,revision,status FROM tm_config.entities "
            "WHERE instance_id=%s AND workspace_id=%s::uuid AND status='active' "
            "AND entity_type IN ('postproduction-asset','postproduction-job','postproduction-item') "
            "ORDER BY id LIMIT 801",
            (self.instance, m["workspace_id"]),
        )
        if len(rows) > 800:
            raise Rejected("postproduction_entity_limit")
        assets, parents, items = {}, {}, {}
        for record in rows:
            data = record.get("data") or {}
            entity_type = record.get("entity_type")
            if entity_type == "postproduction-job":
                parents[str(record["id"])] = data.get("title") or ""
                continue
            if entity_type == "postproduction-item":
                asset_id = data.get("asset_id")
                kind = data.get("kind")
                if isinstance(asset_id, str) and kind in WORKSTREAMS:
                    key = (asset_id, kind)
                    if key in items:
                        raise Rejected("duplicate_canonical_postproduction_workstream")
                    items[key] = {
                        "entity_id": str(record["id"]),
                        "revision": record["revision"],
                        "data": data,
                    }
                continue
            asset_id = data.get("asset_id")
            if isinstance(asset_id, str):
                if asset_id in assets:
                    raise Rejected("duplicate_canonical_postproduction_asset_id")
                assets[asset_id] = {"entity_id": str(record["id"]), "data": data}
        missing = [value for value in asset_ids if value not in assets]
        if missing:
            raise Rejected("canonical_postproduction_asset_not_found")
        return (
            workspace, destination, [assets[value] for value in asset_ids],
            parents, items, workstreams,
        )

    def _event_rows(self, tx, reminder_ids):
        if not reminder_ids:
            return {}
        rows = tx.all(
            "SELECT * FROM tm_calendar.events WHERE id=ANY(%s::uuid[])",
            (reminder_ids,),
        )
        return {str(row["id"]): row for row in rows}

    def plan(self, tx, m, lock=False):
        if m["operation"] not in OPS:
            raise Rejected("unsupported_postproduction_reminder_operation")
        workspace, destination, assets, parents, items, workstreams = self._load(tx, m)
        user = calendars.api_user(tx, self.instance)
        calendar = calendars.access(tx, user, destination["id"], write=True)
        if calendar.get("component_type") != "VTODO":
            raise Rejected("postproduction_destination_not_todo")
        reminder_ids = [
            postproduction_reminder_id(
                self.instance, workspace["id"], asset["data"]["asset_id"], workstream
            )
            for asset in assets for workstream in workstreams
        ]
        existing = self._event_rows(tx, reminder_ids)
        operations, states = [], []

        for asset_record in assets:
            asset = asset_record["data"]
            if not isinstance(asset.get("asset_id"), str):
                raise Rejected("invalid_postproduction_asset_id")
            parent_title = parents.get(str(asset.get("parent_id")), "")

            for workstream in workstreams:
                canonical_item = items.get((asset["asset_id"], workstream))
                rid = postproduction_reminder_id(
                    self.instance, workspace["id"], asset["asset_id"], workstream
                )
                current = existing.get(rid)
                decision = desired_action(asset, canonical_item, parent_title, workstream)
                state = {
                    "asset_id": asset["asset_id"],
                    "workstream": workstream,
                    "canonical_item_id": (
                        canonical_item["entity_id"] if canonical_item else None
                    ),
                    "canonical_item_revision": (
                        canonical_item["revision"] if canonical_item else None
                    ),
                    "reminder_id": rid,
                    "decision": decision,
                    "current": calendars.event_public(current) if current else None,
                }
                states.append(state)
                op = self._plan_one(
                    workspace, destination, asset, workstream, rid, current, decision
                )
                if op:
                    operations.append(op)

        if not operations:
            raise Rejected("postproduction_reminder_projection_no_changes")
        before = {
            "workspace_revision": workspace["revision"],
            "authority": "canonical_server_state",
            "sheet_input": False,
            "states": states,
        }
        after = {
            **deepcopy(before),
            "projection_operations": [
                {key: value for key, value in op.items() if not key.startswith("_")}
                for op in operations
            ],
            "status": "approved_for_projection",
        }
        return {
            "entity": "postproduction_reminder_projection",
            "operation": m["operation"],
            "target_id": str(workspace["id"]),
            "before": before,
            "after": after,
            "_writes": operations,
        }

    def _plan_one(
        self, workspace, destination, asset, workstream, rid, current, decision
    ):
        mode = decision["mode"]
        if mode == "preserve":
            return None
        if current and current.get("deleted_at") is not None:
            raise Rejected("postproduction_reminder_manual_delete")
        if current and str(current.get("calendar_id")) != str(destination["id"]):
            raise Rejected("postproduction_reminder_wrong_list")
        if mode == "complete":
            if not current or current["fields"].get("status") in ("completed", "cancelled"):
                return None
            if current.get("source") == "caldav":
                raise Rejected("postproduction_reminder_manual_override")
            fields = Reminder.model_validate(
                current["fields"] | {"status": "completed"}
            ).model_dump()
            text = codec.encode(
                fields, current["caldav_uid"], current["icalendar"], changed={"status"}
            )
            return {
                "action": "complete", "asset_id": asset["asset_id"],
                "workstream": workstream, "reminder_id": rid,
                "expected_revision": current["revision"], "_icalendar": text,
                "_href": current["href"],
            }

        fields = Reminder(
            title=decision["title"], description=decision["description"],
            timezone=workspace["settings"]["timezone"], status="open",
        ).model_dump()
        if current:
            same = all(
                current["fields"].get(key) == fields.get(key)
                for key in ("title", "description", "status", "due_at", "all_day")
            )
            if same:
                return None
            if current.get("source") == "caldav":
                raise Rejected("postproduction_reminder_manual_override")
            text = codec.encode(
                fields, current["caldav_uid"], current["icalendar"],
                changed={"title", "description", "status", "due_at", "all_day"},
            )
            return {
                "action": "update", "asset_id": asset["asset_id"],
                "workstream": workstream, "reminder_id": rid,
                "expected_revision": current["revision"], "_icalendar": text,
                "_href": current["href"],
            }

        uid = rid + "@telegram-manager"
        text = codec.encode(fields, uid)
        return {
            "action": "create", "asset_id": asset["asset_id"],
            "workstream": workstream, "reminder_id": rid,
            "dedupe_key": _dedupe_key(
                str(workspace["id"]), asset["asset_id"], workstream
            ),
            "_icalendar": text, "_href": rid + ".ics",
        }

    def execute(self, tx, m, plan):
        user = calendars.api_user(tx, self.instance)
        workspace = tx.one(
            "SELECT settings FROM tm_config.workspaces WHERE id=%s::uuid AND instance_id=%s",
            (plan["target_id"], self.instance),
        )
        destination = (workspace.get("settings") or {}).get("reminder_list") or {}
        results = []
        for item in plan.get("_writes") or []:
            if item["action"] == "create":
                row = calendars.put(
                    tx, user, destination["id"], item["_href"], item["_icalendar"],
                    ident=item["reminder_id"], source="telegram_manager",
                )
            else:
                row = calendars.put(
                    tx, user, destination["id"], item["_href"], item["_icalendar"],
                    expected_revision=item["expected_revision"],
                    ident=item["reminder_id"], source="telegram_manager",
                )
            results.append({
                "asset_id": item["asset_id"],
                "workstream": item["workstream"],
                "reminder_id": item["reminder_id"],
                "action": item["action"],
                "revision": row["revision"],
                "status": row["fields"]["status"],
            })
        return {
            "entity": "postproduction_reminder_projection",
            "id": plan["target_id"],
            "operation_count": len(results),
            "results": results,
        }
