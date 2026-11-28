"""Фонові сповіщення: перед відключенням, при зміні графіка, аварії з каналів, «люди пишуть»."""
import asyncio
import json
import logging
import time
from datetime import datetime
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError

import db
import directory_refresh
import health
import dtek_http
import dtek_site
import keyboards as kb
import providers
import status
import svitlobot
from config import (CITY, CROWD_MIN, DIGEST_HOUR, QUIET_FROM, QUIET_TO, DTEK_POLL_FAST_SEC, DTEK_POLL_INTERVAL_SEC, DTEK_STREETS, SOURCE, SVITLOBOT_ENABLED,
                    SVITLOBOT_POLL_INTERVAL, TZ)
from providers import get_schedule
from render import day_text, hm, now_h, now_status

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


def _quiet_hour(hour: int) -> bool:
    return (hour >= QUIET_FROM or hour < QUIET_TO) if QUIET_FROM > QUIET_TO else QUIET_FROM <= hour < QUIET_TO


async def in_quiet(uid: int) -> bool:
    """Людина ввімкнула тихі години і зараз ніч (київський час)."""
    u = await db.get_user(uid)
    return bool(u.get("quiet")) and _quiet_hour(datetime.now(TZ).hour)


RATE_MAX, RATE_WINDOW = 6, 3600  # групових пушів на людину за годину; решту згортаємо в одне «ще N»
_rate: dict[int, list[float]] = {}
_skipped: dict[int, int] = {}


async def _send(bot: Bot, uid: int, text: str, urgent: bool = False):
    """urgent — аварія в самому будинку людини (і відповідь на її ж дію): іде й у тихі години і без ліміту."""
    if not urgent and await in_quiet(uid):
        await db.bump("quiet_skip")
        return
    if not urgent:
        now = time.time()
        recent = [t for t in _rate.get(uid, []) if now - t < RATE_WINDOW]
        if len(recent) >= RATE_MAX:
            _rate[uid] = recent
            _skipped[uid] = _skipped.get(uid, 0) + 1
            await db.bump("rate_skip")
            return
        _rate[uid] = recent + [now]
        if n := _skipped.pop(uid, 0):
            text += (f"\n\nℹ️ Ще {n} {'оновлення' if n < 5 else 'оновлень'} за останню годину не надсилав, "
                     "щоб не спамити, — актуальне завжди в «⚡ Зараз».")
    try:
        await bot.send_message(uid, text, reply_markup=kb.main_menu())
        await db.bump("push")
    except TelegramForbiddenError:
        await db.set_user(uid, notify_on=0)  # заблокував бота
        await db.bump("blocked")
    except Exception as e:
        log.warning("send %s failed: %r", uid, e)
        await db.bump("push_fail")


async def tick(bot: Bot):
    today = datetime.now(TZ).date()
    dates = providers.schedule_dates(today)
    users = await db.users_with_group()
    t = now_h()
    initialized = await db.kv_get("sched_init") is not None  # перший прохід після деплою — без пушів

    schedules = await asyncio.gather(*(providers.get_schedule_days(prov, today=today) for prov in db.PROVIDERS))
    yasno_changes = providers.drain_schedule_changes()  # детектор знімків YASNO (обидва постачальники)
    for prov, days in zip(db.PROVIDERS, schedules):
        name = db.PROVIDER_NAME[prov]
        emergency = await db.get_emergency(prov)

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
                if not initialized or schedule.source == "yasno":
                    continue  # зміни YASNO по чергах шле детектор знімків (send_schedule_changes) — без дублів
                title = "Сьогодні" if day == "today" else "Завтра"
                if old is None:
                    head = f"📅 <b>{name}: з'явився графік на {title.lower()}</b>"
                else:
                    head = f"🔄 <b>{name}: графік на {title.lower()} оновився</b>\nБуло: {old}\nСтало: <b>{new}</b>"
                text = f"{head}\n\n{day_text(grp, slots, title)}"
                if label := schedule.label():
                    text += f"\n\n{label}"
                if day == "today" and emergency:
                    text += "\n\n🚨 Зараз діють екстрені відключення — вони можуть бути поза цим графіком."
                changes.setdefault(grp, []).append(text)
        if not initialized and any(schedule.groups for schedule in days.values()):
            await db.kv_set("sched_init", "1")

        for c in yasno_changes:
            if c.provider == prov and c.date in dates.values():
                changes.setdefault(c.grp, []).append(change_text(c, name, emergency and c.day == "today"))
        for u in users:
            if not u["notify_on"] or _prov(u) != prov:
                continue
            for text in changes.get(u["grp"], []):
                await _send(bot, u["id"], text)
                await asyncio.sleep(0.05)
            # 2) попередження перед відключенням — шлемо і під час екстрених (так просять користувачі)
            for s in days["today"].groups.get(u["grp"], []):
                if s.kind != "off":
                    continue
                left = (s.start - t) * 60
                if 0 <= left <= u["notify_min"] and await db.mark_sent(f"warn:{today}:{u['id']}:{prov}:{s.start}"):
                    await _send(
                        bot, u["id"], f"🔔 Через {int(left)} хв вимкнуть світло ({hm(s.start)}–{hm(s.end)}). Заряджай павербанк!"
                    )


