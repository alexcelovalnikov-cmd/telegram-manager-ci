"""Bounded, explainable Telegram dialog discovery for Postproduction.

The privacy boundary is deliberately split in two stages:
1. dialog identity metadata (chat id/name/type) is checked against global source
   exclusions;
2. only non-excluded dialogs may expose a current message or forum topics to
   Postproduction discovery.

No candidate media is downloaded here. Private-chat evidence is limited to the
single current dialog message for a small bounded set of recent non-excluded
private dialogs; all other private candidates remain metadata-only.
"""
import asyncio
import re

DIALOG_DISCOVERY_SCAN_LIMIT = 200
DIALOG_DISCOVERY_PUBLISH_LIMIT = 100
DIALOG_PRIVATE_EVIDENCE_LIMIT = 12
DIALOG_DISCOVERY_GROUP_KINDS = {"group", "supergroup", "forum"}
DYNAMIC_SOURCE_KINDS = {"group", "supergroup", "forum", "private"}

POSTPRODUCTION_TOPIC_ALIASES = {
    "general": "general", "общие": "general", "общий": "general",
    "color": "color", "colour": "color", "cc": "color", "цк": "color",
    "цвет": "color", "цветокор": "color", "цветокоррекция": "color",
    "edit": "editing", "editing": "editing", "монтаж": "editing", "монтажи": "editing",
    "cleanup": "cleanup", "клинап": "cleanup",
    "vfx": "vfx", "cg": "vfx",
    "sound": "sound", "audio": "sound", "звук": "sound", "саунд": "sound",
    "delivery": "delivery", "export": "delivery", "render": "delivery", "рендер": "delivery",
    "выдача": "delivery", "экспорт": "delivery",
}
POSTPRODUCTION_SIGNAL_KINDS = {"color", "editing", "cleanup", "vfx", "sound", "delivery"}
POSTPRODUCTION_KEYWORDS = (
    "postproduction", "post production", "postprod", "постпрод",
    "монтаж", "editing", " edit ", "color", "colour", "цвет",
    "cleanup", "clean up", "клинап", "vfx", "retouch", "ретуш",
    "sound", "audio", "звук", "delivery", "export", "render", "рендер", "выдача", "экспорт",
)

