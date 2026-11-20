"""Джерела графіків. Формат: {"today": {"1.1": [Slot,...], ...}, "tomorrow": {...}}"""
import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html import escape, unescape

import httpx

from config import CITY, GROUPS, SOURCE, TZ
from parse import emergency_in_city

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
# Відкритий агрегатор (той самий формат, що віддає сайт ДТЕК, але готовим JSON — без антибота).
OUA_URL = "https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/dnipro.json"


@dataclass
class DaySchedule:
    groups: dict[str, list[Slot]] = field(default_factory=dict)
    source: str = "channel"
    updated: str | None = None
    emergency: bool = False

    def label(self) -> str | None:
        if self.source == "demo" or not self.groups:
            return None
        name = {"dtek": "сайт ДТЕК", "yasno": "сайт YASNO", "oua": "відкриті дані outage-data-ua", "channel": "Telegram-канали"}[self.source]
        if self.updated:
            return f"🕒 Оновлено {escape(self.updated)} · {name}"
        return f"ℹ️ Джерело: {name}"


@dataclass
class Emergency:
    """Режим екстрених відключень у місті: графік не діє, вимикають без попередження."""
    active: bool
    text: str = ""  # офіційний текст ДТЕК («Шановні клієнти!»); з YASNO — порожній
    source: str = "dtek"  # dtek | oua | yasno


# Текст вікна «Шановні клієнти!» з останньої вдалої відповіді: source -> (time.monotonic(), текст або None)
_notices: dict[str, tuple[float, str | None]] = {}


def set_notice(source: str, text: str | None):
    _notices[source] = (time.monotonic(), text)


def extract_attention(html: str) -> str | None:
    """Вікно «Шановні клієнти!» (div.m-attention__text) на сторінці ДТЕК -> звичайний текст, рядки через \\n."""
    i = html.find('class="m-attention__text"')
    if i < 0:
        return None
    start = html.find(">", i) + 1
    depth, body = 1, None
    for m in re.finditer(r"<(/?)div\b[^>]*>", html[start:], re.I):
        depth += -1 if m.group(1) else 1
        if depth == 0:
            body = html[start:start + m.start()]
            break
    if body is None:
        return None
    body = unescape(re.sub(r"<[^>]*>", "", re.sub(r"<br\s*/?>|</p>", "\n", body, flags=re.I)))
    lines = (re.sub(r"\s+", " ", x).strip() for x in body.split("\n"))
    return "\n".join(x for x in lines if x) or None


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
    set_notice("dtek", extract_attention(r.text))
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


def _oua_group_key(provider: str, grp: str) -> str:
    """Ключ групи в JSON outage-data-ua: ДТЕК 'GPV1.1', ЦЕК 'GPV1001.1'."""
    if provider == "cek":
        q, s = grp.split(".")
        return f"GPV100{q}.{s}"
    return f"GPV{grp}"


async def fetch_outage_data_ua(today: date, provider: str) -> dict[str, DaySchedule]:
    """Резервне джерело для ДТЕК: відкритий агрегатор outage-data-ua (готовий JSON сайту ДТЕК, оновлюється часто).
    Беремо лише фактичний графік (fact) на сьогодні/завтра. ЦЕК у fact немає (лише тижневий шаблон у preset),
    тому для ЦЕК джерело не використовується. Немає графіка на завтра — не помилка, просто порожньо."""
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
        r = await c.get(OUA_URL)
        r.raise_for_status()
    data = r.json()
    if (data.get("lastUpdateStatus") or {}).get("ok") is False:
        raise ValueError(f"outage-data-ua: останнє оновлення невдале: {data.get('lastUpdateStatus')}")
    if provider == "dtek":  # dnipro.json — це сторінка ДТЕК, вікно «Шановні клієнти!» теж її
        _oua_notice(data)
    fact = data.get("fact") or {}
    if not fact.get("data"):
        raise ValueError("outage-data-ua: немає fact.data")
    by_date = {datetime.fromtimestamp(int(ts), TZ).date(): groups for ts, groups in fact["data"].items()}
    out: dict[str, DaySchedule] = {}
    for day, d in schedule_dates(today).items():
        groups = {
            g: parse_dtek_hours(hours)
            for g in GROUPS
            if (hours := (by_date.get(d) or {}).get(_oua_group_key(provider, g)))
        }
        if groups:
            out[day] = DaySchedule(groups, "oua", fact.get("update"))
    return out