def change_text(c, name: str, emergency: bool = False) -> str:
    """Зміна графіка черги з YASNO -> текст пуша: було / стало / що додали й прибрали."""
    title = "Сьогодні" if c.day == "today" else "Завтра"
    if c.old is None:
        head = f"📅 <b>{name}: з'явився графік на {title.lower()}</b> · група <b>{c.grp}</b>"
    else:
        head = (f"🔄 <b>{name}: графік на {title.lower()} змінився</b> · група <b>{c.grp}</b>\n"
                f"Було: {_slots_str(c.old)}\nСтало: <b>{_slots_str(c.new)}</b>")
        if c.added:
            head += f"\n➕ Додали: {_slots_str(c.added)}"
        if c.removed:
            head += f"\n➖ Прибрали: {_slots_str(c.removed)}"
    text = f"{head}\n\n{day_text(c.grp, c.new, title)}\n\n"
    text += f"🕒 Оновлено {escape(c.updated)} · сайт YASNO" if c.updated else "ℹ️ Джерело: сайт YASNO"
    if emergency:
        text += "\n\n🚨 Зараз діють екстрені відключення — вони можуть бути поза цим графіком."
    return text


# ---------- «вимкнули» — один раз: замок на групу ----------
# Перше «вимкнули» (чати, датчики, канал, сайт ДТЕК) замикає групу: наступні «вимкнули» з чатів і датчиків
# не шлемо — люди вже знають. Знімає замок лише надійне «світло дали»: датчики Світлобота, канал чи сайт ДТЕК.
# «Дали» з чатів замок не знімає (часто це «дали частині»). Страховка — замок сам спадає за OFF_LOCK_SEC.
OFF_LOCK_SEC = 4 * 3600


async def off_locked(prov: str, grp: str) -> bool:
    v = json.loads(await db.kv_get(f"offlock:{prov}:{grp}") or "null")
    return bool(v) and time.time() - v.get("ts", 0) < OFF_LOCK_SEC


async def lock_off(prov: str, grp: str, src: str):
    await db.kv_set(f"offlock:{prov}:{grp}", json.dumps({"ts": int(time.time()), "src": src}))
    await db.kv_set(f"onsent:{prov}:{grp}", "")  # нове відключення — наступне «дали» вже новина


# «Дали світло / триває відновлення» — так само один раз: каналі шлють його по колу кожні 1–2 год (59 за добу).
ON_REPEAT_SEC = 6 * 3600


async def on_already(prov: str, grp: str) -> bool:
    v = json.loads(await db.kv_get(f"onsent:{prov}:{grp}") or "null")
    return bool(v) and time.time() - v.get("ts", 0) < ON_REPEAT_SEC


async def mark_on(prov: str, grp: str):
    await db.kv_set(f"onsent:{prov}:{grp}", json.dumps({"ts": int(time.time())}))


async def unlock_off(prov: str, grp: str):
    await db.kv_set(f"offlock:{prov}:{grp}", "")


ON_HOLD_SEC = 5 * 60  # «світло дали» з каналів чекає 5 хв, щоб звірити з чатами


