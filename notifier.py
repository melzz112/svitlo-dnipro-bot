"""Фонові сповіщення: перед відключенням, при зміні графіка, аварії з каналів, «люди пишуть»."""
import asyncio
import logging
import time
from datetime import datetime
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError

import db
import keyboards as kb
import providers
from config import CROWD_MIN, TZ
from providers import get_schedule
from render import day_text, hm, now_h

log = logging.getLogger(__name__)


def _slots_str(slots) -> str:
    """«06:00–10:00, 16:30–20:30» — і для порівняння, і для «було/стало»."""
    parts = [f"{'~' if s.kind == 'maybe' else ''}{hm(s.start)}–{hm(s.end)}" for s in sorted(slots or [], key=lambda s: s.start)]
    return ", ".join(parts) or "без відключень"


SITE_SOURCES = ("dtek", "yasno", "oua")  # «офіційніші» за Telegram-канали


def _pkey(provider: str, name: str) -> str:
    """Ключі kv: у ДТЕК — старі (без префікса), щоб після деплою не було хибних «оновився»."""
    return name if provider == "dtek" else f"{provider}_{name}"


def _prov(user: dict) -> str:
    return user.get("provider") or "dtek"


def _matches(alert_provider: str, user: dict) -> bool:
    return alert_provider == "*" or alert_provider == _prov(user)


async def _send(bot: Bot, uid: int, text: str):
    try:
        await bot.send_message(uid, text, reply_markup=kb.main_menu())
    except TelegramForbiddenError:
        await db.set_user(uid, notify_on=0)  # заблокував бота
    except Exception as e:
        log.warning("send %s failed: %r", uid, e)


async def tick(bot: Bot):
    today = datetime.now(TZ).date()
    dates = providers.schedule_dates(today)
    users = await db.users_with_group()
    t = now_h()
    initialized = await db.kv_get("sched_init") is not None  # перший прохід після деплою — без пушів

    schedules = await asyncio.gather(*(providers.get_schedule_days(prov, today=today) for prov in db.PROVIDERS))
    for prov, days in zip(db.PROVIDERS, schedules):
        name = db.PROVIDER_NAME[prov]

        # 1) графік оновився / з'явився -> шлемо новий графік
        changes: dict[str, list[str]] = {}  # grp -> готові тексти
        for day, d in dates.items():
            schedule = days[day]
            for grp, slots in schedule.groups.items():
                key, new = f"{_pkey(prov, 'sched')}:{d}:{grp}", _slots_str(slots)
                src_key = f"{_pkey(prov, 'schedsrc')}:{d}:{grp}"
                old = await db.kv_get(key)
                if old == new:
                    continue
                old_src = await db.kv_get(src_key)
                if schedule.source == "channel" and old_src in SITE_SOURCES:
                    # сайт тимчасово недоступний, а в каналах інша (часто старіша) версія — не смикаємо людей
                    # і не перезаписуємо: коли сайт повернеться з тим самим графіком, пуша теж не буде
                    log.info("skip %s %s %s: site->channel fallback (%s vs %s)", prov, d, grp, old, new)
                    continue
                await db.kv_set(key, new)
                await db.kv_set(src_key, schedule.source)
                if not initialized:
                    continue
                title = "Сьогодні" if day == "today" else "Завтра"
                if old is None:
                    head = f"📅 <b>{name}: з'явився графік на {title.lower()}</b>"
                else:
                    head = f"🔄 <b>{name}: графік на {title.lower()} оновився</b>\nБуло: {old}\nСтало: <b>{new}</b>"
                text = f"{head}\n\n{day_text(grp, slots, title)}"
                if label := schedule.label():
                    text += f"\n\n{label}"
                changes.setdefault(grp, []).append(text)
        if not initialized and any(schedule.groups for schedule in days.values()):
            await db.kv_set("sched_init", "1")

        for u in users:
            if not u["notify_on"] or _prov(u) != prov:
                continue
            for text in changes.get(u["grp"], []):
                await _send(bot, u["id"], text)
                await asyncio.sleep(0.05)
            # 2) попередження перед відключенням
            for s in days["today"].groups.get(u["grp"], []):
                if s.kind != "off":
                    continue
                left = (s.start - t) * 60
                if 0 <= left <= u["notify_min"] and await db.mark_sent(f"warn:{today}:{u['id']}:{prov}:{s.start}"):
                    await _send(
                        bot, u["id"], f"🔔 Через {int(left)} хв вимкнуть світло ({hm(s.start)}–{hm(s.end)}). Заряджай павербанк!"
                    )


