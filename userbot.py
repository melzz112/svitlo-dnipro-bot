"""Юзербот: слухає публічні чати Дніпра і складає релевантні повідомлення в БД.
Запуск окремим процесом: python userbot.py (перший раз попросить номер і код).
Використовуй ОКРЕМИЙ акаунт, не основний."""
import asyncio
import logging
import os

from telethon import TelegramClient, events

import db
from config import API_HASH, API_ID, CHATS
from parse import classify

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("userbot")


async def main():
    if not (API_ID and API_HASH and CHATS):
        raise SystemExit("Заповни API_ID, API_HASH і CHATS у .env")
    await db.init()
    client = TelegramClient(os.getenv("SESSION_PATH", "userbot"), API_ID, API_HASH)

    @client.on(events.NewMessage(chats=CHATS))
    async def on_msg(event):
        text = event.raw_text or ""
        res = classify(text)
        if not res:
            return
        ev, district = res
        chat = getattr(event.chat, "title", "") or ""
        await db.add_chat_msg(chat, text, district, ev)  # автора НЕ зберігаємо
        log.info("%s | %s | %s", ev, district, text[:80])

    await client.start()
    log.info("Слухаю %d чатів", len(CHATS))
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
