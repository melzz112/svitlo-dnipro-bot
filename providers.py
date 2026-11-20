"""Джерела графіків. Формат: {"today": {"1.1": [Slot,...], ...}, "tomorrow": {...}}"""
import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html import escape

import httpx

from config import CITY, GROUPS, SOURCE, TZ

log = logging.getLogger(__name__)


@dataclass
class Slot:
    start: float  # години, 13.5 = 13:30
    end: float
    kind: str = "off"  # off — точно, maybe — можливо


Schedule = dict[str, dict[str, list[Slot]]]

DTEK_URL = "https://www.dtek-dnem.com.ua/ua/shutdowns"
YASNO_URL = "https://app.yasno.ua/api/blackout-service/public/shutdowns/regions/{region}/dsos/{dso}/planned-outages"
YASNO_OPERATORS = {"dnipro": {"dtek": (3, 301), "cek": (3, 303)}, "kyiv": {"dtek": (25, 902)}}


@dataclass
class DaySchedule:
    groups: dict[str, list[Slot]] = field(default_factory=dict)
    source: str = "channel"
    updated: str | None = None
    emergency: bool = False

    def label(self) -> str | None:
        if self.source == "demo" or not self.groups:
            return None
        name = {"dtek": "сайт ДТЕК", "yasno": "сайт YASNO", "channel": "Telegram-канали"}[self.source]
        if self.updated:
            return f"🕒 Оновлено {escape(self.updated)} · {name}"
        return f"ℹ️ Джерело: {name}"


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


def schedule_dates(today: date) -> dict[str, date]:
    return {"today": today, "tomorrow": today + timedelta(days=1)}


async def fetch_dtek(today: date) -> dict[str, DaySchedule]:
    async with httpx.AsyncClient(timeout=5, follow_redirects=True) as c:
        r = await c.get(DTEK_URL)
        r.raise_for_status()
    m = re.search(r"DisconSchedule\.fact\s*=\s*(\{.*?\})\s*;?\s*</script>", r.text, re.S)
    if not m:
        raise ValueError("DisconSchedule.fact не знайдено на сторінці ДТЕК")
    fact = json.loads(m.group(1))
    by_date = {datetime.fromtimestamp(int(ts), TZ).date(): groups for ts, groups in fact["data"].items()}
    out = {}
    for day, d in schedule_dates(today).items():
        groups = {
            g: parse_dtek_hours(hours)
            for g in GROUPS
            if (hours := (by_date.get(d) or {}).get(f"GPV{g}"))
        }
        if groups:
            out[day] = DaySchedule(groups, "dtek", fact.get("update"))
    if not out:
        raise ValueError("На сайті ДТЕК немає графіка на сьогодні або завтра")
    return out


_YASNO_SLOT_TYPES = {"Definite": "off", "Possible": "maybe", "NotPlanned": None}


def _yasno_slots(slots: list[dict]) -> list[Slot] | None:
    if not slots:
        return None
    out = []
    for slot in slots:
        start, end, kind = slot["start"], slot["end"], slot["type"]
        if kind not in _YASNO_SLOT_TYPES or not 0 <= start < end <= 1440:
            return None
        if outage := _YASNO_SLOT_TYPES[kind]:
            out.append(Slot(start / 60, end / 60, outage))
    return out


def parse_yasno_schedule(data: dict, today: date) -> dict[str, DaySchedule]:
    out = {}
    waiting = False
    for day, d in schedule_dates(today).items():
        result = DaySchedule(source="yasno")
        stamps = []
        for group in GROUPS:
            entry = data.get(group) or {}
            info = entry.get(day) or {}
            if not info.get("date") or date.fromisoformat(info["date"][:10]) != d:
                continue
            status = info.get("status")
            if status == "WaitingForSchedule":
                waiting = True
                continue
            if status == "EmergencyShutdowns":
                result.emergency = True
                out[day] = result
                continue
            if status == "NoOutages":
                slots = []
            elif status == "ScheduleApplies":
                slots = _yasno_slots(info.get("slots") or [])
                if slots is None:
                    continue
            else:
                continue
            result.groups[group] = slots
            out[day] = result
            if stamp := entry.get("updatedOn"):
                stamps.append(datetime.fromisoformat(stamp))
        if stamps:
            result.updated = max(stamps).astimezone(TZ).strftime("%d.%m.%Y %H:%M")
    if not out and not waiting:
        raise ValueError("YASNO не повернув актуальних даних про графік")
    return out


async def fetch_yasno(today: date, provider: str) -> dict[str, DaySchedule]:
    city = CITY if SOURCE == "yasno" and provider == "dtek" else "dnipro"
    region, dso = YASNO_OPERATORS[city][provider]
    async with httpx.AsyncClient(timeout=5) as c:
        r = await c.get(YASNO_URL.format(region=region, dso=dso))
        r.raise_for_status()
    return parse_yasno_schedule(r.json(), today)


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


SITE_CACHE_SEC = 60
SITE_RETRY_SEC = {"dtek": 30 * 60, "yasno": 60}
_site_cache: dict[tuple[str, str], tuple[date, float, dict[str, DaySchedule] | None]] = {}
_site_locks: dict[tuple[str, str], asyncio.Lock] = {}


async def channel_schedule(provider: str, day: date) -> DaySchedule:
    import db

    groups, stamp = await db.get_channel_schedule(provider, day.isoformat())
    return DaySchedule({g: [Slot(a, b) for a, b in spans] for g, spans in groups.items()}, "channel", stamp)


async def _site_schedule(source: str, provider: str, today: date) -> dict[str, DaySchedule] | None:
    key = (source, provider)
    async with _site_locks.setdefault(key, asyncio.Lock()):
        if cached := _site_cache.get(key):
            day, expires, snapshot = cached
            if time.monotonic() < expires and (snapshot is None or day == today):
                return snapshot
        try:
            async with asyncio.timeout(7):
                snapshot = await (fetch_dtek(today) if source == "dtek" else fetch_yasno(today, provider))
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning("%s schedule for %s unavailable: %r", source, provider, exc)
            _site_cache[key] = (today, time.monotonic() + SITE_RETRY_SEC[source], None)
            return None
        _site_cache[key] = (today, time.monotonic() + SITE_CACHE_SEC, snapshot)
        return snapshot


async def get_schedule_days(provider="dtek", *, today: date | None = None) -> dict[str, DaySchedule]:
    if today is None:
        today = datetime.now(TZ).date()
    if SOURCE == "demo":
        return {day: DaySchedule(groups, "demo") for day, groups in demo_schedule().items()}

    dates = schedule_dates(today)
    days = {}
    if provider == "dtek" and SOURCE != "yasno":
        days.update(await _site_schedule("dtek", provider, today) or {})
    if dates.keys() - days.keys():
        for day, schedule in (await _site_schedule("yasno", provider, today) or {}).items():
            days.setdefault(day, schedule)
    for day, d in dates.items():
        if day not in days:
            days[day] = await channel_schedule(provider, d)
    return days


async def get_schedule(provider="dtek") -> Schedule:
    return {day: schedule.groups for day, schedule in (await get_schedule_days(provider)).items()}
