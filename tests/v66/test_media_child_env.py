from pathlib import Path


def test_media_child_receives_only_required_runtime_path_settings():
    text=Path("worker/telegram_supabase.py").read_text()
    for key in ("TM_MEDIA_BACKEND","TM_OCR_LANG","TM_MODELS_HOME","TM_MODELS_DIR","TM_AUDIO_PYTHON"):
        assert key in text
    assert "TELEGRAM_API_HASH" not in text[text.index("safe_env="):text.index("env.update",text.index("safe_env="))]


def test_media_processor_revision_requeues_old_failed_jobs():
    queue=Path("worker/tm_content_queue.py").read_text()
    collector=Path("worker/telegram_supabase.py").read_text()
    assert "processor_revision" in queue
    assert "MEDIA_PROCESSOR_REVISION" in collector
    assert "offer(meta, MEDIA_PROCESSOR_REVISION)" in collector
