"""Джерела графіків. Формат: {"today": {"1.1": [Slot,...], ...}, "tomorrow": {...}}"""
import asyncio
import hashlib
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html import escape, unescape

import httpx

import dtek_http
import groups as G
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


def _fact_groups(day_data: dict | None, provider: str = "dtek") -> dict[str, dict]:
    """{"GPV14.1": {...}, "GPV1001.1": {...}} -> {"14.1": години} лише цього постачальника (ЦЕК — GPV100X.Y)."""
    rx = r"GPV100(\d{1,2}\.\d{1,2})" if provider == "cek" else r"GPV(\d{1,2}\.\d{1,2})"
    out = {}
    for key, hours in (day_data or {}).items():
        if (m := re.fullmatch(rx, str(key))) and (g := G.norm(m.group(1))) and isinstance(hours, dict):
            out[g] = hours
    return {g: out[g] for g in G.sort(out)}


async def fetch_dtek(today: date) -> dict[str, DaySchedule]:
    import dtek_http

    r = await dtek_http.page()
    m = re.search(r"DisconSchedule\.fact\s*=\s*(\{.*?\})\s*;?\s*</script>", r.text, re.S)
    if not m:
        await dtek_http.reset()
        dtek_http.backoff("сторінка без DisconSchedule.fact")
        raise ValueError("DisconSchedule.fact не знайдено на сторінці ДТЕК")
    fact = json.loads(m.group(1))
    set_notice("dtek", extract_attention(r.text))
    by_date = {datetime.fromtimestamp(int(ts), TZ).date(): groups for ts, groups in fact["data"].items()}
    out = {}
    for day, d in schedule_dates(today).items():
        groups = {g: parse_dtek_hours(hours) for g, hours in _fact_groups(by_date.get(d)).items()}
        if groups:
            G.register(groups)
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
        for group in G.sort(k for k in data if G.is_group(k)):
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
        G.register(result.groups)
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
    """Ключ групи в JSON outage-data-ua: ДТЕК 'GPV14.1', ЦЕК 'GPV1001.1'."""
    return f"GPV100{grp}" if provider == "cek" else f"GPV{grp}"


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
        groups = {g: parse_dtek_hours(hours) for g, hours in _fact_groups(by_date.get(d), provider).items()}
        if groups:
            G.register(groups)
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
# графік на сайті ДТЕК змінюється рідко, а сторінка важка й під антиботом — смикаємо її рідше
SITE_CACHE_BY_SOURCE = {"dtek": 30 * 60}
SITE_TIMEOUT = {"dtek": 40, "yasno": 15, "oua": 15}  # з урахуванням повторної спроби
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