OUA_MAX_AGE = timedelta(hours=3)  # якщо агрегатор давно не оновлювався — його «екстрені» вже не новина


def _oua_notice(data: dict):
    try:
        updated = datetime.fromisoformat(data["lastUpdated"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, TypeError, ValueError):
        return
    if datetime.now(TZ) - updated > OUA_MAX_AGE:
        log.warning("outage-data-ua не оновлювався з %s — текст ДТЕК не враховуємо", updated)
        return
    att = data.get("attention")
    set_notice("oua", att.get("text") if isinstance(att, dict) else att)


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
SITE_RETRY_SEC = {"dtek": 30 * 60, "yasno": 60, "oua": 5 * 60}
# Якщо сайт на хвилину не відповів — тримаємо його останню відповідь, а не стрибаємо на графік з каналів:
# він може відрізнятись, і бот розсилав би «оновився» туди-сюди.
SITE_STALE_SEC = 3 * 3600
_site_cache: dict[tuple[str, str], tuple[date, float, dict[str, DaySchedule] | None]] = {}
_site_last_good: dict[tuple[str, str], tuple[date, float, dict[str, DaySchedule]]] = {}
_site_locks: dict[tuple[str, str], asyncio.Lock] = {}


def _stale(key, today: date) -> dict[str, DaySchedule] | None:
    if good := _site_last_good.get(key):
        day, at, snapshot = good
        if day == today and time.monotonic() - at < SITE_STALE_SEC:
            return snapshot
    return None


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
                return snapshot if snapshot is not None else _stale(key, today)
        try:
            async with asyncio.timeout(7):
                if source == "dtek":
                    snapshot = await fetch_dtek(today)
                elif source == "yasno":
                    snapshot = await fetch_yasno(today, provider)
                else:
                    snapshot = await fetch_outage_data_ua(today, provider)
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning("%s schedule for %s unavailable: %r", source, provider, exc)
            _site_cache[key] = (today, time.monotonic() + SITE_RETRY_SEC[source], None)
            return _stale(key, today)
        _site_cache[key] = (today, time.monotonic() + SITE_CACHE_SEC, snapshot)
        _site_last_good[key] = (today, time.monotonic(), snapshot)
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
    if provider == "dtek" and dates.keys() - days.keys():  # у outage-data-ua фактичний графік лише ДТЕК
        for day, schedule in (await _site_schedule("oua", provider, today) or {}).items():
            days.setdefault(day, schedule)
    for day, d in dates.items():
        if day not in days:
            days[day] = await channel_schedule(provider, d)
    return days


async def get_emergency(provider="dtek", *, today: date | None = None) -> Emergency | None:
    """Чи діють зараз екстрені відключення в місті. None — жодне джерело не відповіло, стан невідомий.
    ДТЕК: вікно «Шановні клієнти!» з сайту ДТЕК (напряму або через outage-data-ua) — там перелічені міста,
    тож воно точніше; YASNO (статус EmergencyShutdowns на всю ДТЕК-область) — лише якщо вікна не дістали.
    ЦЕК: лише YASNO."""
    if SOURCE == "demo":
        return None
    if today is None:
        today = datetime.now(TZ).date()
    if provider == "dtek" and CITY == "dnipro":
        if SOURCE != "yasno":
            await _site_schedule("dtek", provider, today)
        await _site_schedule("oua", provider, today)
        fresh = [(at, src, text) for src, (at, text) in _notices.items() if time.monotonic() - at < SITE_STALE_SEC]
        if fresh:
            _, src, text = max(fresh)
            return Emergency(emergency_in_city(text), text or "", src)
    yasno = await _site_schedule("yasno", provider, today)
    if yasno and (d := yasno.get("today")):
        return Emergency(d.emergency, "", "yasno")
    return None


async def get_schedule(provider="dtek") -> Schedule:
    return {day: schedule.groups for day, schedule in (await get_schedule_days(provider)).items()}
