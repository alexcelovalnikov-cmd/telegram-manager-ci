import os
import sys
from pathlib import Path

sys.path.insert(0,str(Path("worker").resolve()))
import tm_extract


def test_cloud_vision_is_explicitly_opt_in(monkeypatch):
    monkeypatch.delenv('TM_OPENAI_VISION_ENABLED', raising=False)
    monkeypatch.delenv('TM_OPENAI_VISION_API_KEY', raising=False)
    assert tm_extract._cloud_vision_enabled() is False


def test_cloud_vision_model_is_allowlisted(monkeypatch):
    monkeypatch.setenv('TM_OPENAI_VISION_MODEL','gpt-5.6-luna')
    assert tm_extract._cloud_vision_model() == 'gpt-5.6-luna'
    monkeypatch.setenv('TM_OPENAI_VISION_MODEL','arbitrary-remote-model')
    try:
        tm_extract._cloud_vision_model()
    except tm_extract.MediaError as exc:
        assert str(exc) == 'cloud_vision_model_not_allowlisted'
    else:
        raise AssertionError('unallowlisted cloud model accepted')


def test_response_text_reads_raw_responses_api_shape():
    payload={'output':[{'type':'message','content':[{'type':'output_text','text':'{"visible_text":"","description":"x","uncertainties":[]}'}]}]}
    assert tm_extract._response_text(payload).startswith('{"visible_text"')

def test_cloud_request_contract_is_bounded_and_non_storing():
    text=Path('worker/tm_extract.py').read_text()
    assert "https://api.openai.com/v1/responses" in text
    assert "'store':False" in text
    assert "'detail':'low'" in text
    assert "thumbnail((1600,1600))" in text
    assert "cloud_vision_preview_too_large" in text
    assert "ProxyHandler({})" in text
    assert "NoRedirect()" in text


def test_only_dedicated_cloud_secret_reaches_extractor():
    text=Path('worker/telegram_supabase.py').read_text()
    section=text[text.index("safe_env="):text.index("env.update",text.index("safe_env="))]
    assert 'TM_OPENAI_VISION_API_KEY' in section
    assert 'TM_OPENAI_VISION_ENABLED' in section
    assert 'TM_OPENAI_VISION_MODEL' in section
    assert 'OPENAI_API_KEY' not in section.replace('TM_OPENAI_VISION_API_KEY','')
    assert 'TELEGRAM_API_HASH' not in section


def test_processor_revision_retries_old_partial_images_once():
    text=Path('worker/telegram_supabase.py').read_text()
    assert "MEDIA_PROCESSOR_REVISION = " in text
    assert "offer(meta, MEDIA_PROCESSOR_REVISION)" in text



def test_tmctl_keeps_cloud_secret_out_of_worker_and_login_env():
    text=Path('deploy/ops/tmctl-root').read_text()
    assert "cloud vision settings belong in separate staged vision.env" in text
    assert 'install -m 600 -o root -g root "$WORKER_STAGE/vision.env" "$WORKER_ROOT/vision.env"' in text
    start=text[text.index('worker_start_image()'):text.index('worker_stop()',text.index('worker_start_image()'))]
    login=text[text.index('worker_login_start()'):text.index('worker_login_status()',text.index('worker_login_start()'))]
    assert 'vision_env=(--env-file "$WORKER_ROOT/vision.env")' in start
    assert 'vision.env' not in login
