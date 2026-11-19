"""Юзербот: читає Telegram і складає події в БД (бот їх розсилає).

- CHANNELS — канали-агрегатори. Пости -> parse_channel -> alerts (аварійні по групах, загальні).
  Коментарі до їх постів (прив'язана група обговорення) -> звіти людей.
- CHATS — додаткові чати, де пишуть люди: «1.1 нема світла», «-свет», «3.2 +» -> parse_user.

Запуск: python userbot.py (перший раз попросить номер і код). Використовуй ОКРЕМИЙ акаунт.
"""
import asyncio
import hashlib
import logging
import os
import re
import sys
from datetime import datetime

from telethon import TelegramClient, events
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.tl.types import ChatInviteAlready, PeerUser

import db
from config import API_HASH, API_ID, CHANNELS, CHATS, TZ
from parse import dedup_text, parse_cek_schedule, parse_channel, parse_user, ukey

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("userbot")


def alert_key(a, raw: str) -> str:
    day = datetime.now(TZ).strftime("%Y-%m-%d")
    if a.kind == "general":
        return f"general:{day}:{dedup_text(raw)}"
    # «off» з однаковим «до» — один раз; «on» без часу — не частіше раз на 2 год
    bucket = a.until or f"h{datetime.now(TZ).hour // 2}"
    return f"{a.provider}:{a.kind}:{a.grp}:{day}:{bucket}"


async def handle_post(raw: str):
    """Пост каналу -> графік ЦЕК у БД і/або alerts з антиспамом."""
    now = datetime.now(TZ)
    if cek := parse_cek_schedule(raw, now.date()):
        day, groups = cek
        await db.save_cek_schedule(day.isoformat(), groups, now.strftime("%H:%M %d.%m"))
        log.info("CEK schedule %s: %d груп", day, len(groups))
    for a in parse_channel(raw):
        if a.kind == "general":
            # загальних — не частіше раз на 30 хв (крім «скасовано»: це важливо)
            if "скасован" not in raw.lower() and await db.last_alert("general", "*", 30, a.provider):
                continue
        elif a.kind == "off" and a.until:
            prev = await db.last_alert("off", a.grp, 12 * 60, a.provider)
            if prev and prev["until"] and ukey(a.until) > ukey(prev["until"]):
                a.text = a.text.replace("Аварійне/екстрене відключення", "Відключення продовжено")
        if await db.add_alert(alert_key(a, raw), a.kind, a.grp, a.until, a.text, a.provider):
            log.info("ALERT %s %s %s %s", a.provider, a.kind, a.grp, a.until)


async def resolve(client, names):
    """Канали + їх групи обговорень. Вступаємо, щоб отримувати апдейти."""
    channels, discussions = [], []
    for name in names:
        try:
            ent = await client.get_entity(name)
            await client(JoinChannelRequest(ent))
            channels.append(ent)
            full = await client(GetFullChannelRequest(ent))
            linked = full.full_chat.linked_chat_id
            disc = next((c for c in full.chats if c.id == linked), None) if linked else None
            if disc:
                await client(JoinChannelRequest(disc))
                discussions.append(disc)
            log.info("канал %s: обговорення %s", name, getattr(disc, "title", "немає"))
        except Exception as e:
            log.warning("не вдалось підключити %s: %r", name, e)
        await asyncio.sleep(3)  # не вступаємо пачкою — менше шансів на флуд-бан
    return channels, discussions


async def get_chat(client, ref: str):
    """@username, числовий ID (-100…) або інвайт-посилання t.me/+xxxx / t.me/joinchat/xxxx."""
    try:
        if ref.lstrip("-").isdigit():
            return await client.get_entity(int(ref))
        if m := re.search(r"t\.me/(?:\+|joinchat/)([\w-]+)", ref):
            info = await client(CheckChatInviteRequest(m.group(1)))
            if isinstance(info, ChatInviteAlready):
                return info.chat
            await asyncio.sleep(3)
            return (await client(ImportChatInviteRequest(m.group(1)))).chats[0]
        return await client.get_entity(ref)
    except Exception as e:
        log.warning("не вдалось підключити чат %s: %r", ref, e)
        return None


async def main():
    if not (API_ID and API_HASH and (CHANNELS or CHATS)):
        raise SystemExit("Заповни API_ID, API_HASH і CHANNELS/CHATS у .env")
    await db.init()
    client = TelegramClient(os.getenv("SESSION_PATH", "userbot"), API_ID, API_HASH)
    await client.start()

    await client.get_dialogs()  # щоб чати без @username знаходились по ID
    if "--list" in sys.argv:
        async for d in client.iter_dialogs():
            if d.is_group or d.is_channel:
                print(f"{d.id:>16}  {d.name}")
        return

    channels, discussions = await resolve(client, CHANNELS)
    user_chats = discussions
    for c in CHATS:
        if ent := await get_chat(client, c):
            if all(ent.id != x.id for x in user_chats):
                user_chats.append(ent)

    async def on_post(event):
        await handle_post(event.raw_text or "")

    async def on_user_msg(event):
        msg = event.message
        if msg.fwd_from or not isinstance(msg.from_id, PeerUser):  # копії постів каналу, адміни від імені каналу
            return
        res = parse_user(event.raw_text or "")
        if not res:
            return
        chat = getattr(event.chat, "title", "") or ""
        # автора НЕ зберігаємо — лише солоний хеш, щоб рахувати різних людей
        who = hashlib.sha256(f"{API_HASH}:{msg.from_id.user_id}".encode()).hexdigest()[:12]
        await db.add_chat_msg(
            chat, event.raw_text, res["district"], res["event"], res["groups"], res["street"], who, res["provider"]
        )
        log.info("USER %s %s %s | %s", res["event"], res["groups"], res["street"] or "", event.raw_text[:60])

    if channels:
        client.add_event_handler(on_post, events.NewMessage(chats=channels))
    if user_chats:
        client.add_event_handler(on_user_msg, events.NewMessage(chats=user_chats))
    log.info("Слухаю: %d каналів, %d чатів людей", len(channels), len(user_chats))
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
