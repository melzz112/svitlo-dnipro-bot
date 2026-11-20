"""Підсумок «є світло чи ні» для екрана «Зараз»: графік + аварійні з каналів + що пишуть люди.

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
    """Активні аварійні алерти групи: (останній — для причини, час першого — початок відключення)."""
    active = [a for a in await db.off_alerts(grp, provider) if _is_active(a, now)]
    if not active:
        return None, None
    return active[-1], active[0]["ts"]


def _label(alert_text: str) -> str:
    m = re.search(r"<b>(.*?)</b>", alert_text)
    return (m.group(1) if m else "відключення").lower()


def _ago(ts: float) -> str:
    m = int(time.time() - ts) // 60
    return f"{m} хв тому" if m < 60 else f"{m // 60} год {m % 60} хв тому"


async def verdict(grp: str, provider: str) -> str:
    now = datetime.now(TZ)
    sch = await get_schedule(provider=provider)
    t = now_h()
    slot = next((s for s in sch.get("today", {}).get(grp, []) if s.kind == "off" and s.start <= t < s.end), None)
    alert, alert_start = await _outage_chain(grp, provider, now)
    official_off = bool(alert or slot)

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

    # 1) підсумок: люди важать більше за графік/канали, коли їх хоча б двоє
    if n_on >= 2 and n_off == 0:
        head = "🟢 <b>Світло є</b> — люди пишуть, що дали"
    elif n_on >= 2 and n_on >= 3 * n_off:
        head = "🟢 <b>Світло є у більшості</b> — але дехто ще пише, що немає"
    elif n_on >= 2 and n_off >= 1 and n_on >= n_off:
        head = "🟡 <b>Частково є світло</b> — багатьом дали, але не всім"
    elif n_off >= 2 and n_on >= 1:
        head = "🟡 <b>Здебільшого немає</b> — лише частині адрес дали"
    elif n_off >= 2:
        head = "🔴 <b>Світла немає</b> — люди підтверджують"
    elif n_on == 1 and n_off == 1:
        head = "🟡 <b>Неоднозначно</b> — повідомлення розходяться"
    elif official_off:
        head = "🔴 <b>Світла немає</b>"
    elif sch.get("today", {}).get(grp) is None:
        head = "❔ <b>Невідомо</b> — графіка немає, люди не пишуть"
    else:
        head = "🟢 <b>Світло має бути</b>"

    lines = [head]
    people_say_on = n_on >= 2 and n_on >= n_off
    # 2) причина / що офіційно
    if alert:
        until = f", орієнтовно до <b>{alert['until']}</b>" if alert["until"] else ""
        prefix = "🚨 Офіційно ще" if people_say_on else "🚨 Причина:"
        lines.append(f"{prefix} {_label(alert['text'])}{until}")
    elif slot:
        prefix = "📅 За графіком ще" if people_say_on else "📅 Причина:"
        lines.append(f"{prefix} відключення до <b>{hm(slot.end)}</b>")
    elif n_off >= 2:
        lines.append("❓ Ні в графіку, ні в каналах відключення немає — можливо, локальна аварія")
    # 3) люди
    if n_on or n_off:
        period = "з початку відключення" if starts else "за 3 год"
        lines.append(f"👥 Люди {period}: 💡 «є» — {n_on}, 🕯 «нема» — {n_off}")
        if on_ts and people_say_on:
            lines.append(f"🕐 Останнє «дали» — {_ago(max(on_ts))}")
    return "\n".join(lines)
