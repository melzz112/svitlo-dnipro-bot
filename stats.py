"""Статистика бота для адміна (/stats). Видає лише тим, хто в ADMIN_IDS; решті бот мовчить, ніби команди немає."""
import json
import os
import shutil
import time
from datetime import datetime, timedelta
from html import escape

import db
from config import DB_PATH, GROUPS, TZ

DAY, WEEK = 86400, 7 * 86400


async def _one(sql, args=()):
    rows = await db._all(sql, args)
    return next(iter(rows[0].values())) if rows else 0


async def _counter(name: str, days_ago: int = 0) -> int:
    d = datetime.now(TZ) - timedelta(days=days_ago)
    return int(await db.kv_get(f"stat:{name}:{d:%Y-%m-%d}") or 0)


def _ago(ts: float) -> str:
    m = int((time.time() - ts) // 60)
    return f"{m} хв тому" if m < 60 else f"{m // 60} год {m % 60} хв тому"


async def build() -> str:
    now = int(time.time())
    total = await _one("SELECT COUNT(*) FROM users")
    with_grp = await _one("SELECT COUNT(*) FROM users WHERE grp IS NOT NULL")
    notify = await _one("SELECT COUNT(*) FROM users WHERE grp IS NOT NULL AND notify_on=1")
    with_addr = await _one("SELECT COUNT(*) FROM users WHERE street!='' AND house!=''")
    new_24 = await _one("SELECT COUNT(*) FROM users WHERE created>=?", (now - DAY,))
    new_7 = await _one("SELECT COUNT(*) FROM users WHERE created>=?", (now - WEEK,))
    act_24 = await _one("SELECT COUNT(*) FROM users WHERE seen>=?", (now - DAY,))
    act_7 = await _one("SELECT COUNT(*) FROM users WHERE seen>=?", (now - WEEK,))
    by_prov = {r["provider"]: r["n"] for r in await db._all("SELECT provider, COUNT(*) n FROM users GROUP BY provider")}
    by_grp = {r["grp"]: r["n"] for r in await db._all(
        "SELECT grp, COUNT(*) n FROM users WHERE grp IS NOT NULL GROUP BY grp")}

    lines = ["📊 <b>Статистика</b> · " + datetime.now(TZ).strftime("%d.%m %H:%M"), ""]
    lines += [
        "👥 <b>Користувачі</b>",
        f"Усього: <b>{total}</b> · з групою: {with_grp} · зі сповіщеннями: {notify} · з адресою: {with_addr}",
        f"Нових: за добу {new_24}, за тиждень {new_7}",
        f"Активних: за добу {act_24}, за тиждень {act_7}",
        f"ДТЕК {by_prov.get('dtek', 0)} · ЦЕК {by_prov.get('cek', 0)}",
    ]
    if by_grp:
        top = sorted(by_grp.items(), key=lambda x: (-x[1], x[0]))
        lines.append("По групах: " + ", ".join(f"{g} — {n}" for g, n in top))
        empty = [g for g in GROUPS if g not in by_grp]
        if empty:
            lines.append(f"Без людей: {', '.join(empty)}")

    push_t, push_y = await _counter("push"), await _counter("push", 1)
    fail_t, blocked_t = await _counter("push_fail"), await _counter("blocked")
    lines += ["", "🔔 <b>Пуші</b>", f"Сьогодні: {push_t} (вчора {push_y}) · помилок {fail_t} · заблокували бота {blocked_t}"]

    since = now - DAY
    chats = await db._all("SELECT chat, COUNT(*) n, COUNT(DISTINCT who) p FROM chat_msgs WHERE ts>=? GROUP BY chat", (since,))
    alerts = await db._all("SELECT provider, src, kind, COUNT(*) n FROM alerts WHERE ts>=? GROUP BY provider, src, kind", (since,))
    reports = await _one("SELECT COUNT(*) FROM reports WHERE ts>=?", (since,))
    lines += ["", "💬 <b>Дані за добу</b>"]
    lines += [f"{escape(r['chat'] or '(без назви)')}: {r['n']} повід. від {r['p']} людей" for r in chats] or ["Повідомлень з чатів немає"]
    if alerts:
        src = {"dteksite": "сайт ДТЕК", "channel": "канали"}
        lines.append("Аварії/події: " + ", ".join(
            f"{src.get(r['src'], r['src'])} {r['provider']} {r['kind']} — {r['n']}" for r in alerts))
    lines.append(f"Звітів «є/нема» кнопками: {reports}")

    lines += ["", "🔌 <b>Джерела</b>"]
    probe = json.loads(await db.kv_get("site_probe") or "null")
    if probe:
        status = f"✅ {probe.get('houses', '?')} будинків" if probe["ok"] else f"❌ {escape(probe.get('err', ''))}"
        lines.append(f"Сайт ДТЕК (адреси): {status} · {_ago(probe['ts'])}")
    else:
        lines.append("Сайт ДТЕК (адреси): ще не перевірявся")
    try:
        from providers import get_schedule_days

        for prov, name in (("dtek", "ДТЕК"), ("cek", "ЦЕК")):
            today = (await get_schedule_days(prov))["today"]
            src = {"dtek": "сайт ДТЕК", "yasno": "YASNO", "oua": "outage-data-ua", "channel": "канали"}.get(today.source, today.source)
            lines.append(f"Графік {name}: {src}" + (f" · оновлено {escape(today.updated)}" if today.updated else "") +
                         f" · груп {len(today.groups)}")
    except Exception as e:
        lines.append(f"Графіки: не вдалось дізнатись ({escape(repr(e)[:60])})")
    last_msg = await _one("SELECT MAX(ts) FROM chat_msgs")
    if last_msg:
        lines.append(f"Остання звістка з чату: {_ago(last_msg)}")

    lines += ["", "🖥 <b>Сервер</b>"]
    try:
        du = shutil.disk_usage(os.path.dirname(DB_PATH) or ".")
        lines.append(f"Диск: зайнято {du.used / du.total:.0%}, вільно {du.free / 2**30:.1f} ГБ")
    except OSError:
        pass
    if os.path.exists(DB_PATH):
        lines.append(f"База: {os.path.getsize(DB_PATH) / 1024:.0f} КБ")
    return "\n".join(lines)
