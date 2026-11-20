"""Підсумок «є світло чи ні» для екрана «Зараз»: графік + аварійні з каналів + що пишуть люди."""
import re
from datetime import datetime, timedelta

import db
from config import TZ
from providers import get_schedule
from render import hm, now_h


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


async def _active_off_alert(grp, provider):
    """Аварійне/екстрене з каналів, яке ще не скінчилось (за «до …»), за останні 12 год."""
    a = await db.last_alert("off", grp, 12 * 60, provider)
    if not a:
        return None
    end = _until_dt(a["until"], a["ts"])
    if end is None:  # без часу — вважаємо актуальним 3 год
        return a if datetime.now(TZ).timestamp() - a["ts"] < 3 * 3600 else None
    return a if end > datetime.now(TZ) else None


def _label(alert_text: str) -> str:
    m = re.search(r"<b>(.*?)</b>", alert_text)
    return (m.group(1) if m else "відключення").lower()


async def verdict(grp: str, provider: str) -> str:
    sch = await get_schedule(provider=provider)
    t = now_h()
    slot = next((s for s in sch.get("today", {}).get(grp, []) if s.kind == "off" and s.start <= t < s.end), None)
    alert = await _active_off_alert(grp, provider)
    c = (await db.crowd(30, provider)).get(grp) or {"on": set(), "off": set()}
    n_on, n_off = len(c["on"]), len(c["off"])
    official_off = bool(alert or slot)

    # 1) підсумок: люди важать більше за графік, коли їх хоча б двоє
    if n_on >= 2 and n_off >= 1 and n_on >= n_off:
        head = "🟡 <b>Частково є світло</b> — багатьом дали, але не всім"
    elif n_off >= 2 and n_on >= 1:
        head = "🟡 <b>Здебільшого немає</b> — лише частині адрес дали"
    elif n_on >= 2:
        head = "🟢 <b>Світло є</b> — люди пишуть, що дали"
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
    # 2) причина / офіційно
    if alert:
        until = f", орієнтовно до <b>{alert['until']}</b>" if alert["until"] else ""
        lines.append(f"🚨 Причина: {_label(alert['text'])}{until}")
    elif slot:
        lines.append(f"📅 Причина: відключення за графіком до <b>{hm(slot.end)}</b>")
    elif n_off >= 2:
        lines.append("❓ Ні в графіку, ні в каналах відключення немає — можливо, локальна аварія")
    # 3) люди
    if n_on or n_off:
        lines.append(f"👥 За 30 хв: 💡 «є» — {n_on}, 🕯 «нема» — {n_off}")
    return "\n".join(lines)
