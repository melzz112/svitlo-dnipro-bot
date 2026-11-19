"""Джерела графіків. Формат: {"today": {"1.1": [Slot,...], ...}, "tomorrow": {...}}"""
import logging
import random
import time
from dataclasses import dataclass

import httpx

from config import CITY, GROUPS, SOURCE

log = logging.getLogger(__name__)


@dataclass
class Slot:
    start: float  # години, 13.5 = 13:30
    end: float
    kind: str = "off"  # off — точно, maybe — можливо


Schedule = dict[str, dict[str, list[Slot]]]

YASNO_URL = "https://api.yasno.com.ua/api/v1/pages/home/schedule-turn-off-electricity"


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


async def get_schedule(force=False) -> Schedule:
    global _cache
    if not force and _cache and time.time() - _cache[0] < 300:
        return _cache[1]
    if SOURCE == "demo":
        sch = demo_schedule()
    else:
        try:
            sch = await fetch_yasno()
        except Exception as e:  # API змінилось/лежить — не падаємо
            log.warning("YASNO fetch failed: %r", e)
            if _cache:
                return _cache[1]
            sch = {"today": {}, "tomorrow": {}}
    _cache = (time.time(), sch)
    return sch
