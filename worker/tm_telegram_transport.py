"""Server-side Telegram transport selection."""
import os


def telethon_proxy():
    mode = os.environ.get("TM_TELEGRAM_PROXY_MODE", "").strip().lower()
    if not mode:
        return None
    if mode == "tor":
        import socks
        return (socks.SOCKS5, "127.0.0.1", 9050)
    raise RuntimeError("Unsupported Telegram proxy mode")