async def _contradicted_by_chats(a: dict) -> bool:
    """Канал каже «не підтверджується / відновлення», а люди в чатах пишуть «нема» -> не шлемо."""
    c = (await db.crowd(30, a.get("provider") or "dtek")).get(a["grp"])
    if not c:
        return False
    n_on, n_off = len(c["on"]), len(c["off"])
    return n_off >= 2 and n_off >= n_on


ALERT_FOOTER = {"channel": "За даними Telegram-каналів", "dteksite": "Офіційно — сайт ДТЕК, перевірка адрес групи"}
OFFICIAL_SRC = ("dteksite",)


async def send_alerts(bot: Bot):
    """Події з каналів і сайту ДТЕК (юзербот кладе в alerts) -> людям потрібної групи і постачальника,
    загальні — всім. Офіційні (сайт ДТЕК) — одразу, без звірки з чатами."""
    alerts = await db.pending_alerts()
    if not alerts:
        return
    users = [u for u in await db.users_with_group() if u["notify_on"]]
    import config

    for a in alerts:
        src = a.get("src") or "channel"
        if src == "channel" and not config.CHANNEL_ALERTS:  # аварії з каналів вимкнені — лише сайт ДТЕК
            await db.mark_alert_sent(a["key"])
            continue
        if a["kind"] == "on" and src not in OFFICIAL_SRC:
            if time.time() - a["ts"] < ON_HOLD_SEC:
                continue  # ще чекаємо, що скажуть люди
            if await _contradicted_by_chats(a):
                await db.mark_alert_sent(a["key"])
                log.info("alert %s dropped: chats say no power", a["key"])
                continue
        await db.mark_alert_sent(a["key"])
        prov = a.get("provider") or "dtek"
        if a["kind"] in ("on", "off") and prov in db.PROVIDERS:
            # група вже знає — «люди пишуть» з тим самим не дублюємо
            await db.kv_set(f"{_pkey(prov, 'crowd')}:{a['grp']}", a["kind"])
            if a["grp"] != "*":
                if a["kind"] == "off":
                    await lock_off(prov, a["grp"], src)
                else:
                    await unlock_off(prov, a["grp"])
                    if await on_already(prov, a["grp"]):  # «триває відновлення» вже було — не повторюємо
                        log.info("alert %s: тихо, група вже знає, що дають світло", a["key"])
                        continue
                    await mark_on(prov, a["grp"])
        for u in users:
            if _matches(prov, u) and (a["grp"] == "*" or a["grp"] == u["grp"]):
                await _send(bot, u["id"], a["text"] + f"\n\n<i>{ALERT_FOOTER.get(src, ALERT_FOOTER['channel'])}</i>")
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
        emergency = await db.get_emergency(prov)
        # під час екстрених «вимкнули» майже напевно правда — вистачає двох людей, щоб пушнути швидше
        min_off = min(CROWD_MIN, 2) if emergency else CROWD_MIN
        for grp, c in data.items():
            n_on, n_off = len(c["on"]), len(c["off"])
            # офіційно ще відключення (аварія «до …» / слот графіка) — вмикають поступово: «дали» від 3 людей
            # ще не новина; пушимо «почали вмикати» лише від ON_PARTIAL_MIN людей
            o_alert, o_slot, _, o_until = await status.official_outage(grp, prov, sch)
            official = bool(o_alert or o_slot)
            min_on = max(CROWD_MIN, status.ON_PARTIAL_MIN) if official else CROWD_MIN
            if n_on >= min_on and n_on >= 2 * n_off:
                new = "on"
            elif n_off >= min_off and n_off >= 2 * n_on:
                new = "off"
            else:
                continue
            skey = _pkey(prov, "crowd")
            prev = await db.kv_get(f"{skey}:{grp}")
            if prev is None:  # ще не знаємо — беремо з каналів/графіка
                a = await db.latest_alert(grp, hours=12, provider=prov)
                off = (a and a["kind"] == "off" and a["grp"] == grp) or (not emergency and _now_off_by_schedule(sch, grp))
                prev = "off" if off else "on"
            if new == prev:
                continue
            last_push = int(await db.kv_get(f"{skey}_ts:{grp}") or 0)
            if time.time() - last_push < 20 * 60:  # не смикаємо людей частіше раз на 20 хв
                continue
            await db.kv_set(f"{skey}:{grp}", new)
            if new == "off":
                if await off_locked(prov, grp):  # про це відключення група вже знає — не повторюємо
                    log.info("crowd %s %s -> off: тихо, група вже знає (%d off)", prov, grp, n_off)
                    continue
                await lock_off(prov, grp, "crowd")
            await db.kv_set(f"{skey}_ts:{grp}", str(int(time.time())))

            if new == "on":
                if await on_already(prov, grp):  # про «дали» група вже знає (канал / сайт / датчики)
                    log.info("crowd %s %s -> on: тихо, група вже знає", prov, grp)
                    continue
                await mark_on(prov, grp)
                what = "почали вмикати світло" if (n_off or official) else "дали світло"
                head, n = f"💡 <b>Люди пишуть: {name} {grp} — {what}</b>", n_on
                if official:
                    if await status.partial_outage(grp, prov, o_alert, o_slot):  # «до …» — лише для частини адрес
                        head += "\n🟡 Поки не всім — офіційно відключення лише на частині адрес групи"
                    else:
                        head += "\n🟡 Поки не всім" + (f" — офіційно відключення до <b>{o_until}</b>" if o_until else "")
            elif await status.sensors_say_on(grp, prov) or await status.partial_outage(grp, prov, o_alert, o_slot):
                # датчики групи бачать світло / на сайті ДТЕК без світла лише частина адрес — не «вся група»
                head, n = f"🕯 <b>Люди пишуть: {name} {grp} — у частини людей вимкнули світло</b>", n_off
                if await status.sensors_say_on(grp, prov):
                    head += "\n📡 За датчиками в більшості будинків групи світло є"
            elif emergency:
                head, n = f"🚨 <b>{name} {grp}: вимкнули світло — екстрене відключення</b>", n_off
            else:
                head, n = f"🕯 <b>Люди пишуть: {name} {grp} — вимкнули світло</b>", n_off
            text = f"{head}\n👥 {n} людей за 15 хв"
            if places := [p for p, _ in c["places"].most_common(4)]:
                text += "\n📍 " + ", ".join(escape(p) for p in places)
            if emergency:
                text += "\n⚠️ У місті діють екстрені — можуть знову вимкнути будь-коли" if new == "on" else (
                    "\n🚨 У місті офіційно діють екстрені відключення, графік не діє"
                )
            text += "\n\n<i>Неофіційно — за повідомленнями в чатах</i>"

            users = users if users is not None else [u for u in await db.users_with_group() if u["notify_on"]]
            for u in users:
                if u["grp"] == grp and _prov(u) == prov:
                    await _send(bot, u["id"], text)
                    await asyncio.sleep(0.05)
            log.info("crowd %s %s -> %s (%d on / %d off)", prov, grp, new, n_on, n_off)


