"""Світлобот: датчики світла по Дніпру -> фактичний стан кожної групи ДТЕК і ЦЕК («світло є» / «світла немає»).

Кожна точка — чийсь будинок з датчиком. Раз на SVITLOBOT_POLL_INTERVAL беремо токен, потім список точок,
групуємо їх за (постачальник, група) і рахуємо частку точок зі світлом. Між порогами (гістерезис) лишаємо
попередній стан: коли вмикають поступово, група не блимає «є / нема» щокілька хвилин.

Стан групи лежить у kv `svb:{provider}:{grp}` (JSON) — його ж показує екран «Зараз».
Перший прохід після запуску бота лише запам'ятовує стани, без пушів.
"""
import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import aiohttp

import groups as G

from config import (
    SVITLOBOT_CHANNELS_URL,
    SVITLOBOT_CITY_FILTER,
    SVITLOBOT_MIN_SENSORS,
    SVITLOBOT_OFF_THRESHOLD,
    SVITLOBOT_ON_THRESHOLD,
    SVITLOBOT_POLL_INTERVAL,
    SVITLOBOT_REGION,
    SVITLOBOT_TOKEN_URL,
)

log = logging.getLogger(__name__)

# код статусу точки (col[1]) -> наш стан. Змінились коди в джерелі — правимо тільки тут.
STATUS_MAP = {
    "1": "on",  # світло є
    "2": "off",  # світла немає
    "3": "maint",  # технічна перерва датчика — у частку не рахуємо
}

