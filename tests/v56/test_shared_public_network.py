from pathlib import Path


def test_compose_reuses_existing_user_defined_public_network():
    text = Path("deploy/v31/compose.production.yaml").read_text()
    assert 'name: "${TM_PUBLIC_NETWORK:-telegram-manager-v53-production_default}"' in text
    assert "external: true" in text
    assert "name: bridge" not in text
    assert "TM_DATABASE_NETWORK" in text