EMERGENCY_OFF_SEC = 10 * 60  # «скасували» підтверджуємо 10 хв — разовий збій джерела не смикає людей
NOTICE_SOURCE = {"dtek": "сайт ДТЕК", "oua": "сайт ДТЕК (через outage-data-ua)", "yasno": "YASNO"}


async def emergency_check(bot: Bot):
    """Офіційно оголосили екстрені відключення в місті -> одразу пуш усім абонентам постачальника (усім групам);
    скасували -> «знову діє графік». Стан лежить у БД — за ним же екран «Зараз» пише «світла немає, екстрене»."""
    users = None
    for prov in db.PROVIDERS:
        em = await providers.get_emergency(prov)
        if em is None:
            continue  # джерела мовчать — стан не чіпаємо
        state = await db.get_emergency(prov)
        name, now, off_key = db.PROVIDER_NAME[prov], int(time.time()), f"emerg_off:{prov}"
        sch = None
        if em.active:
            await db.kv_set(off_key, "")
            if state:
                if em.text and em.text != state.get("text"):  # ДТЕК дописав район — оновлюємо мовчки
                    await db.set_emergency(prov, {**state, "text": em.text, "source": em.source})
                continue
            await db.set_emergency(prov, {"since": now, "text": em.text, "source": em.source})
            text = (
                f"🚨 <b>{name}: у Дніпрі екстрені відключення</b>\n"
                "Графік зараз не діє — світло можуть вимкнути будь-якої миті, без попередження."
            )
            if em.text:
                text += f"\n\n<i>{escape(em.text)}</i>"
            text += f"\n\nℹ️ Джерело: {NOTICE_SOURCE[em.source]}. Що з твоєю групою — у «⚡ Зараз»."
        else:
            if not state:
                continue
            off_since = int(await db.kv_get(off_key) or 0)
            if not off_since:
                await db.kv_set(off_key, str(now))
                continue
            if now - off_since < EMERGENCY_OFF_SEC:
                continue
            await db.set_emergency(prov, None)
            await db.kv_set(off_key, "")
            text = f"✅ <b>{name}: екстрені відключення в Дніпрі скасували</b>\nЗнову діє графік.\n\n"
            sch = await get_schedule(provider=prov)
        log.info("emergency %s -> %s (%s)", prov, em.active, em.source)
        users = users if users is not None else [u for u in await db.users_with_group() if u["notify_on"]]
        for u in users:
            if _prov(u) == prov:
                await _send(bot, u["id"], text + (now_status(sch.get("today", {}).get(u["grp"])) if sch else ""))
                await asyncio.sleep(0.05)


