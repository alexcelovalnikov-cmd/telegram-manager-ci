from pathlib import Path


def test_managed_entity_ops_are_in_reconciliation_allowlist():
    text=Path('tm_api/v24/engine.py').read_text()
    assert "'create_managed_entity', 'update_managed_entity'" in text


def test_managed_auto_apply_is_postproduction_and_rule_guarded():
    text=Path('tm_api/v24/engine.py').read_text()
    assert 'managed_reconciliation_postproduction_only' in text
    assert 'active_workspace_rule_required' in text
    assert 'workspace_rule_reference_required' in text
    assert "body->>'decision_key'" in text


def test_postproduction_contract_drops_sections():
    text=Path('tm_api/v32/reconciliation.py').read_text()
    assert 'Use parent_id plus kind; sections are not part of the product.' in text
