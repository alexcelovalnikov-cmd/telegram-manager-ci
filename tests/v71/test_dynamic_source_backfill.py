from pathlib import Path


def test_dynamic_backfill_is_postselection_and_bounded():
    text=Path('worker/telegram_supabase.py').read_text()
    assert 'async def hydrate_postproduction_sources' in text
    assert 'message_limit=40' in text
    assert "resolution_status')!='resolved'" in text
    assert "rules_profile')!='postproduction'" in text
    assert 'self.allowed_message(cid,message)' in text


def test_dynamic_backfill_has_durable_completion_marker():
    text=Path('worker/telegram_supabase.py').read_text()
    assert 'dynamic_backfill_done' in text
    assert 'PRIMARY KEY(target_id,request_revision)' in text
    assert 'mark_dynamic_backfill_done' in text
    assert 'explicit_backfill_done' in text
    assert 'mark_explicit_backfill_done' in text


def test_capability_contract_documents_guarded_bounded_discovery():
    text=Path('tm_api/service.py').read_text()
    assert 'postproduction_source_backfill' in text
    assert 'After the exclusion guard' in text
    assert 'at most 12 non-excluded recent private candidates' in text
    assert 'candidate media is never processed' in text
