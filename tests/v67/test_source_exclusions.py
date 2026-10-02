from pathlib import Path


def test_source_exclusion_rule_is_applied_to_both_selection_surfaces():
    text=Path('tm_api/v25/reading.py').read_text()
    assert 'source_selection.excluded_names' in text
    assert 'chat_catalog' in text
    assert 'dialog_candidates' in text
    assert 'casefold()' in text


def test_contract_documents_exclusion_without_deleting_existing_membership():
    text=Path('tm_api/service.py').read_text()
    assert 'source_exclusions' in text
    assert 'not silently deleted' in text
