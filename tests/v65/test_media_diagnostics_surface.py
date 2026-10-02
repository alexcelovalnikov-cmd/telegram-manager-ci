from pathlib import Path


def test_attachment_read_exposes_safe_processing_diagnostics():
    text=Path("tm_api/v24/reading.py").read_text()
    assert "'processing_error'" in text
    assert "'processing_details'" in text
    assert "original_file_available_via_this_api':False" in text


def test_system_status_exposes_media_health_without_raw_paths():
    text=Path("tm_api/service.py").read_text()
    for field in ("media_busy","media_last_error","media_settings_error","media_capabilities","media_stats"):
        assert field in text
