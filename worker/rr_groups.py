"""V18: pure group routing and exact selector matching (no credentials or I/O)."""
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


def norm(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().casefold().replace("ё", "е"))


def norm_username(value: str) -> str:
    return norm(value).lstrip("@")


class ConfigurationError(RuntimeError):
    pass


class GroupPaused(ConfigurationError):
    pass


class GroupConfig:
    def __init__(self, raw: Dict[str, Any]):
        if not isinstance(raw, dict) or raw.get("schema_version") != 7:
            raise ConfigurationError("Требуется совместимая серверная схема групп V7")
        for key in ("groups", "members", "targets", "chats"):
            if not isinstance(raw.get(key), list):
                raise ConfigurationError("Неполная конфигурация групп: " + key)
        self.raw = raw
        self.groups = {int(g["id"]): g for g in raw["groups"]}
        if len(self.groups) != len(raw["groups"]):
            raise ConfigurationError("Повторяющийся ID группы")
        self.chats = {int(c["chat_id"]): c for c in raw["chats"]}
        self.targets = raw["targets"]
        self.by_chat: Dict[int, List[int]] = {}
        resolved = {(int(t["group_id"]), int(t["resolved_chat_id"]))
                    for t in self.targets if t.get("enabled") and t.get("resolution_status") == "resolved"
                    and t.get("resolved_chat_id") is not None}
        for m in raw["members"]:
            gid, cid = int(m["group_id"]), int(m["chat_id"])
            g, c = self.groups.get(gid), self.chats.get(cid)
            if not g or not c or not m.get("enabled", True) or not c.get("enabled"):
                continue
            if not g.get("enabled") or not g.get("monitoring_enabled"):
                continue
            if m.get("managed_by_targets") and (gid, cid) not in resolved:
                continue
            self.by_chat.setdefault(cid, []).append(gid)
        for ids in self.by_chat.values():
            ids.sort()
        # Empty means empty: never revert to the old whitelist when paused.
        self.whitelist = {cid: self.chats[cid]["chat_name"] for cid in self.by_chat}
        encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        self.revision = hashlib.sha256(encoded).hexdigest()[:20]

    def monitored_groups(self) -> List[Dict[str, Any]]:
        return sorted((g for g in self.groups.values() if g.get("enabled") and g.get("monitoring_enabled")),
                      key=lambda g: (g.get("priority", 100), g["id"]))

    def pending_targets(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        now = now or datetime.now(timezone.utc)
        active = {g["id"] for g in self.monitored_groups()}
        out = []
        for t in self.targets:
            if t["group_id"] not in active or not t.get("enabled"):
                continue
            if t.get("resolution_status") not in ("pending", "not_found", "error"):
                continue
            due = t.get("next_resolution_at")
            if due:
                # PostgreSQL supports fractional seconds with fewer than six digits.
                text = due.replace("Z", "+00:00")
                text = re.sub(r"\.(\d+)(?=[+-]|$)", lambda m: "." + m[1][:6].ljust(6, "0"), text)
                try:
                    retry = datetime.fromisoformat(text)
                    if retry.tzinfo is None:
                        retry = retry.replace(tzinfo=timezone.utc)
                    if retry > now:
                        continue
                except ValueError:
                    continue  # Invalid retry date must not cause a busy loop.
            out.append(t)
        return out

    def destination(self, task: Dict[str, Any], default: str = "Рентал") -> str:
        gid = task.get("context_group_id")
        if gid is None and task.get("source_chat_id") is not None:
            matches = self.by_chat.get(int(task["source_chat_id"]), [])
            if len(matches) != 1:
                raise ConfigurationError("Группа задачи не определена однозначно; требуется выбор в чате")
            gid = matches[0]
        if gid is None:
            return default  # Legacy manually created tasks only.
        g = self.groups.get(int(gid))
        if not g:
            raise ConfigurationError("Неизвестная группа; отправка в список по умолчанию запрещена")
        if not g.get("enabled"):
            raise GroupPaused("Группа на паузе; существующее напоминание не трогаем")
        if g.get('reminder_list_id'):
            if g.get('reminder_list_instance_id') not in (None,'macbook-owner'):
                raise ConfigurationError('Список привязан к другому Mac; требуется выбор')
            return 'id:'+str(g['reminder_list_id'])
        name = (g.get("reminder_list_name") or "").strip()
        if not name:
            raise ConfigurationError("У группы не задан список Reminders")
        return name

    def summary(self) -> Dict[str, Any]:
        return {"config_revision": self.revision, "monitored_chats": len(self.whitelist),
                "pending_targets": len(self.pending_targets()),
                "groups": [{"group_key": g["group_key"], "name": g.get("name"), "rules_profile": g.get("rules_profile","default"), "enabled": bool(g.get("enabled")),
                            "monitoring_enabled": bool(g.get("monitoring_enabled")),
                            "notifications_enabled": bool(g.get("notifications_enabled")),
                            "reminder_list_name": g.get("reminder_list_name"),
                            "members": sum(g["id"] in ids for ids in self.by_chat.values())}
                           for g in sorted(self.groups.values(), key=lambda x: x["id"])]}


def match_target(target: Dict[str, Any], dialogs: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:
    """Match exact normalized titles/usernames; do not guess similar people."""
    chosen = target.get("chosen_chat_id")
    if chosen is not None:
        matches = [d for d in dialogs if int(d["chat_id"]) == int(chosen)]
    elif target.get("selector_kind") == "username":
        wanted = norm_username(target["selector_value"])
        matches = [d for d in dialogs if wanted and wanted in {norm_username(u) for u in d.get("usernames", [])}]
    elif target.get("selector_kind") == "title":
        wanted = norm(target["selector_value"])
        matches = [d for d in dialogs if wanted and norm(d.get("name")) == wanted]
    else:
        raise ConfigurationError("Неизвестный вид запроса на поиск")
    unique = {int(d["chat_id"]): d for d in matches}
    matches = sorted(unique.values(), key=lambda x: x["chat_id"])
    return ("resolved" if len(matches) == 1 else "ambiguous" if matches else "not_found", matches)