ON_HOLD_SEC = 5 * 60  # «світло дали» з каналів чекає 5 хв, щоб звірити з чатами


async def _contradicted_by_chats(a: dict) -> bool:
    """Канал каже «не підтверджується / відновлення», а люди в чатах пишуть «нема» -> не шлемо."""
    c = (await db.crowd(30, a.get("provider") or "dtek")).get(a["grp"])
    if not c:
        return False
    n_on, n_off = len(c["on"]), len(c["off"])
    return n_off >= 2 and n_off >= n_on


async def send_alerts(bot: Bot):
    """Події з каналів (юзербот кладе в alerts) -> людям потрібної групи і постачальника, загальні — всім."""
    alerts = await db.pending_alerts()
    if not alerts:
        return
    users = [u for u in await db.users_with_group() if u["notify_on"]]
    for a in alerts:
        if a["kind"] == "on":
            if time.time() - a["ts"] < ON_HOLD_SEC:
                continue  # ще чекаємо, що скажуть люди
            if await _contradicted_by_chats(a):
                await db.mark_alert_sent(a["key"])
                log.info("alert %s dropped: chats say no power", a["key"])
                continue
        await db.mark_alert_sent(a["key"])
        for u in users:
            if _matches(a.get("provider") or "dtek", u) and (a["grp"] == "*" or a["grp"] == u["grp"]):
                await _send(bot, u["id"], a["text"] + "\n\n<i>За даними Telegram-каналів</i>")
                await asyncio.sleep(0.05)  # ліміт Telegram ~30 повідомлень/с


def _now_off_by_schedule(sch, grp) -> bool:
    t = now_h()
    return any(s.kind == "off" and s.start <= t < s.end for s in sch.get("today", {}).get(grp, []))


async def crowd_check(bot: Bot):
    """Канал пише «відновлення до 00:00», а люди в чатах вже «дали» -> повідомляємо групу.
    Пуш лише коли стан групи змінюється (нема -> є або навпаки) і ≥ CROWD_MIN різних людей за 15 хв.
    ЦЕК — лише з повідомлень, де прямо згадано ЦЕК (номери груп ті самі, що в ДТЕК)."""
    users = None
    for prov in db.PROVIDERS:
        data = await db.crowd(15, prov)
        if not data:
            continue
        sch = await get_schedule(provider=prov)
        name = db.PROVIDER_NAME[prov]
        for grp, c in data.items():
            n_on, n_off = len(c["on"]), len(c["off"])
            if n_on >= CROWD_MIN and n_on >= 2 * n_off:
                new = "on"
            elif n_off >= CROWD_MIN and n_off >= 2 * n_on:
                new = "off"
            else:
                continue
            skey = _pkey(prov, "crowd")
            prev = await db.kv_get(f"{skey}:{grp}")
            if prev is None:  # ще не знаємо — беремо з каналів/графіка
                a = await db.latest_alert(grp, hours=12, provider=prov)
                off = (a and a["kind"] == "off" and a["grp"] == grp) or _now_off_by_schedule(sch, grp)
                prev = "off" if off else "on"
            if new == prev:
                continue
            last_push = int(await db.kv_get(f"{skey}_ts:{grp}") or 0)
            if time.time() - last_push < 20 * 60:  # не смикаємо людей частіше раз на 20 хв
                continue
            await db.kv_set(f"{skey}:{grp}", new)
            await db.kv_set(f"{skey}_ts:{grp}", str(int(time.time())))

            if new == "on":
                what = "почали вмикати світло" if n_off else "дали світло"
                head, n = f"💡 <b>Люди пишуть: {name} {grp} — {what}</b>", n_on
            else:
                head, n = f"🕯 <b>Люди пишуть: {name} {grp} — вимкнули світло</b>", n_off
            text = f"{head}\n👥 {n} людей за 15 хв"
            if places := [p for p, _ in c["places"].most_common(4)]:
                text += "\n📍 " + ", ".join(escape(p) for p in places)
            text += "\n\n<i>Неофіційно — за повідомленнями в чатах</i>"

            users = users if users is not None else [u for u in await db.users_with_group() if u["notify_on"]]
            for u in users:
                if u["grp"] == grp and _prov(u) == prov:
                    await _send(bot, u["id"], text)
                    await asyncio.sleep(0.05)
            log.info("crowd %s %s -> %s (%d on / %d off)", prov, grp, new, n_on, n_off)


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
