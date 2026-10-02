"""RCC Show client-dashboard row planning for TM-031.

This module is pure planning/presentation logic. Chats and canonical server state
remain authoritative; the Google Sheet is only a projection.
"""
from __future__ import annotations

from .postproduction_sheet import SHOW_TAB
from .v24.common import Rejected

DEFAULT_STATUS = "Не готово"
DONE_STATES = {"completed", "delivered", "approved"}
NO_WORK_STATES = {"planned", "waiting_materials"}
ACTIVE_STATES = {"active", "in_progress"}
WAITING_STATES = {"waiting_feedback"}


def norm(value):
    return " ".join(str(value or "").strip().casefold().replace("ё", "е").split())


def _catalog(rule_value):
    cfg = (rule_value or {}).get("sheet_project_bootstrap") or {}
    taxonomy = cfg.get("historical_taxonomy") or {}
    canonical = taxonomy.get("canonical") or []
    aliases = taxonomy.get("aliases") or {}
    if not isinstance(canonical, list) or not canonical:
        raise Rejected("postproduction_sheet_taxonomy_missing")
    canonical_by_norm = {norm(value): str(value) for value in canonical if str(value).strip()}
    alias_by_norm = {
        norm(key): str(value)
        for key, value in aliases.items()
        if isinstance(key, str) and isinstance(value, str)
    }
    return cfg, canonical_by_norm, alias_by_norm


def canonical_content_type(value, rule_value):
    _cfg, canonical, aliases = _catalog(rule_value)
    key = norm(value)
    target = aliases.get(key, canonical.get(key))
    if target is None:
        raise Rejected("postproduction_sheet_unknown_content_type")
    target_key = norm(target)
    if target_key not in canonical:
        raise Rejected("postproduction_sheet_taxonomy_alias_invalid")
    return canonical[target_key]


def template_catalog(parsed, rule_value):
    result = {}
    for row in parsed.get("assets") or []:
        if row.get("tab") != SHOW_TAB:
            continue
        try:
            content_type = canonical_content_type(row.get("content_type"), rule_value)
        except Rejected:
            continue
        # Latest historical row is the closest current visual convention.
        current = result.get(content_type)
        if current is None or row["row_number"] > current["row_number"]:
            result[content_type] = row
    return result


def _item_content_type(item, target, rule_value):
    data = item.get("data") or {}
    explicit = data.get("content_type")
    if explicit:
        try:
            return canonical_content_type(explicit, rule_value) == target
        except Rejected:
            return False
    # Compatibility for existing TM-029 items created before content_type was
    # explicit: match only a delimited title segment, never a loose substring.
    title = str(data.get("title") or "")
    parts = [norm(value) for value in title.split("/") if norm(value)]
    return norm(target) in parts


def state_to_status(state):
    if state in DONE_STATES:
        return "Готово"
    if state in WAITING_STATES:
        return "Нужны правки"
    if state in ACTIVE_STATES:
        return "В работе"
    if state in NO_WORK_STATES or state in (None, ""):
        return DEFAULT_STATUS
    if state == "cancelled":
        return DEFAULT_STATUS
    raise Rejected("postproduction_sheet_unknown_workstream_state")


def content_statuses(items, target, rule_value):
    edit = DEFAULT_STATUS
    color = DEFAULT_STATUS
    seen = set()
    for item in items or []:
        data = item.get("data") or {}
        if not _item_content_type(item, target, rule_value):
            continue
        kind = data.get("kind")
        if kind not in ("edit", "color"):
            continue
        if kind in seen:
            raise Rejected("duplicate_postproduction_content_workstream")
        seen.add(kind)
        status = state_to_status(data.get("state"))
        if kind == "edit":
            edit = status
        else:
            color = status
    return {"edit_status": edit, "color_status": color}


