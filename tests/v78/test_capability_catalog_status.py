from pathlib import Path


def test_runtime_exposes_capability_catalog_drift():
    text = Path("tm_api/service.py").read_text()
    assert "async def _capability_catalog_status" in text
    assert '"runtime_version":VERSION' in text
    assert '"catalog_version":catalog_version' in text
    assert '"in_sync":catalog_version==VERSION' in text
    assert '"capability_catalog": capability_catalog' in text


def test_client_contract_exposes_capability_catalog_status():
    text = Path("tm_api/service.py").read_text()
    assert "result['capability_catalog_status']=await self._capability_catalog_status()" in text
    assert "'capability_catalog':'get_system_status and get_client_contract expose runtime vs capability-catalog version drift." in text


def test_root_readme_does_not_hardcode_old_production_baseline():
    text = Path("README.md").read_text()
    assert "Current production release: **V40**" not in text
    assert "The repository may be ahead of production." in text
    assert "system user: `telegram-manager`" in text
    assert "SSH alias: `telegram-server`" in text
