from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = (ROOT / 'whatsapp' / 'package.json').read_text()
DOCKER = (ROOT / 'Dockerfile.whatsapp').read_text()
PATCH = (ROOT / 'whatsapp' / 'patches' / '@whiskeysockets+baileys+7.0.0-rc14.patch').read_text()

def test_whatsapp_pairing_vendors_companion_refresh_fix():
    assert '"@whiskeysockets/baileys": "7.0.0-rc14"' in PKG
    assert '"postinstall": "patch-package --error-on-fail"' in PKG
    assert 'COPY whatsapp/patches ./patches' in DOCKER
    assert 'companion_reg_refresh' in PATCH
    assert 'makePairingQRRenderer' in PATCH
    assert 'authState.creds.me?.id' in PATCH
