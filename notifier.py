"""Фонові сповіщення: перед відключенням і при зміні графіка."""
import asyncio
import hashlib
import logging
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError

import db
import keyboards as kb
from config import TZ
from providers import get_schedule
from render import hm, now_h

log = logging.getLogger(__name__)


def _hash(slots) -> str:
    return hashlib.md5(repr([(s.start, s.end, s.kind) for s in slots or []]).encode()).hexdigest()


async def _send(bot: Bot, uid: int, text: str):
    try:
        await bot.send_message(uid, text, reply_markup=kb.main_menu())
    except TelegramForbiddenError:
        await db.set_user(uid, notify_on=0)  # заблокував бота
    except Exception as e:
        log.warning("send %s failed: %r", uid, e)


async def tick(bot: Bot):
    sch = await get_schedule(force=True)
    today = datetime.now(TZ).date()
    dates = {"today": today, "tomorrow": today + timedelta(days=1)}
    users = await db.users_with_group()
    t = now_h()

    # 1) зміна графіка
    changed: dict[str, list[str]] = {}
    for day, d in dates.items():
        for grp, slots in sch.get(day, {}).items():
            key, h = f"hash:{d}:{grp}", _hash(slots)
            old = await db.kv_get(key)
            if old != h:
                await db.kv_set(key, h)
                if old is not None:
                    changed.setdefault(grp, []).append("сьогодні" if day == "today" else "завтра")

    for u in users:
        if not u["notify_on"]:
            continue
        if u["grp"] in changed:
            await _send(bot, u["id"], f"⚠️ Графік для групи <b>{u['grp']}</b> змінився ({', '.join(changed[u['grp']])})")

        # 2) попередження перед відключенням
        for s in sch.get("today", {}).get(u["grp"], []):
            if s.kind != "off":
                continue
            left = (s.start - t) * 60
            if 0 <= left <= u["notify_min"] and await db.mark_sent(f"warn:{today}:{u['id']}:{s.start}"):
                await _send(bot, u["id"], f"🔔 Через {int(left)} хв вимкнуть світло ({hm(s.start)}–{hm(s.end)}). Заряджай павербанк!")


async def loop(bot: Bot, every=60):
    while True:
        try:
            await tick(bot)
        except Exception:
            log.exception("notifier tick failed")
        await asyncio.sleep(every)