_site_next = 0.0
_site_warned = 0.0
MAX_USER_STREETS = 30  # скільки вулиць підписників перевіряти за раз (решта — наступного разу, по колу)


async def _finish_address(u: dict, street: str, h) -> str:
    """Адреса чекала на сайт ДТЕК — сайт відповів: ставимо будинок і групу, повертаємо текст для людини."""
    _, _, house = u["addr_q"].partition("|")
    if h is None:
        await db.set_user(u["id"], addr_q="")
        return (f"🏠 Сайт ДТЕК відповів, але будинку <b>{escape(house)}</b> на вулиці <b>{escape(street)}</b> там немає.\n"
                "Перевір номер (⚙️ Налаштування → 📍) — або твій будинок обслуговує ЦЕК, тоді обери групу вручну.")
    if h.cek:
        grp = dtek_site.cek_group(h)
        await db.set_user(u["id"], addr_q="", provider="cek", street="", house="", **({"grp": grp} if grp else {}))
        return (f"🏠 {escape(street)}, {escape(h.num)}\nЗа даними сайту ДТЕК твій будинок обслуговує <b>ЦЕК</b>"
                + (f" · група <b>{grp}</b>. Поставив ЦЕК {grp}." if grp else ". Обери групу ЦЕК вручну (є в рахунку)."))
    grp = h.groups[0] if h.groups else None
    await db.set_user(u["id"], addr_q="", provider="dtek", street=street, house=h.num, **({"grp": grp} if grp else {}))
    grp_line = f"🏢 ДТЕК · група <b>{grp}</b>" if grp else "🏢 ДТЕК · у цього будинку на сайті ДТЕК черги немає — стежу за самим будинком"
    return (f"✅ <b>Знайшов твій будинок!</b>\n🏠 {escape(street)}, {escape(h.num)}\n{grp_line}\n\n"
            "Тепер стежу за ним на сайті ДТЕК і напишу, щойно буде аварія, продовження чи світло повернеться.")


