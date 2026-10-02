from pathlib import Path


def test_topic_routing_checks_root_and_general_fallback():
    text=Path('worker/telegram_supabase.py').read_text()
    assert "candidates.append(getattr(message,'id',None))" in text
    assert "postproduction_topic_kind(title)=='general'" in text


def test_backfill_revision_bump_retries_v71_markers_once():
    text=Path('worker/telegram_supabase.py').read_text()
    assert "dynamic_backfill_revision" in text
    assert "row[0] != '2'" in text
    assert "DELETE FROM dynamic_backfill_done" in text


def test_sound_and_delivery_are_supported():
    text=Path('worker/tm_source_discovery.py').read_text()
    assert '"звук": "sound"' in text
    assert '"экспорт": "delivery"' in text
