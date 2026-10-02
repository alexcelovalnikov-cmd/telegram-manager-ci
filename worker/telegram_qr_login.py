#!/usr/bin/env python3
"""One-time Telegram QR authorization for the fixed server worker session."""
import asyncio
import os
from pathlib import Path

import qrcode
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from rr_common import load_env
from tm_telegram_transport import telethon_proxy


async def main():
    env = load_env()
    session = Path(os.environ.get("TM_TELEGRAM_SESSION", "/state/telegram_helper.session"))
    qr_path = Path(os.environ.get("TM_LOGIN_QR_PATH", "/state/login-qr.png"))
    session.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(session), int(env["TELEGRAM_API_ID"]), env["TELEGRAM_API_HASH"], proxy=telethon_proxy(), timeout=15, connection_retries=3)
    await client.connect()
    try:
        if await client.is_user_authorized():
            print("SESSION_ALREADY_AUTHORIZED", flush=True)
            return 0

        while True:
            login = await client.qr_login()
            image = qrcode.make(login.url)
            temp = qr_path.with_suffix(".tmp.png")
            image.save(temp)
            os.chmod(temp, 0o600)
            temp.replace(qr_path)
            print("QR_READY", flush=True)
            try:
                await login.wait(timeout=90)
            except asyncio.TimeoutError:
                continue
            except SessionPasswordNeededError:
                print("QR_2FA_REQUIRED", flush=True)
                return 4
            if await client.is_user_authorized():
                try:
                    qr_path.unlink()
                except FileNotFoundError:
                    pass
                print("QR_LOGIN_OK", flush=True)
                return 0
    finally:
        await client.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
