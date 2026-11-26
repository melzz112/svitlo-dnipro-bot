"""Підсумок «є світло чи ні» для екрана «Зараз»: графік + екстрені (сайт ДТЕК, канали) + що пишуть люди.

Люди пишуть «дали» один раз і далі мовчать, поки світло є. Тому рахуємо не «за останні 30 хв»,
а все, що написали з початку поточного відключення (перший алерт ланцюжка або початок слоту за графіком),
по одному — останньому — голосу від людини. «Продовжено до 10:18» від ДТЕК не перекреслює «дали»,
які люди написали вже після початку відключення.
"""
import json
import re
import time
from datetime import datetime, timedelta
from html import escape

import db
from config import TZ
from providers import get_schedule
from render import hm, now_h

NO_OUTAGE_WINDOW = 3 * 3600  # якщо офіційно відключення немає — дивимось останні 3 год
MAX_WINDOW = 12 * 3600


def _until_dt(until: str, ts: int) -> datetime | None:
    """'22:25 19.11' або '22:25' (дата = день алерту) -> datetime; None якщо не розібрати."""
    m = re.match(r"(\d{1,2}):(\d{2})(?:\s+(\d{2})\.(\d{2}))?", until or "")
    if not m:
        return None
    base = datetime.fromtimestamp(ts, TZ)
    h, mi = int(m.group(1)), int(m.group(2))
    day = base.replace(day=int(m.group(3)), month=int(m.group(4))) if m.group(3) else base
    dt = day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=h, minutes=mi)
    if not m.group(3) and dt < base:  # «до 02:00» уночі — це вже наступна доба
        dt += timedelta(days=1)
    return dt


def _is_active(a: dict, now: datetime) -> bool:
    end = _until_dt(a["until"], a["ts"])
    if end is None:  # без часу — вважаємо актуальним 3 год
        return now.timestamp() - a["ts"] < 3 * 3600
    return end > now


async def _outage_chain(grp, provider, now):
    """Активні аварійні алерти групи: (останній — для причини, час першого — початок відключення).
    Сайт ДТЕК сказав «екстрене закінчилось» — усе, що було до того, вже не діє."""
    active = [a for a in await db.off_alerts(grp, provider) if _is_active(a, now)]
    if ended := await db.last_alert("on", grp, 12 * 60, provider, src="dteksite"):
        active = [a for a in active if a["ts"] > ended["ts"]]
    if not active:
        return None, None
    return active[-1], active[0]["ts"]


def is_emergency_alert(a: dict | None) -> bool:
    return bool(a) and "екстрен" in a["text"].lower()


async def emergency_today(grp: str, provider: str) -> str | None:
    """Що показати замість графіка на сьогодні, поки діють екстрені (у місті або по групі). None — графік діє."""
    now = datetime.now(TZ)
    city = await db.get_emergency(provider)
    alert, _ = await _outage_chain(grp, provider, now)
    alert = alert if is_emergency_alert(alert) else None
    if not city and not alert:
        return None
    lines = [
        "🚨 <b>Зараз діють екстрені відключення</b>",
        "Графік не діє — світло вимикають без графіка, за командами Укренерго.",
    ]
    if alert:
        lines += ["", f"🔴 Група <b>{grp}</b> зараз без світла"]
        for icon in ("⏱", "🏠"):  # «з 13:16, орієнтовно до 17:16 20.11», «без світла 3 з 4 адрес»
            if m := re.search(icon + r" ([^\n]*)", alert["text"]):
                lines.append(f"{icon} {m.group(1)}")
    if city and city.get("text"):
        lines += ["", f"<i>{escape(city['text'])}</i>"]
    return "\n".join(lines)


def _label(alert_text: str) -> str:
    m = re.search(r"<b>(.*?)</b>", alert_text)
    return (m.group(1) if m else "відключення").lower()


def _ago(ts: float) -> str:
    m = int(time.time() - ts) // 60
    return f"{m} хв тому" if m < 60 else f"{m // 60} год {m % 60} хв тому"


ON_PARTIAL_MIN = 5  # стільки різних людей «дали», щоб під час офіційного відключення сказати «частково є світло»
OFF_LOCAL_MIN = 3  # стільки «нема» після офіційного кінця, щоб сказати «світла немає» (локальна аварія / затримка)