def plan_show_rows(parsed, jobs, items_by_parent, rule_value):
    cfg, _canonical, _aliases = _catalog(rule_value)
    if cfg.get("eligible_production_line") != "rcc_rmk":
        raise Rejected("postproduction_sheet_rcc_policy_invalid")
    if cfg.get("required_shoot_state") != "completed":
        raise Rejected("postproduction_sheet_shoot_policy_invalid")
    if cfg.get("required_projection") != "show_default":
        raise Rejected("postproduction_sheet_projection_policy_invalid")

    templates = template_catalog(parsed, rule_value)
    show = (parsed.get("sheets") or {}).get(SHOW_TAB) or {}
    show_sheet_id = show.get("sheet_id")
    last_used_row = show.get("last_used_row")
    if type(show_sheet_id) is not int or type(last_used_row) is not int:
        raise Rejected("postproduction_show_sheet_missing")

    show_rows = [row for row in parsed.get("assets") or [] if row.get("tab") == SHOW_TAB]
    rows_by_event = {}
    for row in show_rows:
        rows_by_event.setdefault(row.get("event"), []).append(row)

    operations = []
    selected_jobs = []
    next_row = last_used_row + 1
    virtual_tail = last_used_row

    for job in jobs:
        entity_id = str(job["id"])
        data = job.get("data") or {}
        if data.get("production_line") != cfg["eligible_production_line"]:
            raise Rejected("postproduction_sheet_non_rcc_project_forbidden")
        if data.get("shoot_state") != cfg["required_shoot_state"]:
            raise Rejected("postproduction_sheet_shoot_not_completed")
        if data.get("sheet_projection") != cfg["required_projection"]:
            raise Rejected("postproduction_sheet_projection_not_enabled")
        event_key = data.get("sheet_event_key")
        if not isinstance(event_key, str) or not event_key.strip():
            raise Rejected("postproduction_sheet_event_key_required")
        requested = data.get(cfg.get("canonical_content_field") or "sheet_content_types")
        if not isinstance(requested, list) or not requested or len(requested) > 20:
            raise Rejected("postproduction_sheet_content_types_required")
        content_types = [canonical_content_type(value, rule_value) for value in requested]
        if len(set(content_types)) != len(content_types):
            raise Rejected("duplicate_postproduction_sheet_content_type")

        existing = rows_by_event.get(event_key, [])
        existing_types = []
        for row in existing:
            try:
                existing_types.append(canonical_content_type(row.get("content_type"), rule_value))
            except Rejected:
                # Existing unknown manual rows are authoritative. Do not touch
                # them, but also do not let them masquerade as requested types.
                continue
        missing = [value for value in content_types if value not in existing_types]
        selected_jobs.append({
            "job_id": entity_id,
            "title": data.get("title") or "",
            "event_key": event_key,
            "content_types": content_types,
            "existing_content_types": existing_types,
            "missing_content_types": missing,
        })
        if not missing:
            continue

        if existing:
            event_tail = max(row["row_number"] for row in existing)
            # We never shift established rows because canonical Sheet bindings
            # use provider coordinates. Extra types may append only while this
            # event is the current tail block.
            if event_tail != virtual_tail:
                raise Rejected("postproduction_sheet_existing_block_not_tail")
            write_event = False
        else:
            write_event = True

        job_items = items_by_parent.get(entity_id, [])
        for index, content_type in enumerate(missing):
            template = templates.get(content_type)
            if template is None:
                raise Rejected("postproduction_sheet_content_type_template_missing")
            statuses = content_statuses(job_items, content_type, rule_value)
            operations.append({
                "action": "insert_show_row",
                "job_id": entity_id,
                "event_key": event_key,
                "event_cell": event_key if write_event and index == 0 else "",
                "content_type": content_type,
                "row_number": next_row,
                "sheet_id": show_sheet_id,
                "template_row_number": template["row_number"],
                "template_content_type": template["content_type"],
                "expected_template_presentation_fingerprint": template["presentation_fingerprint"],
                **statuses,
            })
            next_row += 1
            virtual_tail += 1
        rows_by_event[event_key] = existing + [
            {
                "row_number": op["row_number"],
                "content_type": op["content_type"],
                "event": event_key,
            }
            for op in operations if op["job_id"] == entity_id
        ]

    return {
        "operations": operations,
        "selected_jobs": selected_jobs,
        "sheet_id": show_sheet_id,
        "expected_last_used_row": last_used_row,
        "templates": {
            name: {
                "row_number": row["row_number"],
                "content_type": row["content_type"],
                "presentation_fingerprint": row["presentation_fingerprint"],
            }
            for name, row in templates.items()
        },
    }


def cell_value(value):
    if isinstance(value, bool):
        return {"userEnteredValue": {"boolValue": value}}
    if isinstance(value, (int, float)):
        return {"userEnteredValue": {"numberValue": value}}
    return {"userEnteredValue": {"stringValue": str(value or "")}}


def build_insert_requests(delivery):
    operations = delivery.get("operations") or []
    if not operations:
        return []
    sheet_id = delivery["sheet_id"]
    start_index = delivery["expected_last_used_row"]
    requests = [{
        "insertDimension": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "ROWS",
                "startIndex": start_index,
                "endIndex": start_index + len(operations),
            },
            "inheritFromBefore": False,
        }
    }]
    for operation in operations:
        source = operation["template_row_number"] - 1
        dest = operation["row_number"] - 1
        for paste_type in ("PASTE_FORMAT", "PASTE_DATA_VALIDATION"):
            requests.append({
                "copyPaste": {
                    "source": {
                        "sheetId": sheet_id,
                        "startRowIndex": source,
                        "endRowIndex": source + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": 9,
                    },
                    "destination": {
                        "sheetId": sheet_id,
                        "startRowIndex": dest,
                        "endRowIndex": dest + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": 9,
                    },
                    "pasteType": paste_type,
                    "pasteOrientation": "NORMAL",
                }
            })
        values = [
            operation["event_cell"],
            operation["content_type"],
            "", "", "",
            operation["color_status"],
            operation["edit_status"],
            "", "",
        ]
        requests.append({
            "updateCells": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": dest,
                    "endRowIndex": dest + 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": 9,
                },
                "rows": [{"values": [cell_value(value) for value in values]}],
                "fields": "userEnteredValue",
            }
        })
    return requests