# Strong evidence requires a state/action, not a bare production word.
POST_START_PATTERNS = (
    ("editing_started", r"\b(?:перв(?:ый|ого)\s+драфт|чернов(?:ой|ик)\s+(?:монтаж|верс)|монтаж\s+(?:готов|собран|начат|начали|делаем)|нов(?:ая|ую)\s+верси(?:я|ю).{0,40}(?:готов|собран|отправлен)|v\d+\s+(?:готов(?:а|о)?|собран(?:а|о)?|отправлен(?:а|о)?|отправил[аи]?))"),
    ("color_started", r"\b(?:цветокор(?:рекция)?\s+(?:начат|готов|сделан)|начал[аи]?\s+цветокор|покрасил[аи]?\s+(?:ролик|кадр|верси)|грейд(?:инг)?\s+(?:готов|начат))"),
    ("cleanup_vfx_started", r"\b(?:(?:клинап|cleanup|ретуш|retouch|vfx|cg)\s+(?:начат|готов|сделан|делаем)|начал[аи]?\s+(?:клинап|cleanup|ретуш|retouch|vfx|cg))"),
    ("sound_started", r"\b(?:(?:звук|sound|audio|сведение)\s+(?:начат|готов|сделан|свед[её]н)|начал[аи]?\s+(?:звук|сведение))"),
    ("client_corrections", r"\b(?:клиент.{0,35}(?:прислал|дал|написал).{0,25}правк|(?:есть|пришл|получил|внеси|нужно).{0,20}правк[аи].{0,35}(?:кадр|монтаж|верс|цвет|ролик))"),
    ("delivery_started", r"\b(?:(?:экспорт|рендер|финал|delivery|export).{0,30}(?:готов|выгруж|отправ|залит|собран)|(?:выгрузил|отправил).{0,30}(?:финал|рендер|экспорт))"),
    ("approved_post", r"\b(?:(?:верси|ролик|монтаж|цвет|финал).{0,30}(?:согласован|утвержд[её]н|approved|апрув)|(?:согласован|утвержд[её]н|approved|апрув).{0,30}(?:верси|ролик|монтаж|цвет|финал))"),
)
WAITING_MATERIALS_PATTERNS = (
    r"\b(?:жд[её]м|жду|ожидаем).{0,40}(?:исходник|материал|дорожк|запис|файл).{0,45}(?:монтаж|интро|ролик|цвет|клин|правк)",
    r"\b(?:нет|не хватает|не прислал[аи]?|не получили).{0,35}(?:исходник|материал|дорожк|запис|файл).{0,45}(?:монтаж|интро|ролик|цвет|клин|правк)",
    r"\b(?:отдаю тебе|отдадим тебе).{0,25}(?:на )?(?:монтаж|цветокор|цвет).{0,45}(?:после съ[её]мк|когда будут|жд[её]м)",
)
WAITING_FEEDBACK_PATTERNS = (
    r"\b(?:жд[её]м|ожидаем).{0,35}(?:фидбек|feedback|правк|согласован|подтвержден).{0,30}(?:клиент)?",
    r"\b(?:жд[её]м|ожидаем).{0,25}клиент.{0,35}(?:по|на).{0,20}(?:верси|монтаж|цвет|ролик|финал|рендер|драфт)",
    r"\b(?:отправил[аи]?|показал[аи]?).{0,30}клиент.{0,20}(?:на согласован|на просмотр|верси|монтаж|ролик)",
    r"\b(?:верси|монтаж|цвет|ролик|финал|рендер|драфт).{0,35}(?:на согласовании|жд[её]м ответа клиента)",
)
NON_START_PATTERNS = (
    ("shoot_only", r"\b(?:съ[её]мк|снимать|снимаем|съ[её]мочный)"),
    ("estimate_only", r"\b(?:смет|estimate|расч[её]т стоимости)"),
    ("payment_only", r"\b(?:оплат|сч[её]т|аванс|гонорар)"),
    ("equipment_only", r"\b(?:оборудован|камера|объектив|свет|рентал|аренд)"),
    ("logistics_only", r"\b(?:логист|трансфер|такси|привез|забер|доставка оборудования)"),
    ("participants_only", r"\b(?:знакомьтесь|представляю|это наш|это наша).{0,40}(?:монтажер|режисс|продюсер|колорист)"),
    ("future_post_only", r"\b(?:монтаж|цветокор|постпрод).{0,35}(?:будет позже|потом|после съ[её]мк|ещ[её] не|пока не начина)"),
)
POST_MENTION_PATTERN = re.compile(
    r"\b(?:v\d+|монтаж|цветокор|цветокоррек|postprod|постпрод|cleanup|клинап|vfx|cg|ретуш|звук|sound|export|экспорт|рендер|правк|верси|драфт)\w*",
    re.I,
)
OWNER_PATTERN = r"(?:владелец|владелец|владелец)"
ACTION_PATTERN = re.compile(
    rf"\b{OWNER_PATTERN}\b\s*[,!:—-]*\s*(поправь|исправь|подправь|убери|замени|добавь|сделай|перекрась|пересобери|экспортни|выгрузи)\s+([^.!?\n]{{2,140}})",
    re.I,
)
ACTION_VERBS = {
    "поправь": "Поправить", "исправь": "Исправить", "подправь": "Подправить",
    "убери": "Убрать", "замени": "Заменить", "добавь": "Добавить",
    "сделай": "Сделать", "перекрась": "Перекрасить", "пересобери": "Пересобрать",
    "экспортни": "Экспортировать", "выгрузи": "Выгрузить",
}


def discovery_due(last_seen, now, interval=600):
    """Run once immediately, then no more often than the configured interval."""
    return last_seen is None or now - last_seen >= interval


def normalize_display_name(value):
    return re.sub(r"\s+", " ", (value or "").strip().casefold().replace("ё", "е"))


def excluded_reason(chat_id, name, excluded_names=None, excluded_chat_ids=None):
    """Return the matched exclusion key without reading any chat content."""
    excluded_chat_ids = {
        int(value) for value in (excluded_chat_ids or ())
        if not isinstance(value, bool) and str(value).strip().lstrip("-").isdigit()
    }
    if int(chat_id) in excluded_chat_ids:
        return "chat_id"
    names = {normalize_display_name(x) for x in (excluded_names or ())
             if isinstance(x, str) and x.strip()}
    if normalize_display_name(name) in names:
        return "display_name"
    return None


def dialog_kind(entity):
    name = type(entity).__name__
    if name == "User":
        return "bot" if bool(getattr(entity, "bot", False)) else "private"
    if name == "Chat":
        return "group"
    if name == "Channel":
        if bool(getattr(entity, "broadcast", False)) and not bool(getattr(entity, "megagroup", False)):
            return "channel"
        return "forum" if bool(getattr(entity, "forum", False)) else "supergroup"
    return "other"