SEP = ";&&&;"
ON, OFF = "on", "off"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/plain, application/json, */*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8,en-US;q=0.7,en;q=0.6",
}
TIMEOUT = aiohttp.ClientTimeout(total=20, connect=10)

# «Група 1.2», «черга 3.1», «гр. 6.2»; якщо підпису немає — просто «1.2» (лише з відомих груп 1.1…6.2)
_GROUP_LABELED = re.compile(r"(?:груп\w*|черг\w*|гр\.?)\s*№?\s*(\d+\.\d+)", re.I)
_GROUP_BARE = re.compile(r"(?<![\d.])(\d+\.\d+)(?![\d.])")
_CEK = re.compile(r"ц[еэ]к", re.I)
_DTEK = re.compile(r"дт[еэ]к", re.I)


class SourceError(Exception):
    """Джерело не відповіло як слід. retry_after — скільки чекати (429 / Retry-After), інакше None."""

    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


@dataclass
class Point:
    id: str
    status: str  # on / off / maint / ""
    changed: datetime | None
    address: str
    slug: str
    subscribers: int
    provider: str | None  # dtek / cek; None — групу не знайшли
    grp: str | None
    lat: float | None = None
    lon: float | None = None


@dataclass
class GroupStat:
    provider: str
    grp: str
    on: int = 0
    off: int = 0
    maint: int = 0

    @property
    def alive(self) -> int:
        return self.on + self.off

    @property
    def ratio(self) -> float | None:
        return self.on / self.alive if self.alive else None


# --- парсинг ---
def _iso(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def extract_group(*texts: str) -> tuple[str, str] | None:
    """Тексти рядка -> (provider, група) або None. Без оператора — ДТЕК."""
    for text in texts:
        if not text:
            continue
        m = _GROUP_LABELED.search(text)
        grp = G.norm(m.group(1)) if m else None  # «група 14.1» — будь-яка відома черга (з даних ДТЕК/YASNO)
        grp = grp if G.is_known(grp) else None
        if grp is None:
            grp = next((g for g in _GROUP_BARE.findall(text) if G.is_known(g)), None)
        if grp is None:
            continue
        provider = "cek" if _CEK.search(text) and not _DTEK.search(text) else "dtek"
        return provider, grp
    return None


def region_re(region: str = SVITLOBOT_REGION) -> re.Pattern | None:
    """«Дніпро» -> слово «Дніпро» (і «Днiпро» з латинською i), але не «Дніпровська» чи «Дніпропетровськ»."""
    region = (region or "").strip()
    if not region:
        return None
    pat = re.escape(region).replace("і", "[іi]").replace("І", "[Іi]")
    return re.compile(rf"(?<!\w){pat}(?!\w)", re.I)


def _float(s: str) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def city_of(address: str) -> str:
    """«Кривий Ріг -> Ньютона 34» -> «Кривий Ріг»."""
    return address.split("->", 1)[0].strip()


def parse_point(line: str, region: re.Pattern | None = None) -> Point | None:
    """Група з даних — лише якщо підпис про наш регіон: «Дніпро (ДТЕК) — група 5.1». «Група 1.1» без регіону
    чи «Київська обл. — група 2.1» — інші області з тими ж номерами черг, їх не беремо."""
    cols = line.strip().split(SEP)
    if len(cols) < 5:
        return None
    try:
        subs = int(cols[5]) if len(cols) > 5 and cols[5].strip() else 0
    except ValueError:
        subs = 0
    region = region if region is not None else region_re()
    extra = [c for c in cols[6:] if c.strip()]
    label = next((c for c in extra if _GROUP_LABELED.search(c)), " ".join(extra))
    address = cols[3].strip()
    found = None
    if region is None or region.search(label):
        found = extract_group(label)
    if found is None and (region is None or region.search(city_of(address))):
        found = extract_group(address)  # «Дніпро -> Робоча 5 (гр. 2.1)»
    return Point(
        id=cols[0].strip(),
        status=STATUS_MAP.get(cols[1].strip(), ""),
        changed=_iso(cols[2]),
        address=address,
        slug=cols[4].strip(),
        subscribers=subs,
        provider=found[0] if found else None,
        grp=found[1] if found else None,
        lat=_float(cols[6]) if len(cols) > 6 else None,
        lon=_float(cols[7]) if len(cols) > 7 else None,
    )


MAP_PATH = Path(__file__).with_name("svitlobot_groups.json")  # будує svitlobot_map.py за сайтом ДТЕК
_map_cache: tuple[float, dict] = (0.0, {})


def groups_map() -> dict[str, tuple[str, str]]:
    """{slug: (provider, група)} для точок без підпису групи. Файл перечитуємо, лише коли він змінився."""
    global _map_cache
    try:
        mtime = MAP_PATH.stat().st_mtime
    except OSError:
        return {}
    if mtime != _map_cache[0]:
        try:
            raw = json.loads(MAP_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        _map_cache = (mtime, {k: (v["provider"], v["grp"]) for k, v in raw.items()
                              if isinstance(v, dict) and G.is_known(v.get("grp")) and v.get("provider") in ("dtek", "cek")})
    return _map_cache[1]


def parse_channels(text: str, city: str = SVITLOBOT_CITY_FILTER, mapping: dict | None = None,
                   region: str = SVITLOBOT_REGION) -> list[Point]:
    """Сирий список точок -> точки нашого регіону: з підписом групи регіону або з групою з `mapping`
    {slug: (provider, група)} (svitlobot_groups.json — точки без підпису, групу знайшли на сайті ДТЕК).
    `city` (необов'язково) — лише цей населений пункт: слово в частині адреси до «->» (не у вулиці:
    «Київ -> Дніпровська набережна 3» — не Дніпро). Порожній — без обмеження за містом."""
    rre = region_re(region)
    city_re = region_re(city) if city and city.strip() else None
    mapping = mapping or {}
    out = []
    for line in text.splitlines():
        if not line.strip() or (p := parse_point(line, rre)) is None:
            continue
        if not p.grp and p.slug in mapping:
            p.provider, p.grp = mapping[p.slug]
        if not p.grp and rre is not None:
            continue  # не наш регіон (або група невідома — і так не рахується)
        if city_re and not city_re.search(city_of(p.address)):
            continue
        out.append(p)
    return out


def parse_all(text: str) -> list[Point]:
    """Усі точки як є, без фільтрів (для svitlobot_map.py)."""
    return [p for line in text.splitlines() if line.strip() and (p := parse_point(line, region_re())) is not None]


def parse_token(body: str) -> str:
    """Токен приходить або голим текстом (можливо в лапках), або JSON {"token": …} / {"t": …}."""
    body = body.strip()
    if body.startswith("{"):
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        if isinstance(data, dict):
            tok = data.get("token") or data.get("t")
            return str(tok).strip() if tok else ""
    return body.strip().strip("\"'").strip()


# --- ядро: групи ---
def aggregate(points: list[Point]) -> dict[tuple[str, str], GroupStat]:
    stats: dict[tuple[str, str], GroupStat] = {}
    for p in points:
        if not p.grp or not p.status:
            continue
        st = stats.setdefault((p.provider, p.grp), GroupStat(p.provider, p.grp))
        setattr(st, p.status, getattr(st, p.status) + 1)
    return stats


def decide(
    st: GroupStat,
    prev: str | None,
    min_sensors: int = SVITLOBOT_MIN_SENSORS,
    on_th: float = SVITLOBOT_ON_THRESHOLD,
    off_th: float = SVITLOBOT_OFF_THRESHOLD,
) -> str | None:
    """Новий стан групи: on / off; між порогами або замало живих точок — попередній (може бути None)."""
    if st.alive < min_sensors:
        return prev
    r = st.ratio
    if r >= on_th:
        return ON
    if r <= off_th:
        return OFF
    return prev


# --- мережа ---
async def _get(session: aiohttp.ClientSession, url: str, **params) -> str:
    try:
        async with session.get(url, params=params or None) as r:
            if r.status == 429:
                ra = r.headers.get("Retry-After", "")
                raise SourceError("429 Too Many Requests", float(ra) if ra.isdigit() else None)
            if r.status in (401, 403):
                raise SourceError(f"{r.status}: доступ заборонено (токен / Origin)")
            if r.status == 404:
                raise SourceError("404: адреса не знайдена")
            if r.status >= 400:
                raise SourceError(f"HTTP {r.status}")
            return await r.text()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise SourceError(f"мережа: {e!r}") from e


def _origin(url: str) -> str:
    """https://api.svitlobot.in.ua/website/… -> https://svitlobot.in.ua (сайт, з якого мапа ходить в API)."""
    m = re.match(r"(https?://)(?:api\.)?([^/]+)", url)
    return m.group(1) + m.group(2) if m else ""


async def fetch_raw() -> str:
    """Токен -> сирий список точок усієї країни. Будь-яка біда — SourceError (цикл бота не падає)."""
    if not SVITLOBOT_TOKEN_URL or not SVITLOBOT_CHANNELS_URL:
        raise SourceError("не задані SVITLOBOT_TOKEN_URL / SVITLOBOT_CHANNELS_URL")
    origin = _origin(SVITLOBOT_CHANNELS_URL)
    headers = {**HEADERS, "Origin": origin, "Referer": origin + "/"}
    async with aiohttp.ClientSession(timeout=TIMEOUT, headers=headers) as s:
        token = parse_token(await _get(s, SVITLOBOT_TOKEN_URL))
        if not token:
            raise SourceError("порожній токен")
        return await _get(s, SVITLOBOT_CHANNELS_URL, t=token)


async def fetch_points() -> list[Point]:
    """Точки міста з групами: спершу з довідника адрес ДТЕК (актуальна черга будинку), далі — з підпису
    датчика чи svitlobot_groups.json (підписи бувають ще зі старої сітки 1.1–6.2)."""
    return await resolve_points(await fetch_raw())


DIR_TTL = 6 * 3600  # адреса точки -> черга з довідника: кеш на 6 год (точок сотні, опитуємо раз на 2 хв)
_dir_cache: dict[str, tuple[bool, str | None, str | None]] = {}
_dir_streets: list[str] = []
_dir_ts = 0.0


_LAT = str.maketrans("iIaAoOeEcCpPxXyY", "іІаАоОеЕсСрРхХуУ")


def split_address(address: str) -> tuple[str, str | None]:
    """«Дніпро -> пр Слобожанський 19» -> («пр Слобожанський», «19»); «Магалевського 6 (п.1-3)» -> (…, «6»).
    Латинська «i» в кирилиці («Сiчових Стрiльцiв»), «пров.Джинчарадзе», ініціали «О. Поля» — теж."""
    from address import parse_text_address

    addr = address.split("->", 1)[-1].split("(", 1)[0]
    addr = re.sub(r"[A-Za-z]+", lambda m: m.group().translate(_LAT) if re.search(r"[а-яіїєґ]", addr, re.I) else m.group(), addr)
    addr = re.sub(r"(?<=\w)\.(?=[А-ЯІЇЄҐа-яіїєґ])", ". ", addr)  # «пров.Джинчарадзе» -> «пров. Джинчарадзе»
    addr = re.sub(r"(?<![\w])[А-ЯІЇЄҐ]\.\s*(?=[А-ЯІЇЄҐ][а-яіїєґ'’]{2,})", "", addr)  # «О. Поля» -> «Поля»
    return parse_text_address(addr)


def _base_house(house: str) -> str | None:
    """«129Б», «81/2», «24-А» -> «129», «81», «24» — у довіднику буває лише основний номер."""
    m = re.match(r"\d+", house or "")
    return m.group() if m and m.group() != house else None


async def directory_group(address: str) -> tuple[bool, str | None, str | None]:
    """(будинок є в довіднику?, постачальник, черга або None — будинок без черги)."""
    global _dir_ts, _dir_streets
    import db
    from address import norm_house, street_matches

    if time.time() - _dir_ts > DIR_TTL:
        _dir_cache.clear()
        _dir_streets = await db.street_names()
        _dir_ts = time.time()
    if address in _dir_cache:
        return _dir_cache[address]
    res: tuple[bool, str | None, str | None] = (False, None, None)
    street, house = split_address(address)
    if street and house and _dir_streets:
        names = street_matches(street, _dir_streets, limit=5)
        house = norm_house(house)
        # спершу точний номер на будь-якій із вулиць, потім основний номер («129Б» -> «129»)
        for num in filter(None, (house, _base_house(house))):
            h = next(iter([x for n in names if (x := await db.find_house(n, num))]), None)
            if h:
                res = (True, h["operator"], h["group_name"])
                break
    _dir_cache[address] = res
    return res


async def resolve_points(raw: str, mapping: dict | None = None, city: str = SVITLOBOT_CITY_FILTER) -> list[Point]:
    mapping = groups_map() if mapping is None else mapping
    rre = region_re()
    city_re = region_re(city) if city and city.strip() else None
    out = []
    for line in raw.splitlines():
        if not line.strip() or (p := parse_point(line, rre)) is None:
            continue
        if city_re and not city_re.search(city_of(p.address)):
            continue
        found, prov, grp = await directory_group(p.address)
        if found:  # довідник — головне: будинок без черги в групах не рахуємо
            p.provider, p.grp = prov, grp
        elif not p.grp and p.slug in mapping:
            p.provider, p.grp = mapping[p.slug]
        if p.grp:
            out.append(p)
    return out


# --- стан і зміни ---
def _key(provider: str, grp: str) -> str:
    return f"svb:{provider}:{grp}"


@dataclass
class Change:
    provider: str
    grp: str
    state: str  # on / off
    stat: GroupStat


_initialized = False  # перший прохід після запуску — лише базові стани


async def apply(points: list[Point], now: float | None = None) -> list[Change]:
    """Точки -> стани всіх груп у kv. Повертає зміни ON<->OFF (на першому проході після запуску — порожньо)."""
    global _initialized
    import db

    now = int(now or time.time())
    changes = []
    for (prov, grp), st in sorted(aggregate(points).items()):
        old = json.loads(await db.kv_get(_key(prov, grp)) or "null") or {}
        prev = old.get("state")
        new = decide(st, prev)
        # 1–2 живі точки: стан чесно за ними (а не застряглий попередній), але з позначкою «мало датчиків» —
        # для показу в «Зараз»; пушів групі й рішень про всю групу з таких даних не робимо
        weak = 0 < st.alive < SVITLOBOT_MIN_SENSORS
        if weak:
            new = ON if st.on * 2 >= st.alive else OFF
        await db.kv_set(_key(prov, grp), json.dumps(
            {"state": new, "on": st.on, "off": st.off, "maint": st.maint, "ts": now, "weak": weak,
             "since": old.get("since", now) if new == prev else now}
        ))
        if _initialized and not weak and not old.get("weak") and prev in (ON, OFF) and new in (ON, OFF) and new != prev:
            changes.append(Change(prov, grp, new, st))
    _initialized = True
    return changes


async def group_state(provider: str, grp: str, max_age: float | None = None) -> dict | None:
    """Свіжий стан групи за датчиками (для «Зараз»): {"state", "on", "off", "maint", "ts", "since"} або None."""
    import db

    s = json.loads(await db.kv_get(_key(provider, grp)) or "null")
    max_age = max_age if max_age is not None else max(3 * SVITLOBOT_POLL_INTERVAL, 10 * 60)
    if not s or time.time() - s.get("ts", 0) > max_age:
        return None
    return s


# --- тексти ---
def notify_text(c: Change, provider_name: str) -> str:
    """«💡 ДТЕК · група 1.2: світло з'явилось» + «📡 За датчиками: 14 з 16 точок зі світлом»."""
    st = c.stat
    if c.state == ON:
        head = f"💡 <b>{provider_name} · група {c.grp}: світло з'явилось</b>"
    else:
        head = f"🕯 <b>{provider_name} · група {c.grp}: світло зникло</b>"
    return f"{head}\n📡 За датчиками: {st.on} з {st.alive} точок зі світлом"


def status_line(s: dict | None) -> str | None:
    """Рядок для «Зараз»: «📡 Датчики: 🟢 є світло (14/16 точок)»."""
    if not s:
        return None
    alive = s.get("on", 0) + s.get("off", 0)
    if not alive:
        return None
    icon = {ON: "🟢", OFF: "🔴"}.get(s.get("state"), "🟡")
    weak = " · мало точок" if s.get("weak") else ""
    return f"📡 Датчики: {icon} {s['on']} з {alive} зі світлом{weak}"


if __name__ == "__main__":  # python svitlobot.py — що зараз бачать датчики по групах
    async def _main():
        pts = await fetch_points()
        towns = {}
        for p in pts:
            towns[city_of(p.address)] = towns.get(city_of(p.address), 0) + 1
        print(f"точок з групою: {len(pts)}; " + ", ".join(f"{k} {v}" for k, v in sorted(towns.items(), key=lambda x: -x[1])))
        for (prov, grp), st in sorted(aggregate(pts).items()):
            r = f"{st.ratio:.0%}" if st.ratio is not None else "—"
            print(f"{prov:4} {grp}: on={st.on:3} off={st.off:3} maint={st.maint:2}  {r:>4} -> {decide(st, None)}")

    asyncio.run(_main())