async def _health(ok: bool, err: str = ""):
    try:
        import health

        await health.mark("yasno", ok, err)
    except Exception:  # стан джерела — не привід лишити людей без графіка
        log.exception("health yasno")


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
            async with asyncio.timeout(SITE_TIMEOUT[source]):
                if source == "dtek":
                    snapshot = await fetch_dtek(today)
                elif source == "yasno":
                    snapshot = await fetch_yasno(today, provider)
                else:
                    snapshot = await fetch_outage_data_ua(today, provider)
        except (httpx.HTTPError, dtek_http.DtekError, TimeoutError, ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning("%s schedule for %s unavailable: %r", source, provider, exc)
            if source == "yasno":
                await _health(False, f"{provider}: {exc!r}")
            _site_cache[key] = (today, time.monotonic() + SITE_RETRY_SEC[source], None)
            return _stale(key, today)
        _site_cache[key] = (today, time.monotonic() + SITE_CACHE_BY_SOURCE.get(source, SITE_CACHE_SEC), snapshot)
        _site_last_good[key] = (today, time.monotonic(), snapshot)
        if source == "yasno":
            await _health(True)
        if source == "yasno":
            try:
                await track_yasno(provider, today, snapshot)
            except Exception:  # знімки — не привід лишити людей без графіка
                log.exception("YASNO: порівняння графіків %s не вдалось", provider)
        return snapshot


async def get_schedule_days(provider="dtek", *, today: date | None = None) -> dict[str, DaySchedule]:
    if today is None:
        today = datetime.now(TZ).date()
    if SOURCE == "demo":
        return {day: DaySchedule(groups, "demo") for day, groups in demo_schedule().items()}

    import dtek_http

    dates = schedule_dates(today)
    # YASNO — головне джерело (готовий API, без антибота); сайт ДТЕК — лише якщо більше ніде немає й він не на паузі
    days = dict(await _site_schedule("yasno", provider, today) or {})
    if provider == "dtek" and dates.keys() - days.keys():  # у outage-data-ua фактичний графік лише ДТЕК
        for day, schedule in (await _site_schedule("oua", provider, today) or {}).items():
            days.setdefault(day, schedule)
    if provider == "dtek" and SOURCE == "dtek" and dates.keys() - days.keys() and not dtek_http.backoff_left():
        for day, schedule in (await _site_schedule("dtek", provider, today) or {}).items():
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
        # вікно «Шановні клієнти!» з сайту ДТЕК приносить перевірка адрес (site_check) — сторінку окремо не смикаємо
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


# ---------- YASNO: що змінилось у графіку кожної черги ----------


@dataclass
class ScheduleChange:
    provider: str
    day: str  # today | tomorrow
    date: date
    grp: str
    old: list[Slot] | None  # None — графіка на цей день для черги ще не було
    new: list[Slot]
    updated: str | None = None

    @property
    def added(self) -> list[Slot]:
        return _minus(self.new, self.old or [])

    @property
    def removed(self) -> list[Slot]:
        return _minus(self.old or [], self.new)


def _halves(slots: list[Slot]) -> dict[int, str]:
    """Слоти -> {номер півгодини: off|maybe}: різницю рахуємо по півгодинах, а не цілими інтервалами."""
    out = {}
    for s in slots:
        for i in range(int(round(s.start * 2)), int(round(s.end * 2))):
            out[i] = s.kind
    return out


def _minus(a: list[Slot], b: list[Slot]) -> list[Slot]:
    """Півгодини з a, яких (того ж типу) немає в b, — злиті в інтервали."""
    ha, hb = _halves(a), _halves(b)
    out: list[Slot] = []
    for i in sorted(k for k, v in ha.items() if hb.get(k) != v):
        if out and out[-1].end == i / 2 and out[-1].kind == ha[i]:
            out[-1].end = (i + 1) / 2
        else:
            out.append(Slot(i / 2, (i + 1) / 2, ha[i]))
    return out


def slots_json(slots: list[Slot]) -> str:
    return json.dumps([[round(s.start, 4), round(s.end, 4), s.kind] for s in sorted(slots, key=lambda s: (s.start, s.end))])


def slots_hash(slots: list[Slot]) -> str:
    return hashlib.sha1(slots_json(slots).encode()).hexdigest()[:16]


def _load_slots(raw: str) -> list[Slot]:
    return [Slot(a, b, k) for a, b, k in json.loads(raw or "[]")]


_schedule_changes: list[ScheduleChange] = []


async def track_yasno(provider: str, today: date, days: dict[str, DaySchedule]) -> list[ScheduleChange]:
    """Свіжа відповідь YASNO -> слоти кожної черги проти збереженого знімка (schedule_snapshots).
    Зміни накопичуються для notifier (drain_schedule_changes). Перший знімок постачальника — без пушів."""
    import db

    first = not await db.has_snapshots(provider)
    changes = []
    for day, d in schedule_dates(today).items():
        sched = days.get(day)
        if not sched or not sched.groups:
            continue
        prev = await db.get_snapshots(provider, d.isoformat())
        rows = []
        for grp, slots in sched.groups.items():
            h = slots_hash(slots)
            old = prev.get(grp)
            if old and old["slots_hash"] == h:
                continue
            rows.append((grp, h, slots_json(slots)))
            if not first:
                changes.append(ScheduleChange(provider, day, d, grp, _load_slots(old["slots"]) if old else None,
                                              list(slots), sched.updated))
        if rows:
            await db.save_snapshots(provider, d.isoformat(), rows)
    _schedule_changes.extend(changes)
    return changes


def drain_schedule_changes() -> list[ScheduleChange]:
    out = list(_schedule_changes)
    _schedule_changes.clear()
    return out
