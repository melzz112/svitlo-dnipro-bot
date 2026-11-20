"""Сайт ЦЕК, сторінка «Відключення»: стрічка оголошень <p>…</p>, найновіше — зверху.

Розмітка (20.11.2026):
    <p>📢 Повідомляємо про зміни в ГПВ на 20 ЛИСТОПАДА, … з 13:45 додатково</p>
    <p>відключаються черги: 5.1, 5.2;</p>
    <p>📢 Повідомляємо про зміни у ГПВ 20 ЛИСТОПАДА … з 14:00 до 24:00 діятимуть наступні черги:</p>
    <p>🔹 Черга 1.1<br />16:30-23:30</p>
    <p>🔹 Черга 3.1<br />13:00-16:30<br />23:30-24:00</p> …

Читаємо згори вниз:
1. Графік на дату — НАЙВИЩИЙ блок «зміни у ГПВ / застосовуватимуться відключення … черг» з цією датою
   і рядками «🔹 Черга X.Y» під ним. Нижчі блоки — старіші версії, ігноруємо.
2. «з HH:MM додатково відключаються черги: X.Y, …» ВИЩЕ графіка на ту саму дату — додаємо до графіка.
   Нижче графіка — вже врахована в ньому, ігноруємо. Кінця в такому оголошенні немає: зливаємо з найближчим
   плановим відключенням черги (якщо воно починається протягом 4 год), інакше — 4 год, позначка «можливо».
3. «застосовуються ЕКСТРЕНІ ВІДКЛЮЧЕННЯ» для м. Дніпро ВИЩЕ актуального графіка й без «скасовуються ЕКСТРЕНІ…»
   над ним — у місті екстрені, ГПВ не діють.

Перевірка вручну: python cek_site.py
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from html import unescape

import httpx

from config import GROUPS, TZ
from parse import INTERVAL_RE, _post_date

CEK_URL = "https://cek.dp.ua/index.php/cpojivaham/vidkliuchennia.html"

GROUP_LINE_RE = re.compile(r"Черг[аи]\s*№?\s*([1-6])\.([12])(?!\d)", re.I)
SCHEDULE_HEAD_RE = re.compile(r"(гпв|графік)", re.I)
SCHEDULE_HEAD_KEY_RE = re.compile(r"діятимуть|застосовуватим|відключення наступних черг|наступні черги", re.I)
EXTRA_RE = re.compile(r"додатково", re.I)
EMERGENCY_ON_RE = re.compile(r"застосову\w*\s+(?:\w+\s+){0,2}екстрен\w*\s+відключ", re.I)
EMERGENCY_OFF_RE = re.compile(r"скасову\w*\s+(?:\w+\s+){0,2}екстрен\w*\s+відключ|екстрен\w*\s+відключ\w*\s+скасован", re.I)
CITY_RE = re.compile(r"дніпр|днепр", re.I)
OTHER_PLACE_RE = re.compile(r"кам.янськ|павлоград|самар|нікопол|марганц|кривий|жовті води|синельников", re.I)
TIME_RE = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?![\d:])")

EXTRA_MERGE_H = 4  # «додатково з 13:45» зливаємо з плановим відключенням, що починається в межах 4 год
EXTRA_DEFAULT_H = 4  # якщо такого немає — 4 год, позначка «можливо»


def paragraphs(html: str) -> list[str]:
    """<p>…</p> -> текст: <br> -> новий рядок, теги геть, &nbsp; -> пробіл. Порожні абзаци пропускаємо."""
    out = []
    for inner in re.findall(r"<p\b[^>]*>(.*?)</p>", html, re.S | re.I):
        t = re.sub(r"<br\s*/?>", "\n", inner, flags=re.I)
        t = unescape(re.sub(r"<[^>]+>", "", t)).replace("\xa0", " ")
        t = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in t.splitlines()).strip()
        if t:
            out.append(t)
    return out


@dataclass
class Extra:
    grp: str
    start: float  # години: 13.75 = 13:45
    end: float | None = None  # явно «до HH:MM», якщо було


@dataclass
class CekDay:
    date: date
    groups: dict[str, list[tuple[float, float, str]]] = field(default_factory=dict)  # (початок, кінець, off|maybe)
    header: str = ""
    extras: list[Extra] = field(default_factory=list)  # застосовані «додатково відключаються»
    partial: bool = False  # графіка на дату немає — лише «додатково» по кількох чергах


@dataclass
class CekState:
    days: dict[date, CekDay] = field(default_factory=dict)
    emergency: bool = False  # у м. Дніпро діють екстрені — ГПВ не діють
    emergency_text: str | None = None


def _hours(h: str, m: str) -> float:
    return int(h) + int(m) / 60


def _parse_extra(text: str) -> tuple[float, float | None, list[str]] | None:
    """«з 13:45 додатково відключаються черги: 5.1, 5.2» -> (13.75, None, ["5.1", "5.2"])."""
    times = TIME_RE.findall(text)
    if not times:
        return None
    start = _hours(*times[0])
    m_end = re.search(r"до\s+(\d{1,2}):(\d{2})", text)
    end = _hours(*m_end.groups()) if m_end else None
    after = text.lower().split("черг", 1)[1] if "черг" in text.lower() else ""
    groups = sorted({f"{a}.{b}" for a, b in re.findall(r"(?<![\d.])([1-6])\.([12])(?!\d)", after)} & set(GROUPS))
    return (start, end, groups) if groups else None


def _apply_extra(slots: list[tuple[float, float, str]], ex: Extra) -> list[tuple[float, float, str]]:
    slots = sorted(slots)
    if any(a <= ex.start < b for a, b, _ in slots):
        return slots  # і так відключення в цей час
    if ex.end is not None:
        return sorted(slots + [(ex.start, ex.end, "off")])
    nxt = next(((a, b, k) for a, b, k in slots if a > ex.start), None)
    if nxt and nxt[0] - ex.start <= EXTRA_MERGE_H:
        return sorted([s for s in slots if s != nxt] + [(ex.start, nxt[1], "off")])
    return sorted(slots + [(ex.start, min(ex.start + EXTRA_DEFAULT_H, 24.0), "maybe")])


def parse_page(html: str, today: date | None = None) -> CekState:
    today = today or datetime.now(TZ).date()
    ps = paragraphs(html)
    events = []  # (індекс, тип, дані) згори вниз
    i = 0
    while i < len(ps):
        p = ps[i]
        low = p.lower()
        if EMERGENCY_OFF_RE.search(low):
            events.append((i, "em_off", p))
        elif EMERGENCY_ON_RE.search(low):
            # для м. Дніпро; якщо названі лише інші міста — не наше
            if CITY_RE.search(low) or not OTHER_PLACE_RE.search(low):
                events.append((i, "em_on", p))
        elif EXTRA_RE.search(low) and "черг" in (low + " " + (ps[i + 1].lower() if i + 1 < len(ps) else "")):
            text = p if "черг" in low else p + " " + ps[i + 1]  # речення буває розірване на два <p>
            if parsed := _parse_extra(text):
                events.append((i, "extra", (_post_date(low, today), *parsed)))
            i += 0 if "черг" in low else 1
        elif SCHEDULE_HEAD_RE.search(low) and SCHEDULE_HEAD_KEY_RE.search(low):
            groups: dict[str, list] = {}
            j = i + 1
            while j < len(ps) and (m := GROUP_LINE_RE.search(ps[j])):
                g = f"{m.group(1)}.{m.group(2)}"
                groups[g] = [(_hours(h1, m1), _hours(h2, m2), "off") for h1, m1, h2, m2 in INTERVAL_RE.findall(ps[j])]
                j += 1
            if groups:
                events.append((i, "schedule", (_post_date(low, today), groups, p)))
                i = j - 1
        i += 1

    state = CekState()
    # 1) графік на кожну дату — найвищий блок
    sched_idx: dict[date, int] = {}
    for idx, kind, data in events:
        if kind == "schedule" and data[0] not in state.days:
            d, groups, head = data
            state.days[d] = CekDay(d, {g: list(v) for g, v in groups.items() if g in GROUPS}, head)
            sched_idx[d] = idx
    # 2) «додатково» вище графіка своєї дати (або коли графіка на дату ще немає)
    for idx, kind, data in events:
        if kind != "extra":
            continue
        d, start, end, groups = data
        if d in sched_idx and idx > sched_idx[d]:
            continue  # нижче графіка — вже врахована
        day = state.days.setdefault(d, CekDay(d, partial=True))
        for g in groups:
            ex = Extra(g, start, end)
            day.groups[g] = _apply_extra(day.groups.get(g, []), ex)
            day.extras.append(ex)
    # 3) екстрені: найвища подія «застосовуються/скасовуються» вище актуального (сьогоднішнього) графіка
    top = next(((idx, kind, data) for idx, kind, data in events if kind in ("em_on", "em_off")), None)
    if top and top[1] == "em_on" and (today not in sched_idx or top[0] < sched_idx[today]):
        state.emergency, state.emergency_text = True, top[2]
    return state


def decode(content: bytes, content_type: str = "") -> str:
    """Сайт ЦЕК буває у windows-1251: беремо charset із заголовка/мети, інакше пробуємо utf-8, потім cp1251."""
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I) or re.search(rb'charset=["\']?([\w-]+)', content[:2000])
    enc = (m.group(1).decode() if isinstance(m.group(1), bytes) else m.group(1)) if m else None
    for e in filter(None, (enc, "utf-8", "cp1251")):
        try:
            return content.decode(e)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", "replace")


async def fetch(today: date | None = None) -> CekState:
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
        r = await c.get(CEK_URL)
        r.raise_for_status()
    return parse_page(decode(r.content, r.headers.get("content-type", "")), today)


def _fmt(slots) -> str:
    def hm(h):
        m = round(h * 60)
        return f"{m // 60:02d}:{m % 60:02d}"
    return ", ".join(f"{'~' if k == 'maybe' else ''}{hm(a)}-{hm(b)}" for a, b, k in slots) or "без відключень"


if __name__ == "__main__":
    import asyncio
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    st = asyncio.run(fetch())
    print("Екстрені в м. Дніпро:", "ТАК — " + (st.emergency_text or "") if st.emergency else "ні")
    for d, day in sorted(st.days.items()):
        print(f"\n{d}{' (лише «додатково»)' if day.partial else ''}: {day.header[:90]}")
        for g in GROUPS:
            if g in day.groups:
                print(f"  {g}: {_fmt(day.groups[g])}")
        for ex in day.extras:
            print(f"  + додатково {ex.grp} з {_fmt([(ex.start, ex.start, 'off')]).split('-')[0]}")
