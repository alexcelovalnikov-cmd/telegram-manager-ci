from pathlib import Path


def test_reconciliation_context_includes_managed_entities():
    text = Path("tm_api/v32/reconciliation.py").read_text()
    assert "'managed_entities': managed_entities" in text
    assert "'managed_entities_truncated': managed_entities_truncated" in text
    assert "e.status='active'" in text
    assert "w.status='active'" in text
    assert "e.data->>'parent_id' AS parent_id" in text
    assert "e.parent_id" not in text


def test_managed_snapshot_is_bounded():
    text = Path("tm_api/v32/reconciliation.py").read_text()
    assert "min(state_limit,100)+1" in text
    assert "managed_entities[:min(state_limit,100)]" in text


def test_postproduction_contract_requires_comparison_before_create():
    text = Path("tm_api/v32/reconciliation.py").read_text()
    assert "Compare evidence against managed_entities in this context before creating anything." in text


def test_client_contract_exposes_managed_state_capability():
    text = Path("tm_api/service.py").read_text()
    assert "'reconciliation_managed_state'" in text