RECENT_SEC = 30 * 60
FAST_SEC = 5 * 60  # «за 5 хв ≥5 людей пишуть одне й те саме» — перемикаємо вердикт, хоч би що казав офіційний час
FAST_MIN = 5


def _recent_votes(people: dict, now: datetime) -> tuple[int, int]:
    """(«є», «нема») серед тих, хто писав за останні 30 хв (від кожної людини — останній голос)."""
    since = now.timestamp() - RECENT_SEC
    recent = [ev for ev, ts in people.values() if ts >= since]
    return recent.count("on"), len(recent) - recent.count("on")


def _hm_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%H:%M")


async def official_outage(grp: str, provider: str, sch: dict | None = None, now: datetime | None = None):
    """Офіційно зараз відключення? -> (алерт з каналів/сайту ДТЕК або None, поточний слот графіка або None,
    час початку відключення, «до HH:MM» текстом). Під час екстрених у місті графік не діє — слот не враховуємо."""
    now = now or datetime.now(TZ)
    sch = sch if sch is not None else await get_schedule(provider=provider)
    city_em = await db.get_emergency(provider)
    t = now_h()
    slot = None if city_em else next(
        (s for s in sch.get("today", {}).get(grp, []) if s.kind == "off" and s.start <= t < s.end), None
    )
    alert, alert_start = await _outage_chain(grp, provider, now)
    starts = []
    if alert_start:
        starts.append(alert_start)
    if slot:
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        starts.append((midnight + timedelta(hours=slot.start)).timestamp())
    if alert and alert["until"]:
        until = _short_until(alert["until"], now)
    elif slot:
        until = hm(slot.end)
    else:
        until = None
    return alert, slot, (min(starts) if starts else None), until


async def _recent_end(grp: str, provider: str, sch: dict, now: datetime) -> float | None:
    """Коли (за останні 3 год) офіційно скінчилось відключення групи: «до …» аварії минуло, сайт ДТЕК написав
    «екстрене закінчилось» або скінчився слот графіка. None — офіційних відключень останнім часом не було."""
    ends = []
    for a in await db.off_alerts(grp, provider):
        if (end := _until_dt(a["until"], a["ts"])) and end <= now:
            ends.append(end.timestamp())
    if ended := await db.last_alert("on", grp, 12 * 60, provider, src="dteksite"):
        ends.append(ended["ts"])
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for s in sch.get("today", {}).get(grp, []) or []:
        if s.kind == "off" and s.end <= now_h():
            ends.append((midnight + timedelta(hours=s.end)).timestamp())
    ends = [e for e in ends if e >= now.timestamp() - NO_OUTAGE_WINDOW]
    return max(ends) if ends else None