async def forum_topics(client, entity):
    if not bool(getattr(entity, "forum", False)):
        return []
    try:
        from telethon.tl.functions.messages import GetForumTopicsRequest
        peer = await client.get_input_entity(entity)
        response = await asyncio.wait_for(client(GetForumTopicsRequest(
            peer=peer, offset_date=None, offset_id=0, offset_topic=0,
            limit=30, q=None)), timeout=15)
        return [{"id": int(topic.id), "title": (getattr(topic, "title", "") or "")[:120]}
                for topic in response.topics[:30]]
    except Exception:
        return []


def topic_key(title):
    match = re.search(r"[a-zA-Zа-яА-Я0-9]+", (title or "").casefold().replace("ё", "е"))
    return match.group(0) if match else ""


def postproduction_topic_kind(title):
    return POSTPRODUCTION_TOPIC_ALIASES.get(topic_key(title))


def postproduction_topic_allowed(title):
    return postproduction_topic_kind(title) is not None


def concrete_owner_action(text):
    """Extract only an explicitly addressed, concrete owner action."""
    match = ACTION_PATTERN.search(text or "")
    if not match:
        return None
    verb = ACTION_VERBS.get(match.group(1).casefold())
    raw = re.sub(r"\s+", " ", match.group(2)).strip(" ,;:-")
    if not verb or not raw:
        return None
    parts = re.split(r"\s*[,;—]\s*", raw, maxsplit=1)
    obj = re.sub(r"^(?:пожалуйста\s+)", "", parts[0].strip(), flags=re.I)
    description = parts[1].strip() if len(parts) > 1 else ""
    title = f"{verb} {obj}".strip()
    if len(title) > 120:
        return None
    if description:
        description = re.sub(r"^(?:там|потому что|так как)\s+", "", description,
                             flags=re.I).strip()[:300]
        if description:
            description = description[0].upper() + description[1:]
    return {"title": title, "description": description}


def classify_postproduction_evidence(row):
    """Deterministic, explainable and fail-closed Postproduction start classifier."""
    evidence = row.get("_evidence_preview") or row.get("last_message_preview") or ""
    text = " " + re.sub(r"\s+", " ", evidence).casefold().replace("ё", "е") + " "
    positive = [code for code, pattern in POST_START_PATTERNS if re.search(pattern, text, re.I)]
    waiting = bool(POST_MENTION_PATTERN.search(text)) and any(
        re.search(pattern, text, re.I) for pattern in WAITING_FEEDBACK_PATTERNS)
    waiting_materials = any(
        re.search(pattern, text, re.I) for pattern in WAITING_MATERIALS_PATTERNS)
    negative = [code for code, pattern in NON_START_PATTERNS if re.search(pattern, text, re.I)]
    action = concrete_owner_action(evidence)
    if action and re.search(r"\b(?:кадр|шот|shot|сцен|монтаж|верс|ролик|цвет|грейд|клинап|ретуш|vfx|cg|звук|рендер|экспорт|финал)\w*", text, re.I):
        positive.append("concrete_revision_action")
    if waiting:
        positive.append("waiting_feedback_after_post")
    if waiting_materials:
        positive.append("waiting_materials")
    positive = list(dict.fromkeys(positive))

    approval_only = bool(positive) and set(positive) == {"approved_post"}
    if waiting_materials and set(positive).issubset({"waiting_materials"}):
        state = "waiting_materials"
    elif positive and not (negative and approval_only):
        state = "post_started"
    elif negative:
        state = "not_started"
    elif POST_MENTION_PATTERN.search(text):
        state = "ambiguous"
    else:
        state = "ambiguous"

    action = None if waiting else action
    return {
        "state": state,
        "signals": positive,
        "non_start_signals": negative,
        "waiting_feedback": bool(waiting),
        "waiting_materials": bool(waiting_materials),
        "user_action": action,
        "explainable": True,
        "classifier": "deterministic_v105",
    }


