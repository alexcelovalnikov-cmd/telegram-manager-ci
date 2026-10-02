from pathlib import Path


def test_tmctl_has_guarded_vision_credentials_import():
    text=Path('deploy/ops/tmctl-root').read_text()
    assert 'vision_credentials_import()' in text
    assert '/opt/telegram-manager/staging/vision.env' in text
    assert 'staged vision.env must have mode 600' in text
    assert "allowed={'TM_OPENAI_VISION_ENABLED','TM_OPENAI_VISION_MODEL','TM_OPENAI_VISION_API_KEY'}" in text
    assert 'gpt-5.6-luna' in text
    assert 'VISION_CREDENTIALS_IMPORT_OK' in text


def test_import_never_prints_api_key():
    text=Path('deploy/ops/tmctl-root').read_text()
    block=text[text.index('vision_credentials_import()'):text.index('worker_start_image()',text.index('vision_credentials_import()'))]
    assert 'TM_OPENAI_VISION_API_KEY' in block
    assert "print(values.get('TM_OPENAI_VISION_API_KEY'))" not in block
    assert 'cat "$stage"' not in block


def test_running_worker_is_rolled_to_apply_vision_env():
    text=Path('deploy/ops/tmctl-root').read_text()
    block=text[text.index('vision_credentials_import()'):text.index('worker_start_image()',text.index('vision_credentials_import()'))]
    assert 'worker_start_image "${TM_WORKER_IMAGE:-}"' in block
