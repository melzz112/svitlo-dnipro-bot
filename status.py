"""Підсумок «є світло чи ні» для екрана «Зараз»: графік + екстрені (сайт ДТЕК, канали) + що пишуть люди.

Люди пишуть «дали» один раз і далі мовчать, поки світло є. Тому рахуємо не «за останні 30 хв»,
а все, що написали з початку поточного відключення (перший алерт ланцюжка або початок слоту за графіком),
по одному — останньому — голосу від людини. «Продовжено до 10:18» від ДТЕК не перекреслює «дали»,
які люди написали вже після початку відключення.
"""
import re
import time
from datetime import datetime, timedelta

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


def _label(alert_text: str) -> str:
    m = re.search(r"<b>(.*?)</b>", alert_text)
    return (m.group(1) if m else "відключення").lower()


def _ago(ts: float) -> str:
    m = int(time.time() - ts) // 60
    return f"{m} хв тому" if m < 60 else f"{m // 60} год {m % 60} хв тому"


def _emergency_head(n_on: int, n_off: int, alert: dict | None) -> tuple[str, str]:
    """Офіційно екстрене по групі (сайт ДТЕК / канали) або екстрені в усьому місті: «нема», поки люди
    масово не пишуть «дали» (рахуються лише голоси з початку відключення)."""
    if n_on >= 2 and n_off == 0:
        return "🟢 <b>Світло є</b>", "люди пишуть, що дали"
    if n_on >= 2 and n_on >= 3 * n_off:
        return "🟢 <b>Світло є у більшості</b>", "дехто ще пише, що немає"
    if n_on >= 2 and n_on >= n_off:
        return "🟡 <b>Частково є світло</b>", "багатьом дали, але не всім"
    confirmed = " · люди підтверджують" if n_off >= 2 else ""
    if alert:
        return "🔴 <b>Світла немає</b>", _label(alert["text"]) + confirmed
    if n_off and n_off > n_on:
        return "🔴 <b>Світла немає</b>", "екстрені відключення" + confirmed
    return "🟠 <b>Екстрені відключення</b>", "світло можуть вимкнути будь-якої миті"


async def verdict(grp: str, provider: str) -> str:
    now = datetime.now(TZ)
    sch = await get_schedule(provider=provider)
    city_em = await db.get_emergency(provider)  # у місті діють екстрені — графік не діє
    t = now_h()
    slot = None if city_em else next(
        (s for s in sch.get("today", {}).get(grp, []) if s.kind == "off" and s.start <= t < s.end), None
    )
    alert, alert_start = await _outage_chain(grp, provider, now)

    # з якого моменту рахуємо голоси людей
    starts = []
    if alert_start:
        starts.append(alert_start)
    if slot:
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        starts.append((midnight + timedelta(hours=slot.start)).timestamp())
    since = min(starts) if starts else now.timestamp() - NO_OUTAGE_WINDOW
    since = max(since, now.timestamp() - MAX_WINDOW)

    people = await db.crowd_people(grp, provider, since)
    on_ts = [ts for ev, ts in people.values() if ev == "on"]
    off_ts = [ts for ev, ts in people.values() if ev == "off"]
    if len(on_ts) >= 2:
        # «вирубили» о 06:16, а з 07:19 люди пишуть «дали» — ранні «нема» вже застарілі
        # (ці люди просто не написали вдруге). Рахуємо «нема», написані після першого «дали».
        first_on = min(on_ts)
        off_ts = [ts for ts in off_ts if ts >= first_on]
    n_on, n_off = len(on_ts), len(off_ts)

    # 1) підсумок (великий рядок) + пояснення: люди важать більше за графік/канали, коли їх хоча б двоє
    today_slots = sch.get("today", {}).get(grp)
    if alert or city_em:
        head, sub = _emergency_head(n_on, n_off, alert)
    elif n_on >= 2 and n_off == 0:
        head, sub = "🟢 <b>Світло є</b>", "люди пишуть, що дали"
    elif n_on >= 2 and n_on >= 3 * n_off:
        head, sub = "🟢 <b>Світло є у більшості</b>", "дехто ще пише, що немає"
    elif n_on >= 2 and n_off >= 1 and n_on >= n_off:
        head, sub = "🟡 <b>Частково є світло</b>", "багатьом дали, але не всім"
    elif n_off >= 2 and n_on >= 1:
        head, sub = "🟡 <b>Здебільшого немає</b>", "лише частині адрес дали"
    elif n_off >= 2:
        head, sub = "🔴 <b>Світла немає</b>", "люди підтверджують"
    elif n_on == 1 and n_off == 1:
        head, sub = "🟡 <b>Неоднозначно</b>", "повідомлення розходяться"
    elif slot:
        head, sub = "🔴 <b>Світла немає</b>", "за графіком"
    elif today_slots is None:
        head, sub = "❔ <b>Невідомо</b>", "графіка немає, люди не пишуть"
    else:
        head, sub = "🟢 <b>Світло має бути</b>", "за графіком зараз без відключень"

    lines = [head, f"<i>{sub}</i>", ""]
    # 2) факти: люди / офіційно / графік
    if n_on or n_off:
        lines.append(f"👥 Люди пишуть:  💡 є — {n_on}  ·  🕯 нема — {n_off}")
    if alert:
        until = f" до <b>{_short_until(alert['until'], now)}</b>" if alert["until"] else ""
        lines.append(f"🚨 Офіційно: {_label(alert['text'])}{until}")
        if alert.get("src") == "dteksite" and (m := re.search(r"🏠 ([^\n]*)", alert["text"])):
            lines.append(f"🏠 {m.group(1)} (сайт ДТЕК)")
    if city_em:
        lines.append("🚨 У Дніпрі діють екстрені відключення — графік зараз не діє")
    elif not alert and n_off >= 2 and not slot:
        lines.append("❓ Ні в графіку, ні в каналах відключення немає — можливо, локальна аварія")
    if not city_em and (sched_line := _schedule_line(today_slots, slot, t)):
        lines.append(sched_line)
    return "\n".join(lines).rstrip()


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
