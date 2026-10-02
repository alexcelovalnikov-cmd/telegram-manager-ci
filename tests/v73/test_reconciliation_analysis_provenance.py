from pathlib import Path


def test_workspace_provenance_accepts_reconciliation_binding_fields():
    text=Path('tm_api/v25/repository.py').read_text()
    assert "'classification','finding_key','rule_key'" in text
    assert 'incomplete_reconciliation_analysis_binding' in text
    assert 'invalid_analysis_finding_key' in text
    assert 'invalid_analysis_rule_key' in text


def test_apply_guard_remains_deterministic_and_rule_bound():
    text=Path('tm_api/v24/engine.py').read_text()
    assert "analysis.get('classification')!='deterministic'" in text
    assert 'workspace_rule_reference_required' in text
    assert 'managed_reconciliation_postproduction_only' in text