async def read_candidates(client, limit=DIALOG_DISCOVERY_SCAN_LIMIT, *,
                          excluded_names=None, excluded_chat_ids=None,
                          private_evidence_limit=DIALOG_PRIVATE_EVIDENCE_LIMIT):
    """Read bounded candidates with the exclusion guard before content access."""
    rows, topic_index, skipped = [], {}, []
    private_evidence_used = 0
    async for dialog in client.iter_dialogs(limit=limit):
        entity = dialog.entity
        kind = dialog_kind(entity)
        chat_id = int(dialog.id)
        name = (dialog.name or "")[:200]

        reason = excluded_reason(chat_id, name, excluded_names, excluded_chat_ids)
        if reason:
            skipped.append({
                "chat_id": chat_id,
                "status": "skipped_by_exclusion",
                "matched_by": reason,
            })
            continue

        usernames = []
        if getattr(entity, "username", None):
            usernames.append(entity.username)
        usernames += [u.username for u in (getattr(entity, "usernames", None) or [])
                      if getattr(u, "active", False)]

        topics = await forum_topics(client, entity) if kind == "forum" else []
        for topic in topics:
            topic_index[(chat_id, int(topic["id"]))] = topic["title"]

        message = None
        evidence_status = "metadata_only"
        if kind == "private":
            if private_evidence_used < private_evidence_limit:
                message = getattr(dialog, "message", None)
                private_evidence_used += 1
                evidence_status = "single_current_message" if message else "no_current_message"
            else:
                evidence_status = "bounded_private_limit"
        elif kind not in ("bot", "channel", "other"):
            message = getattr(dialog, "message", None)
            evidence_status = "single_current_message" if message else "no_current_message"

        evidence_preview = ((getattr(message, "message", "") or "")[:700] if message else "")
        date = getattr(message, "date", None) if message else None
        public_preview = "" if kind in ("private", "bot") else evidence_preview
        row = {
            "chat_id": chat_id,
            "name": name,
            "kind": kind,
            "username": usernames[0] if usernames else None,
            "forum": kind == "forum",
            "topic_titles": [topic["title"] for topic in topics],
            "last_message_at": date.isoformat() if date else None,
            "last_message_preview": public_preview,
            "_evidence_preview": evidence_preview,
            "_evidence_status": evidence_status,
        }
        row["_classification"] = classify_postproduction_evidence(row)
        rows.append(row)
    return rows, topic_index, skipped


def publish_candidates(rows, limit=DIALOG_DISCOVERY_PUBLISH_LIMIT):
    """Publish safe metadata; private evidence never appears on this surface."""
    group_like = [row for row in rows if row.get("kind") in DIALOG_DISCOVERY_GROUP_KINDS]
    others = [row for row in rows if row.get("kind") not in DIALOG_DISCOVERY_GROUP_KINDS]
    out = []
    for source in (group_like + others)[:limit]:
        row = {k: v for k, v in source.items() if not k.startswith("_")}
        if row.get("kind") in ("private", "bot"):
            row["last_message_preview"] = ""
        out.append(row)
    return out


def discovery_bundle(rows, skipped, private_limit=DIALOG_PRIVATE_EVIDENCE_LIMIT):
    """Persist an auditable explanation without persisting private message text."""
    candidates = []
    for row in rows:
        classification = row.get("_classification") or classify_postproduction_evidence(row)
        candidates.append({
            "chat_id": int(row["chat_id"]),
            "name": row.get("name") or "",
            "kind": row.get("kind"),
            "evidence_status": row.get("_evidence_status") or "metadata_only",
            "classification": classification["state"],
            "signals": list(classification.get("signals") or []),
            "non_start_signals": list(classification.get("non_start_signals") or []),
            "waiting_feedback": bool(classification.get("waiting_feedback")),
            "user_action": classification.get("user_action"),
            "activation_eligible": (
                row.get("kind") in DYNAMIC_SOURCE_KINDS
                and classification["state"] == "post_started"
            ),
        })
    return {
        "privacy_guard": "exclusions_before_content",
        "classifier": "deterministic_v105",
        "private_evidence_limit": private_limit,
        "candidates": candidates,
        "skipped_count": len(skipped),
        "skipped": [{"status": "skipped_by_exclusion"} for _ in skipped],
        "excluded_content_exposed": False,
        "candidate_media_processed": False,
    }


def activation_candidates(rows):
    """Return deterministic active or externally-blocked Postproduction candidates."""
    out = []
    for row in rows:
        classification = row.get("_classification") or classify_postproduction_evidence(row)
        if (row.get("kind") in DYNAMIC_SOURCE_KINDS
                and classification.get("state") in ("post_started", "waiting_materials")):
            out.append(row)
    return out


def qualifies_postproduction(row):
    """Legacy group-only metadata signal used for prioritization, not activation."""
    if row.get("kind") not in ("group", "supergroup", "forum"):
        return False
    title_and_preview = " " + " ".join([
        row.get("name") or "",
        row.get("last_message_preview") or "",
    ]).casefold() + " "
    topic_kinds = {postproduction_topic_kind(str(x))
                   for x in row.get("topic_titles") or []}
    topic_kinds.discard(None)
    return bool(
        topic_kinds.intersection(POSTPRODUCTION_SIGNAL_KINDS)
        or any(word in title_and_preview for word in POSTPRODUCTION_KEYWORDS)
    )
