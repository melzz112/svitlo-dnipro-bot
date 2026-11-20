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
from parse import dedup_text, parse_channel, parse_schedule_posts, parse_user, ukey

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("userbot")


def alert_key(a, raw: str) -> str:
    day = datetime.now(TZ).strftime("%Y-%m-%d")
    if a.kind == "general":
        return f"general:{day}:{dedup_text(raw)}"
    # «off» з однаковим «до» — один раз; «on» без часу — не частіше раз на 2 год
    bucket = a.until or f"h{datetime.now(TZ).hour // 2}"
    return f"{a.provider}:{a.kind}:{a.grp}:{day}:{bucket}"


MIN_SHIFT_MIN = 30  # «продовжено до 10:16» після «до 10:03» — не новина; пушимо зсув від 30 хв
_post_lock = asyncio.Lock()  # два канали постять одне й те саме в ту ж секунду -> без дублів


def until_shift(new: str, old: str) -> int:
    """Різниця в хвилинах між «HH:MM [DD.MM]». Якщо в одного немає дати — порівнюємо лише час."""
    def parse(u):
        hm, _, dm = u.partition(" ")
        h, m = map(int, hm.split(":"))
        d, _, mo = dm.partition(".")
        return h * 60 + m, (int(mo), int(d)) if dm else None

    (tn, dn), (to, do) = parse(new), parse(old)
    if dn and do:
        from datetime import date

        days = (date(2000, *dn) - date(2000, *do)).days
        return days * 1440 + tn - to
    d = (tn - to) % 1440
    return d - 1440 if d > 720 else d


async def handle_post(raw: str):
    async with _post_lock:
        await _handle_post(raw)


async def save_schedules(raw: str, when: datetime) -> int:
    """Графіки ДТЕК/ЦЕК з поста -> БД. when — час поста (для постів без дати = «сьогодні» на момент поста)."""
    n = 0
    for prov, day, groups in parse_schedule_posts(raw, when.date()):
        await db.save_channel_schedule(prov, day.isoformat(), groups, when.strftime("%H:%M %d.%m"))
        log.info("schedule %s %s: %d груп", prov, day, len(groups))
        n += 1
    return n


async def backfill(client, channels, limit=40):
    """При старті: дочитуємо останні пости, щоб графік на сьогодні/завтра був одразу (лише графіки, без пушів)."""
    for ch in channels:
        try:
            msgs = [m async for m in client.iter_messages(ch, limit=limit) if m.raw_text]
        except Exception as e:
            log.warning("backfill %s failed: %r", getattr(ch, "username", ch), e)
            continue
        for m in reversed(msgs):  # від старих до нових — новіші коригування перекривають
            await save_schedules(m.raw_text, m.date.astimezone(TZ))


async def _handle_post(raw: str):
    """Пост каналу -> графіки в БД і/або alerts з антиспамом."""
    await save_schedules(raw, datetime.now(TZ))
    for a in parse_channel(raw):
        if a.kind == "general":
            # загальних — не частіше раз на 30 хв (крім «скасовано»: це важливо)
            if "скасован" not in raw.lower() and await db.last_alert("general", "*", 30, a.provider):
                continue
        elif a.kind == "off" and a.until:
            prev = await db.last_alert("off", a.grp, 12 * 60, a.provider)
            if prev and prev["until"]:
                shift = until_shift(a.until, prev["until"])
                if abs(shift) < MIN_SHIFT_MIN:
                    continue  # дрібне уточнення часу — не смикаємо людей
                old_label = "Аварійне/екстрене відключення"
                a.text = a.text.replace(old_label, "Відключення продовжено" if shift > 0 else "Відключення скорочено")
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
    await backfill(client, channels)
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
        reply_raw = None
        if msg.reply_to and getattr(msg.reply_to, "reply_to_msg_id", None):
            try:  # «+» / «у меня тоже» / «а нам не дали» — сенс і група з повідомлення, на яке відповіли
                parent = await event.get_reply_message()
                if parent and isinstance(parent.from_id, PeerUser) and not parent.fwd_from:
                    reply_raw = parent.raw_text
            except Exception as e:
                log.debug("reply fetch failed: %r", e)
        res = parse_user(event.raw_text or "", reply_raw)
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
