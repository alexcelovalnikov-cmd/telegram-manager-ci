from pathlib import Path

from worker.tm_source_discovery import (
    DIALOG_DISCOVERY_PUBLISH_LIMIT,
    DIALOG_DISCOVERY_SCAN_LIMIT,
    publish_candidates,
)


def test_discovery_scans_deeper_but_publishes_bounded_state():
    assert DIALOG_DISCOVERY_SCAN_LIMIT == 200
    assert DIALOG_DISCOVERY_PUBLISH_LIMIT == 100


def test_published_candidates_prioritize_group_sources():
    rows=[
        {'chat_id':1,'kind':'private','name':'Person'},
        {'chat_id':2,'kind':'bot','name':'Bot'},
        {'chat_id':3,'kind':'forum','name':'Post forum'},
        {'chat_id':4,'kind':'group','name':'Edit group'},
        {'chat_id':5,'kind':'channel','name':'Channel'},
    ]
    out=publish_candidates(rows,4)
    assert [x['chat_id'] for x in out] == [3,4,1,2]


def test_full_scan_is_bounded_and_guarded_before_dynamic_activation():
    text=Path('worker/telegram_supabase.py').read_text()
    discovery=Path('worker/tm_source_discovery.py').read_text()
    assert 'publish_candidates(rows, DIALOG_DISCOVERY_PUBLISH_LIMIT)' in text
    assert "self.dialog_forum_ids = {int(row['chat_id']) for row in rows if row.get('forum')}" in text
    assert 'excluded_names=self.source_exclusions.get' in text
    assert 'await self.sync_postproduction_sources(max_activations=1)' in text
    assert 'excluded_reason(chat_id, name, excluded_names, excluded_chat_ids)' in discovery
    assert 'message = getattr(dialog, "message", None)' in discovery
    assert discovery.index('excluded_reason(chat_id, name, excluded_names, excluded_chat_ids)') < discovery.index('message = getattr(dialog, "message", None)')
