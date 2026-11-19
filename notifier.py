"""Фонові сповіщення: перед відключенням і при зміні графіка."""
import asyncio
import logging
import time
from html import escape
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError

import db
import keyboards as kb
from config import CROWD_MIN, TZ
import providers
from providers import get_schedule
from render import day_text, hm, now_h

log = logging.getLogger(__name__)


def _slots_str(slots) -> str:
    """«06:00–10:00, 16:30–20:30» — і для порівняння, і для «було/стало»."""
    parts = [f"{'~' if s.kind == 'maybe' else ''}{hm(s.start)}–{hm(s.end)}" for s in sorted(slots or [], key=lambda s: s.start)]
    return ", ".join(parts) or "без відключень"


async def _send(bot: Bot, uid: int, text: str):
    try:
        await bot.send_message(uid, text, reply_markup=kb.main_menu())
    except TelegramForbiddenError:
        await db.set_user(uid, notify_on=0)  # заблокував бота
    except Exception as e:
        log.warning("send %s failed: %r", uid, e)


async def tick(bot: Bot):
    sch = await get_schedule()
    today = datetime.now(TZ).date()
    dates = {"today": today, "tomorrow": today + timedelta(days=1)}
    users = await db.users_with_group()
    t = now_h()

    # 1) графік оновився / з'явився графік на завтра -> шлемо новий графік
    initialized = await db.kv_get("sched_init") is not None  # перший прохід після деплою — без пушів
    changes: dict[str, list[str]] = {}  # grp -> готові тексти
    for day, d in dates.items():
        for grp, slots in sch.get(day, {}).items():
            key, new = f"sched:{d}:{grp}", _slots_str(slots)
            old = await db.kv_get(key)
            if old == new:
                continue
            await db.kv_set(key, new)
            if not initialized:
                continue
            title = "Сьогодні" if day == "today" else "Завтра"
            if old is None:
                head = f"📅 <b>З'явився графік на {'завтра' if day == 'tomorrow' else 'сьогодні'}</b>"
            else:
                head = f"🔄 <b>Графік на {title.lower()} оновився</b>\nБуло: {old}\nСтало: <b>{new}</b>"
            text = f"{head}\n\n{day_text(grp, slots, title)}"
            if providers.last_update:
                text += f"\n\n🕒 Оновлено ДТЕК: {providers.last_update}"
            changes.setdefault(grp, []).append(text)
    if not initialized and any(sch.get(d) for d in dates):
        await db.kv_set("sched_init", "1")

    for u in users:
        if not u["notify_on"]:
            continue
        for text in changes.get(u["grp"], []):
            await _send(bot, u["id"], text)
            await asyncio.sleep(0.05)

        # 2) попередження перед відключенням
        for s in sch.get("today", {}).get(u["grp"], []):
            if s.kind != "off":
                continue
            left = (s.start - t) * 60
            if 0 <= left <= u["notify_min"] and await db.mark_sent(f"warn:{today}:{u['id']}:{s.start}"):
                await _send(bot, u["id"], f"🔔 Через {int(left)} хв вимкнуть світло ({hm(s.start)}–{hm(s.end)}). Заряджай павербанк!")


async def send_alerts(bot: Bot):
    """Події з каналів (юзербот кладе в alerts) -> людям потрібної групи, загальні — всім."""
    alerts = await db.pending_alerts()
    if not alerts:
        return
    users = [u for u in await db.users_with_group() if u["notify_on"]]
    for a in alerts:
        await db.mark_alert_sent(a["key"])
        for u in users:
            if a["grp"] == "*" or a["grp"] == u["grp"]:
                await _send(bot, u["id"], a["text"] + "\n\n<i>За даними Telegram-каналів</i>")
                await asyncio.sleep(0.05)  # ліміт Telegram ~30 повідомлень/с


def _now_off_by_schedule(sch, grp) -> bool:
    t = now_h()
    return any(s.kind == "off" and s.start <= t < s.end for s in sch.get("today", {}).get(grp, []))


async def crowd_check(bot: Bot):
    """Канал пише «відновлення до 00:00», а люди в чатах вже «дали» -> повідомляємо групу.
    Пуш лише коли стан групи змінюється (нема -> є або навпаки) і ≥ CROWD_MIN різних людей за 15 хв."""
    data = await db.crowd(15)
    if not data:
        return
    sch = await get_schedule()
    users = None
    for grp, c in data.items():
        n_on, n_off = len(c["on"]), len(c["off"])
        if n_on >= CROWD_MIN and n_on >= 2 * n_off:
            new = "on"
        elif n_off >= CROWD_MIN and n_off >= 2 * n_on:
            new = "off"
        else:
            continue
        prev = await db.kv_get(f"crowd:{grp}")
        if prev is None:  # ще не знаємо — беремо з каналів/графіка
            a = await db.latest_alert(grp, hours=12)
            prev = "off" if (a and a["kind"] == "off" and a["grp"] == grp) or _now_off_by_schedule(sch, grp) else "on"
        if new == prev:
            continue
        last_push = int(await db.kv_get(f"crowd_ts:{grp}") or 0)
        if time.time() - last_push < 20 * 60:  # не смикаємо людей частіше раз на 20 хв
            continue
        await db.kv_set(f"crowd:{grp}", new)
        await db.kv_set(f"crowd_ts:{grp}", str(int(time.time())))

        if new == "on":
            head = "💡 <b>Люди пишуть: у групі {g} {what}</b>".format(
                g=grp, what="почали вмикати світло" if n_off else "дали світло"
            )
            n = n_on
        else:
            head, n = f"🕯 <b>Люди пишуть: у групі {grp} вимкнули світло</b>", n_off
        text = f"{head}\n👥 {n} людей за 15 хв"
        if places := [p for p, _ in c["places"].most_common(4)]:
            text += "\n📍 " + ", ".join(escape(p) for p in places)
        text += "\n\n<i>Неофіційно — за повідомленнями в чатах</i>"

        users = users if users is not None else [u for u in await db.users_with_group() if u["notify_on"]]
        for u in users:
            if u["grp"] == grp:
                await _send(bot, u["id"], text)
                await asyncio.sleep(0.05)
        log.info("crowd %s -> %s (%d on / %d off)", grp, new, n_on, n_off)


async def loop(bot: Bot, every=60):
    n = 0
    while True:
        try:
            await send_alerts(bot)
            await crowd_check(bot)
            if n % 4 == 0:  # графік — раз на хвилину, аварії — кожні 15 с
                await tick(bot)
        except Exception:
            log.exception("notifier tick failed")
        n += 1
        await asyncio.sleep(every // 4)
