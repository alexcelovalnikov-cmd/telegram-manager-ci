from worker.tm_source_discovery import qualifies_postproduction, postproduction_topic_allowed, postproduction_topic_kind


def forum(*topics):
    return {"kind": "forum", "name": "Production room", "last_message_preview": "", "topic_titles": list(topics)}


def test_russian_color_and_editing_topics_are_postproduction_signals():
    assert qualifies_postproduction(forum("General", "Цветокор")) is True
    assert qualifies_postproduction(forum("Общие вопросы", "ЦК")) is True
    assert qualifies_postproduction(forum("General", "Монтажи")) is True


def test_general_alone_is_not_enough_to_auto_select_forum():
    assert qualifies_postproduction(forum("General", "Болталка")) is False


def test_allowed_topics_cover_general_and_postproduction_aliases():
    assert postproduction_topic_kind("Общие вопросы") == "general"
    assert postproduction_topic_kind("Цветокор") == "color"
    assert postproduction_topic_kind("Монтажи") == "editing"
    assert postproduction_topic_kind("Звук") == "sound"
    assert postproduction_topic_kind("Экспорт") == "delivery"
    assert postproduction_topic_allowed("CG production // First video") is True
    assert postproduction_topic_allowed("SMM") is False
