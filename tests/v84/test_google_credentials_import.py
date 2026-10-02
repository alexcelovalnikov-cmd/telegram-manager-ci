from pathlib import Path


def test_tmctl_has_guarded_google_credentials_import():
    text=Path('deploy/ops/tmctl-root').read_text()
    assert 'google_credentials_import()' in text
    assert '/opt/telegram-manager/staging/google-sheets-service-account.json' in text
    assert '/opt/telegram-manager/secrets/google-sheets-service-account.json' in text
    assert 'staged Google credential must have mode 600' in text
    assert "value.get('type')!='service_account'" in text
    assert "value.get('token_uri')!='https://oauth2.googleapis.com/token'" in text
    assert 'GOOGLE_CREDENTIALS_IMPORT_OK' in text


def test_import_never_prints_private_key_or_json():
    text=Path('deploy/ops/tmctl-root').read_text()
    block=text[text.index('google_credentials_import()'):text.index('backup_check()',text.index('google_credentials_import()'))]
    assert 'private_key' in block
    assert 'cat "$stage"' not in block
    assert 'echo "$stage"' not in block
    assert 'print(value[\'private_key\'])' not in block
    assert 'client_email=' in block


def test_import_restarts_only_api_not_calendar_or_worker():
    text=Path('deploy/ops/tmctl-root').read_text()
    block=text[text.index('google_credentials_import()'):text.index('backup_check()',text.index('google_credentials_import()'))]
    assert 'restart api' in block
    assert 'restart calendar' not in block
    assert 'worker_start' not in block