async def verdict(grp: str, provider: str) -> str:
    """Офіційний час — головний: поки офіційно відключення, «світло є» не пишемо, навіть якщо кілька людей
    написали «дали» (вмикають поступово). Люди лише уточнюють: багато «дали» -> «частково», після офіційного
    кінця «нема» -> «не всі зі світлом»."""
    now = datetime.now(TZ)
    sch = await get_schedule(provider=provider)
    city_em = await db.get_emergency(provider)  # у місті діють екстрені — графік не діє
    t = now_h()
    alert, slot, start, until = await official_outage(grp, provider, sch, now)
    today_slots = sch.get("today", {}).get(grp)
    note = None

    if alert or slot:
        # 1) офіційно зараз відключення — голоси з його початку (від кожної людини — останній)
        people = await db.crowd_people(grp, provider, max(start, now.timestamp() - MAX_WINDOW))
        n_on = sum(1 for ev, _ in people.values() if ev == "on")
        n_off = len(people) - n_on
        # свіжі голоси (30 хв) важать більше: «нема» о 3 ночі від людини, яка потім мовчить, не перекриває
        # хвилю «дали» зараз
        r_on, r_off = _recent_votes(people, now)
        till = f" до <b>{until}</b>" if until else ""
        if (n_on >= ON_PARTIAL_MIN and n_on >= 2 * n_off) or (r_on >= ON_PARTIAL_MIN and r_on >= 2 * r_off):
            head, sub = "🟡 <b>Частково є світло</b>", "люди масово пишуть «дали» — почали вмикати, але не всім"
        elif (n_on >= 1 and n_on >= n_off) or (r_on >= 2 and r_on > r_off):
            head, sub = "🔴 <b>Світла немає</b>", "дехто пише, що дали — можливо, почали вмикати"
        else:
            reason = _label(alert["text"]) if alert else "за графіком"
            head, sub = "🔴 <b>Світла немає</b>", reason + (" · люди підтверджують" if n_off >= 2 else "")
    else:
        # 2) офіційно відключення немає — «нема» рахуємо лише після офіційного кінця (раніші — про саме відключення)
        end_ts = await _recent_end(grp, provider, sch, now)
        people = await db.crowd_people(grp, provider, end_ts or now.timestamp() - NO_OUTAGE_WINDOW)
        n_on = sum(1 for ev, _ in people.values() if ev == "on")
        n_off = len(people) - n_on
        r_on, r_off = _recent_votes(people, now)
        till = ""
        if city_em and n_off >= 2 and n_off > n_on:  # під час екстрених «нема» майже напевно правда
            head, sub = "🔴 <b>Світла немає</b>", "екстрені відключення · люди підтверджують"
        elif n_off >= OFF_LOCAL_MIN and n_off > n_on:
            head, sub = "🔴 <b>Світла немає</b>", "люди пишуть, хоча офіційно відключення немає — можливо, локальна аварія"
        elif city_em:
            head, sub = "🟠 <b>Екстрені відключення</b>", "світло можуть вимкнути будь-якої миті"
        elif end_ts:
            head, sub = "🟢 <b>Світло є</b>", f"відключення скінчилось о {_hm_ts(end_ts)}"
        elif today_slots is None and not people:
            head, sub = "❔ <b>Невідомо</b>", "графіка немає, люди не пишуть"
        elif today_slots is None and n_on >= 2:
            head, sub = "🟢 <b>Світло є</b>", "люди пишуть, що дали"
        else:
            head, sub = "🟢 <b>Світло має бути</b>", "за графіком зараз без відключень"
        if n_off and not head.startswith("🔴"):
            note = f"⚠️ Не всі зі світлом — {n_off} {'пише' if n_off == 1 else 'пишуть'}, що досі нема"

    # 3) хвиля за 5 хв перекриває офіційний час: ≥5 людей і вдвічі більше, ніж протилежних
    fast = await db.crowd_people(grp, provider, now.timestamp() - FAST_SEC)
    f_on = sum(1 for ev, _ in fast.values() if ev == "on")
    f_off = len(fast) - f_on
    official = bool(alert or slot)
    if f_on >= FAST_MIN and f_on >= 2 * f_off and not head.startswith("🟢"):
        head = "🟡 <b>Є світло, але не в усіх</b>"
        sub = f"за 5 хв {f_on} людей пишуть «дали»" + (f" · офіційно — відключення{till}" if official else "")
        note = None
    elif f_off >= FAST_MIN and f_off >= 2 * f_on and not head.startswith("🔴"):
        head = "🟠 <b>Світла немає, але не в усіх</b>"
        sub = f"за 5 хв {f_off} людей пишуть «нема»" + ("" if official else " · офіційно відключення немає")
        note = None

    lines = [head, f"<i>{sub}</i>", ""]
    # 3) факти: люди / офіційно / графік
    if n_on or n_off:
        lines.append(f"👥 Люди пишуть:  💡 є — {n_on}  ·  🕯 нема — {n_off}")
        if (r_on, r_off) != (n_on, n_off) and (r_on or r_off):
            lines.append(f"     за 30 хв:  💡 є — {r_on}  ·  🕯 нема — {r_off}")
    if note:
        lines.append(note)
    if alert:
        lines.append(f"🚨 Офіційно: {_label(alert['text'])}{till}")
        if alert.get("src") == "dteksite" and (m := re.search(r"🏠 ([^\n]*)", alert["text"])):
            lines.append(f"🏠 {m.group(1)} (сайт ДТЕК)")
    if provider == "dtek" and not (alert and alert.get("src") == "dteksite") and (share := await _group_share(grp, now)):
        lines.append(share)  # екстрене хоч на кількох адресах групи — пуш усім не шлемо, але показуємо завжди
    if city_em:
        lines.append("🚨 У Дніпрі діють екстрені відключення — графік зараз не діє")
    if not city_em and not is_emergency_alert(alert) and (sched_line := _schedule_line(today_slots, slot, t)):
        lines.append(sched_line)
    return "\n".join(lines).rstrip()