async def subscriber_streets() -> tuple[list[str], list[dict], list[dict], dict[int, list[str]]]:
    """Вулиці, де є підписники: адреси людей (і будинки без черги) + адреси «⏳ чекає на сайт» (першими).
    -> (вулиці, люди з адресою, люди, що чекають, кандидати вулиць для тих, хто чекає)."""
    addr_users = await db.users_with_address()
    user_streets = list(dict.fromkeys(u["street"] for u in addr_users))
    if len(user_streets) > MAX_USER_STREETS:  # по колу, щоб усі вулиці перевірялись
        shift = int(time.time() // max(DTEK_POLL_INTERVAL_SEC, 1)) % len(user_streets)
        user_streets = (user_streets[shift:] + user_streets[:shift])[:MAX_USER_STREETS]
    pending = await db.users_addr_pending()
    cands = {u["id"]: await dtek_site.street_candidates(u["addr_q"].partition("|")[0]) for u in pending}
    pending_streets = [st for c in cands.values() for st in c]
    streets = list(dict.fromkeys(pending_streets + user_streets + list(DTEK_STREETS)))
    return streets, addr_users, pending, cands


SOON_MIN_GAP = 60  # позапланова перевірка (людина вказала адресу) — не частіше раза на хвилину
_site_last = -1e9


async def _set_next(at_monotonic: float):
    """Коли наступна перевірка — для «⚡ Зараз» («наступна ≈ 12:49»)."""
    global _site_next
    _site_next = at_monotonic
    await db.kv_set("site_next_ts", str(int(time.time() + at_monotonic - time.monotonic())))


async def site_check(bot: Bot):
    """Сайт ДТЕК, «Перевірити адресу» — бережно: раз на 10 хв (під час аварій — 5), лише вулиці з
    підписниками, 3–5 с між запитами; відмова сайту -> DTEK_BACKOFF_SEC тиші. Людина щойно вказала адресу —
    позапланово (не частіше раза на хвилину). Будинки людей -> особисті пуші; аварії з часом ремонту ->
    group_incidents (send_incidents шле групі одне зведене сповіщення)."""
    global _site_warned, _site_last
    if not DTEK_POLL_INTERVAL_SEC or SOURCE != "dtek" or CITY != "dnipro":
        return
    if time.monotonic() < _site_next:
        if time.monotonic() - _site_last < SOON_MIN_GAP or not dtek_site.take_poll_soon():
            return
    if left := dtek_http.backoff_left():
        await _set_next(time.monotonic() + left)
        return
    _site_last = time.monotonic()
    await _set_next(time.monotonic() + DTEK_POLL_INTERVAL_SEC)
    streets, addr_users, pending, cands = await subscriber_streets()
    if not streets:
        return  # підписників з адресою немає — сайт не смикаємо зовсім
    started = time.monotonic()
    try:
        probe = await dtek_site.probe(streets)
    except dtek_http.ERRORS as e:
        await _set_next(time.monotonic() + max(dtek_http.backoff_left(), DTEK_POLL_INTERVAL_SEC))
        log.warning("сайт ДТЕК: перевірка адрес не вдалась: %r", e)
        await health.mark("dtek", False, repr(e))
        await db.kv_set("site_probe", json.dumps({"ok": False, "ts": int(time.time()), "err": str(e)[:80]}, ensure_ascii=False))
        return
    await db.kv_set("site_probe", json.dumps({"ok": True, "ts": int(time.time()), "houses": len(probe.houses)}))
    await health.mark("dtek", True)
    if probe.page_fetched:  # сторінку тягнули (раз на ~20 хв) — заодно свіже вікно «Шановні клієнти!»
        providers.set_notice("dtek", probe.attention)
    for u, street, h in dtek_site.resolve_pending(probe, pending, cands):
        await _send(bot, u["id"], await _finish_address(u, street, h), urgent=True)
        await asyncio.sleep(0.05)
    for change in await dtek_site.apply(probe):
        log.info("сайт ДТЕК: %s", change)
    for uid, text in await dtek_site.apply_users(probe, addr_users):
        await _send(bot, uid, text, urgent=True)  # будинок людини — і вночі
        await asyncio.sleep(0.05)
    if n := await dtek_site.record_incidents(probe.houses):
        log.info("сайт ДТЕК: нових аварій по чергах: %d", n)
    await _set_next(started + await poll_interval(addr_users))
    if time.monotonic() - _site_warned > 3600 and probe.failed:
        _site_warned = time.monotonic()
        log.warning("сайт ДТЕК: вулиці без відповіді: %s", ", ".join(probe.failed))


_fast_mode = False


async def poll_interval(addr_users: list[dict]) -> int:
    """10 хв спокійно; 5 хв, поки в місті екстрені, у когось із підписників зараз нема світла (сайт ДТЕК)
    чи по його черзі є аварія — щоб швидко сказати і про продовження, і «світло повернулось»."""
    global _fast_mode
    reason = None
    if DTEK_POLL_FAST_SEC and DTEK_POLL_FAST_SEC < DTEK_POLL_INTERVAL_SEC:
        if await db.get_emergency("dtek"):
            reason = "екстрені в місті"
        elif any([await db.kv_get(f"house:{u['id']}") for u in addr_users]):
            reason = "у підписника нема світла"
        else:
            for g in {u["grp"] for u in addr_users if u.get("grp")}:
                if await db.active_incidents("dtek", g):
                    reason = f"аварія по черзі {g}"
                    break
    if bool(reason) != _fast_mode:
        _fast_mode = bool(reason)
        log.info("сайт ДТЕК: опитування раз на %d хв%s", (DTEK_POLL_FAST_SEC if reason else DTEK_POLL_INTERVAL_SEC) // 60,
                 f" ({reason})" if reason else "")
    return DTEK_POLL_FAST_SEC if reason else DTEK_POLL_INTERVAL_SEC


INCIDENT_MERGE_SEC = 60 * 60  # та сама причина по групі з кінцем у межах години — те саме відключення, не новина


async def send_incidents(bot: Bot):
    """Нові аварії по чергах -> одне зведене сповіщення всім підписникам черги на (черга, причина, кінець).
    Сусідні вулиці / канал з тим самим — уже той самий інцидент (унікальний ключ у group_incidents);
    кінець, зсунутий менше ніж на годину від уже розісланого, — теж не дублюємо. Жителям будинку, про який
    уже прийшов особистий пуш, зведене не шлемо."""
    for inc in await db.pending_incidents():
        await db.mark_incident_notified(inc["id"])
        prev = await db.notified_incidents(inc["operator"], inc["group_name"], inc["reason"], inc["id"])
        if any(abs(x["end_time"] - inc["end_time"]) < INCIDENT_MERGE_SEC for x in prev):
            continue  # той самий випадок з сусідньої вулиці / дрібне уточнення часу
        head = None
        if prev:  # той самий випадок, кінець зсунувся на годину і більше
            longer = inc["end_time"] > prev[0]["end_time"]
            head = "відключення продовжено" if longer else "відключення скорочено"
            if longer and (n := await db.extensions_today(inc["operator"], inc["group_name"], inc["reason"])) >= 2:
                head += f" (вже {n}-й раз)"
        text = incident_text(inc, db.PROVIDER_NAME.get(inc["operator"], inc["operator"]), head)
        for u in await db.users_in_group(inc["operator"], inc["group_name"]):
            if await db.kv_get(f"house:{u['id']}"):  # про свій будинок людина вже отримала особистий пуш
                continue
            await _send(bot, u["id"], text)
            await asyncio.sleep(0.05)
        log.info("аварія %s %s: %s до %s — групі розіслано", inc["operator"], inc["group_name"], inc["reason"],
                 datetime.fromtimestamp(inc["end_time"], TZ).strftime("%H:%M %d.%m"))


def incident_text(inc: dict, name: str, head: str | None = None) -> str:
    now = datetime.now(TZ)
    title = f"{name}: {head}" if head else f"{name}: аварія на адресах групи"
    lines = [f"🚧 <b>{title} · група {inc['group_name']}</b>", f"⚡ {escape(inc['reason'])}"]
    if inc.get("street_name"):
        lines.append(f"📍 {escape(inc['street_name'])}")
    lines.append(f"⏱ Орієнтовно до <b>{status.incident_until(inc, now)}</b>")
    lines.append("ℹ️ Можуть вимкнути не всю групу — лише частину адрес. Що з тобою — у «⚡ Зараз».")
    src = "сайт ДТЕК (перевірка адрес)" if inc.get("street_name") else "Telegram-канали"
    lines.append(f"\n<i>За даними: {src}</i>")
    return "\n".join(lines)


_svb_next = 0.0
_svb_fails = 0


async def svitlobot_check(bot: Bot):
    """Датчики Світлобота (раз на SVITLOBOT_POLL_INTERVAL) -> стан кожної групи ДТЕК/ЦЕК; ON<->OFF -> пуш групі.
    Джерело впало (мережа, 401/403/404/429) — пауза 2×, 4× … до години, решта бота працює далі."""
    global _svb_next, _svb_fails
    if not SVITLOBOT_ENABLED or time.monotonic() < _svb_next:
        return
    _svb_next = time.monotonic() + SVITLOBOT_POLL_INTERVAL
    try:
        points = await svitlobot.fetch_points()
    except svitlobot.SourceError as e:
        _svb_fails += 1
        pause = e.retry_after or min(SVITLOBOT_POLL_INTERVAL * 2 ** _svb_fails, 3600)
        _svb_next = time.monotonic() + pause
        log.warning("світлобот: %s — наступна спроба за %d с", e, pause)
        await health.mark("svitlobot", False, str(e))
        return
    _svb_fails = 0
    await health.mark("svitlobot", True)
    changes = await svitlobot.apply(points)
    if not changes:
        return
    users = [u for u in await db.users_with_group() if u["notify_on"]]
    for c in changes:
        log.info("світлобот: %s %s -> %s (%d/%d)", c.provider, c.grp, c.state, c.stat.on, c.stat.alive)
        if c.state == svitlobot.OFF:
            if await off_locked(c.provider, c.grp):
                continue  # «вимкнули» вже було — датчики лише підтверджують
            await lock_off(c.provider, c.grp, "svitlobot")
        else:
            await unlock_off(c.provider, c.grp)  # датчики бачать світло — наступне «вимкнули» вже новина
            if await on_already(c.provider, c.grp):
                continue
            await mark_on(c.provider, c.grp)
        text = svitlobot.notify_text(c, db.PROVIDER_NAME[c.provider]) + "\n\n<i>За даними датчиків Світлобота</i>"
        for u in users:
            if u["grp"] == c.grp and _prov(u) == c.provider:
                await _send(bot, u["id"], text)
                await asyncio.sleep(0.05)


async def digest_text(u: dict) -> str:
    """☀️ Ранкове зведення: будинок (сайт ДТЕК), графік на сьогодні, екстрені й аварії по черзі."""
    prov, grp = _prov(u), u.get("grp")
    name = db.PROVIDER_NAME[prov]
    lines = ["☀️ <b>Доброго ранку!</b>"]
    if house := await status.house_block(u):
        lines += ["", house]
    if grp:
        days = await providers.get_schedule_days(prov)
        lines += ["", f"🏢 {name}\n" + day_text(grp, days["today"].groups.get(grp), "Сьогодні")]
        now = datetime.now(TZ)
        for inc in (await db.active_incidents(prov, grp, int(now.timestamp())))[:2]:
            lines.append(status.incident_line(inc, now))
    if await db.get_emergency(prov):
        lines += ["", "🚨 У Дніпрі діють екстрені відключення — графік може не діяти"]
    return "\n".join(lines)


async def morning_digest(bot: Bot):
    """Раз на день о DIGEST_HOUR — тим, хто ввімкнув «☀️ Зведення»."""
    now = datetime.now(TZ)
    if now.hour != DIGEST_HOUR or await db.kv_get(f"digest_day:{now:%Y-%m-%d}"):
        return
    await db.kv_set(f"digest_day:{now:%Y-%m-%d}", "1")
    users = await db._all("SELECT * FROM users WHERE digest=1 AND notify_on=1")
    for u in users:
        try:
            await _send(bot, u["id"], await digest_text(u), urgent=True)
        except Exception:
            log.exception("зведення %s", u["id"])
        await asyncio.sleep(0.05)
    if users:
        log.info("ранкове зведення: %d людей", len(users))


async def loop(bot: Bot, every=60):
    """Сайт ДТЕК — раз на хвилину; аварії, «люди пишуть» і екстрені — кожні 15 с; графік — раз на хвилину."""
    asyncio.create_task(directory_refresh.run(bot))  # оновлення довідника (/db) — своїм темпом
    n = 0
    while True:
        for job in (site_check, svitlobot_check, send_alerts, send_incidents, crowd_check, emergency_check,
                    health.check, morning_digest) + (
                (tick,) if n % 4 == 0 else ()):
            try:
                await job(bot)
            except Exception:
                log.exception("notifier %s failed", job.__name__)
        n += 1
        await asyncio.sleep(every // 4)
