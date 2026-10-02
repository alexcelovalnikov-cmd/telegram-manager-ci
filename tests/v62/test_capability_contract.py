from pathlib import Path


def test_contract_documents_postproduction_aliases():
    text = Path("tm_api/service.py").read_text()
    assert "Цветокор" in text
    assert "Монтажи" in text
    assert "General-only forums" in text