FRESH_SEC = 15 * 60  # дані перевірки адрес на сайті ДТЕК вважаємо свіжими 15 хв


async def _group_share(grp: str, now: datetime) -> str | None:
    """«🏠 Сайт ДТЕК: екстрене у 30 з 265 перевірених адрес групи, до 18:05» (дані ≤15 хв)."""
    s = json.loads(await db.kv_get(f"site_grp:{grp}") or "null")
    if not s or not s.get("off") or now.timestamp() - s.get("ts", 0) > FRESH_SEC:
        return None
    end = re.match(r"(\d{1,2}:\d{2})(?:\s+(\d{2}\.\d{2}))?", s.get("end") or "")
    until = f", до <b>{_short_until(' '.join(x for x in end.groups() if x), now)}</b>" if end else ""
    streets = s.get("streets") or []
    where = (f" · {escape(', '.join(streets))}" + (" та ін." if s["off"] > len(streets) else "")) if streets else ""
    return f"🏠 Сайт ДТЕК: екстрене у {s['off']} з {s['total']} перевірених адрес групи{until}{where}"


async def house_block(user: dict) -> str | None:
    """Перший блок «Зараз» для того, хто вказав адресу: що сайт ДТЕК каже саме про його будинок."""
    if not user.get("street") or not user.get("house") or (user.get("provider") or "dtek") != "dtek":
        return None
    seen = json.loads(await db.kv_get(f"house_seen:{user['id']}") or "null")
    addr = f"{escape(user['street'])}, {escape(user['house'])}"
    now = datetime.now(TZ)
    if not seen or now.timestamp() - seen.get("ts", 0) > FRESH_SEC:
        return f"🏠 <b>Твій будинок</b> ({addr}): сайт ДТЕК зараз не відповідає — дивись нижче за групою"
    ago = int(now.timestamp() - seen["ts"]) // 60
    checked = f"<i>сайт ДТЕК, {ago} хв тому</i>" if ago else "<i>сайт ДТЕК, щойно</i>"
    if seen.get("kind") and seen["kind"] != "schedule":
        end = re.match(r"(\d{1,2}:\d{2})(?:\s+(\d{2}\.\d{2}))?", seen.get("end") or "")
        until = f" до <b>{_short_until(' '.join(x for x in end.groups() if x), now)}</b>" if end else ""
        return f"🏠 <b>Твій будинок</b> ({addr})\n🔴 {seen.get('reason') or 'відключення'}{until} · {checked}"
    if seen.get("kind") == "schedule":
        return f"🏠 <b>Твій будинок</b> ({addr})\n🔴 відключення за графіком · {checked}"
    text = f"🏠 <b>Твій будинок</b> ({addr})\n✅ відключень немає · {checked}"
    if user.get("grp") and (share := await _group_share(user["grp"], now)):
        # у тебе на сайті чисто, а інші адреси групи вже під екстреним — часто це доходить і до тебе
        text += "\n⚠️ Але по групі вже є екстрене — можуть вимкнути й тебе:\n" + share
    return text


def _short_until(until: str, now: datetime) -> str:
    """«10:18 20.11» -> «10:18» сьогодні, «02:06 (завтра)» — наступного дня."""
    hm_, _, dm = until.partition(" ")
    if not dm or dm == now.strftime("%d.%m"):
        return hm_
    if dm == (now + timedelta(days=1)).strftime("%d.%m"):
        return f"{hm_} (завтра)"
    return until


def _schedule_line(slots, current, t: float) -> str | None:
    if slots is None:
        return None
    if current:
        return f"📅 Графік: відключення до <b>{hm(current.end)}</b>"
    nxt = min((s for s in slots if s.kind == "off" and s.start > t), key=lambda s: s.start, default=None)
    if not nxt:
        return "📅 Графік: сьогодні більше відключень немає"
    left = nxt.start - t
    wait = f"{int(left)} год {round(left % 1 * 60)} хв" if left >= 1 else f"{round(left * 60)} хв"
    return f"📅 Графік: далі <b>{hm(nxt.start)}–{hm(nxt.end)}</b> (через {wait})"
