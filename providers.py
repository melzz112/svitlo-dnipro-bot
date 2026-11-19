"""Джерела графіків. Формат: {"today": {"1.1": [Slot,...], ...}, "tomorrow": {...}}"""
import json
import logging
import random
import re
import time
from dataclasses import dataclass

import httpx

from datetime import datetime, timedelta

from config import CITY, GROUPS, SOURCE, TZ

log = logging.getLogger(__name__)


@dataclass
class Slot:
    start: float  # години, 13.5 = 13:30
    end: float
    kind: str = "off"  # off — точно, maybe — можливо


Schedule = dict[str, dict[str, list[Slot]]]

YASNO_URL = "https://api.yasno.com.ua/api/v1/pages/home/schedule-turn-off-electricity"
DTEK_URL = "https://www.dtek-dnem.com.ua/ua/shutdowns"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

last_update: str | None = None  # "19.11.2026 21:01" з сайту ДТЕК

# Значення години в JSON ДТЕК -> які півгодини без світла і якого типу
_DTEK_HALVES = {
    "no": ("off", "off"),
    "first": ("off", None),
    "second": (None, "off"),
    "maybe": ("maybe", "maybe"),
    "mfirst": ("maybe", None),
    "msecond": (None, "maybe"),
}


def parse_dtek_hours(hours: dict[str, str]) -> list[Slot]:
    """{"1": "yes", "2": "no", ...} (година 1 = 00:00–01:00) -> злиті інтервали."""
    halves: list[str | None] = []
    for h in range(1, 25):
        halves.extend(_DTEK_HALVES.get(hours.get(str(h), "yes"), (None, None)))
    slots: list[Slot] = []
    for i, kind in enumerate(halves):
        if kind is None:
            continue
        if slots and slots[-1].kind == kind and slots[-1].end == i / 2:
            slots[-1].end = (i + 1) / 2
        else:
            slots.append(Slot(i / 2, (i + 1) / 2, kind))
    return slots


async def fetch_dtek() -> Schedule:
    global last_update
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": UA}, follow_redirects=True) as c:
        r = await c.get(DTEK_URL)
        r.raise_for_status()
    m = re.search(r"DisconSchedule\.fact\s*=\s*(\{.*?\})\s*</script>", r.text, re.S)
    if not m:
        raise ValueError("DisconSchedule.fact не знайдено на сторінці ДТЕК")
    fact = json.loads(m.group(1))
    last_update = fact.get("update")
    today = int(fact["today"])
    data = fact.get("data", {})
    out: Schedule = {}
    for day, ts in (("today", today), ("tomorrow", today + 86400)):
        groups = data.get(str(ts))
        if groups:
            out[day] = {g.removeprefix("GPV"): parse_dtek_hours(h) for g, h in groups.items()}
        else:
            out[day] = {}
    return out


async def fetch_yasno() -> Schedule:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(YASNO_URL)
        r.raise_for_status()
        data = r.json()
    comp = next(x for x in data.get("components", []) if "dailySchedule" in x)
    city = comp["dailySchedule"][CITY]
    out: Schedule = {}
    for day in ("today", "tomorrow"):
        groups = (city.get(day) or {}).get("groups") or {}
        out[day] = {
            g: [
                Slot(s["start"], s["end"], "off" if s.get("type") == "DEFINITE_OUTAGE" else "maybe")
                for s in slots
            ]
            for g, slots in groups.items()
        }
    return out


def demo_schedule() -> Schedule:
    """Стабільні фейкові дані на день — щоб тестити меню без API."""
    rnd = random.Random(time.strftime("%Y%m%d"))
    out: Schedule = {}
    for day in ("today", "tomorrow"):
        out[day] = {}
        for i, g in enumerate(GROUPS):
            base = (i * 2 + rnd.randint(0, 2)) % 24
            out[day][g] = [Slot(float(h % 24), float(min(h % 24 + 4, 24))) for h in (base, base + 12)]
    return out


_cache: tuple[float, Schedule] | None = None


FETCHERS = {"dtek": fetch_dtek, "yasno": fetch_yasno}


async def cek_schedule() -> Schedule:
    """ЦЕК не дає даних на сайті — графік збирає userbot з постів у Telegram-каналах (db.save_cek_schedule)."""
    import db  # тут, щоб providers не тягнув БД при імпорті

    today = datetime.now(TZ).date()
    out: Schedule = {}
    for day, d in (("today", today), ("tomorrow", today + timedelta(days=1))):
        groups, _ = await db.get_cek_schedule(d.isoformat())
        out[day] = {g: [Slot(a, b) for a, b in v] for g, v in groups.items()}
    return out


async def update_label(provider="dtek") -> str | None:
    """Рядок «🕒 Оновлено …» для екрана графіка і пушів."""
    if provider == "cek":
        import db

        _, upd = await db.get_cek_schedule(datetime.now(TZ).date().isoformat())
        return f"🕒 Графік ЦЕК з Telegram-каналів, оновлено {upd}" if upd else None
    return f"🕒 Оновлено ДТЕК: {last_update}" if last_update else None


async def get_schedule(force=False, provider="dtek") -> Schedule:
    """Кеш 5 хв; force (кнопка 🔄) — не частіше раз на хвилину, сторінка ДТЕК ~1 МБ."""
    if provider == "cek":
        return await cek_schedule()
    global _cache
    if _cache:
        age = time.time() - _cache[0]
        if age < 60 or (not force and age < 300):
            return _cache[1]
    if SOURCE == "demo":
        sch = demo_schedule()
    else:
        try:
            sch = await FETCHERS.get(SOURCE, fetch_dtek)()
        except Exception as e:  # сайт змінився/лежить — не падаємо
            log.warning("%s fetch failed: %r", SOURCE, e)
            if _cache:
                return _cache[1]
            return {"today": {}, "tomorrow": {}}
    _cache = (time.time(), sch)
    return sch
