from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVICE = (ROOT / "tm_api" / "service.py").read_text()
REGISTER = (ROOT / "tm_api" / "v24" / "register.py").read_text()

WHATSAPP_READS = (
    "get_whatsapp_status",
    "list_whatsapp_chats",
    "search_whatsapp_messages",
    "get_whatsapp_message_context",
)


def test_v105_contract_describes_self_hosted_whatsapp_runtime():
    assert "A self-hosted read-only WhatsApp linked-device source is available" in SERVICE
    assert "No WhatsApp source is connected" not in SERVICE
    assert "Pairing/connection state is runtime data from get_whatsapp_status" in SERVICE


def test_v24_register_declares_all_whatsapp_read_tools():
    for name in WHATSAPP_READS:
        assert f"'{name}'" in REGISTER
        assert f"async def {name}" in REGISTER
