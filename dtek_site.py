"""Сайт ДТЕК, «Перевірити адресу»: будинки вулиць, де є підписники, і їх черги.

Сайт на запит вулиці (POST /ua/ajax, method=getHomeNum) віддає всі її будинки:
    {"14": {"sub_type": "Екстренні відключення (Аварійне без застосування графіку погодинних відключень)",
            "start_date": "13:16 20.11.2026", "end_date": "17:16 20.11.2026", "sub_type_reason": ["GPV14.1"], ...}}
Тобто по кожному будинку видно його чергу (будь-яку X.Y; буває й без черги) і чи є зараз світло.
method=getStreets віддає перелік усіх вулиць: {"streets": {"м. Дніпро": ["вул. …", …], …}}.

Повний довідник вулиць і будинків збирає scripts/seed_streets.py; сервер опитує лише вулиці з підписниками.
Перевірка вручну: python dtek_site.py [вулиця ...]
"""
import asyncio
import json
import logging
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime

import dtek_http
import groups as G
from config import DTEK_BACKOFF_SEC, DTEK_POLL_PAUSE_MAX, DTEK_POLL_PAUSE_MIN, DTEK_STREETS, TZ

log = logging.getLogger(__name__)

PAGE_URL = dtek_http.PAGE_URL
AJAX_URL = dtek_http.AJAX_URL
CITY = "м. Дніпро"


@dataclass
class House:
    street: str
    num: str
    groups: list[str]
    kind: str | None  # emergency | schedule | accident | planned | other | None (світло є)
    sub_type: str = ""
    start: str = ""  # «13:16 20.11.2026»
    end: str = ""
    cek: str = ""  # поле «cek» у відповіді сайту: не порожнє — будинок, схоже, обслуговує ЦЕК

    @property
    def key(self) -> str:
        return f"{self.street}|{self.num}"


@dataclass
class Probe:
    houses: list[House] = field(default_factory=list)
    attention: str | None = None  # вікно «Шановні клієнти!» з тієї ж сторінки
    failed: list[str] = field(default_factory=list)  # вулиці, по яких сайт нічого не дав
    streets: list[str] = field(default_factory=list)  # перелік вулиць м. Дніпро з поля «Введіть вулицю»
    page_fetched: bool = True  # False — сторінку не тягнули (csrf з кешу): вікна «Шановні клієнти!» цього разу немає


def classify(sub_type: str) -> str | None:
    """Причина відключення з сайту -> тип. «Екстренні відключення (Аварійне без застосування графіку…)» — екстрене."""
    t = (sub_type or "").lower()
    if not t.strip():
        return None
    if "екстрен" in t or "гав" in t.split():
        return "emergency"
    if "стабілізац" in t or ("графік" in t and "без застосування" not in t):
        return "schedule"
    if "аварій" in t:
        return "accident"
    if "планов" in t:
        return "planned"
    return "other"


def parse_houses(street: str, payload) -> list[House]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list):
        data = dict(enumerate(data))
    out = []
    for num, h in (data or {}).items():
        if not isinstance(h, dict):
            continue
        reasons = h.get("sub_type_reason") or []
        groups = G.sort(g for r in (reasons if isinstance(reasons, list) else [reasons]) if (g := G.from_dtek_reason(r)))
        sub = str(h.get("sub_type") or "").strip()
        cek = h.get("cek")
        cek = "" if cek in (None, False, "", 0, "0") else (json.dumps(cek, ensure_ascii=False) if isinstance(cek, (dict, list)) else str(cek))
        out.append(House(street, str(num).strip(), groups, classify(sub), sub,
                         str(h.get("start_date") or "").strip(), str(h.get("end_date") or "").strip(), cek.strip()))
    return out


def cek_group(h: House) -> str | None:
    """З поля «cek» дістаємо чергу ЦЕК, якщо вона там є («2.1», «GPV1002.1», «ЦЕК 14.1»)."""
    return G.from_cek(h.cek)


def _form(street: str, update: str | None) -> dict[str, str]:
    fields = [("city", CITY), ("street", street)] + ([("updateFact", update)] if update else [])
    return dtek_http.ajax_form("getHomeNum", fields)


def parse_streets_payload(payload, city: str = CITY) -> list[str]:
    """Відповідь getStreets {"streets": {"м. Дніпро": [...], ...}} -> вулиці цього міста."""
    data = payload.get("streets") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        want = re.sub(r"\s+", " ", city).strip().lower()
        data = next((v for k, v in data.items() if re.sub(r"\s+", " ", str(k)).strip().lower() == want), None)
    if not isinstance(data, list):
        return []
    return [x.strip() for x in data if isinstance(x, str) and x.strip()]


def parse_streets(html: str, city: str = CITY) -> list[str]:
    """`DisconSchedule.streets = {"с. Філія": ["вул. Лісова"], …, "м. Дніпро": ["вул. Микитинська", "тупик Орільський", …]}`
    -> вулиці саме цього населеного пункту, як є (їх і шлемо в «Перевірити адресу»).
    Ключ — точно «м. Дніпро»: «смт Дніпровське» чи «м. Дніпрорудне» теж містять «Дніпро», але це інші місця."""
    m = re.search(r"DisconSchedule\.streets\s*=\s*", html)
    if not m:
        return []
    try:
        data, _ = json.JSONDecoder().raw_decode(html[m.end():])
    except ValueError:
        return []
    if isinstance(data, dict):
        want = re.sub(r"\s+", " ", city).strip().lower()
        data = next((v for k, v in data.items() if re.sub(r"\s+", " ", str(k)).strip().lower() == want), None)
    if not isinstance(data, list):
        return []  # міста в переліку немає — lookup підбере назву сам (guess_dtek_names)
    return [x.strip() for x in data if isinstance(x, str) and x.strip()]


class _Session:
    """Сторінка (кукі + csrf + перелік вулиць) і запити «Перевірити адресу» в одній сесії curl_cffi.
    use_backoff=False — для сідера довідника з ПК: він сам вирішує, скільки чекати після відмови."""

    def __init__(self, use_backoff: bool = True):
        self.use_backoff = use_backoff

    async def __aenter__(self):
        from providers import extract_attention

        if self.use_backoff:  # сервер: сторінку (~800 КБ) не тягнемо щоциклу — csrf під поточні кукі вже є
            if dtek_http.backoff_left():
                raise dtek_http.Backoff(f"пауза ще {int(dtek_http.backoff_left())} с")
            await dtek_http.ensure_browser_session()
            if cached := dtek_http.cached_page():
                self.csrf, self.update = cached["csrf"], cached.get("update")
                self.attention, self.streets, self.page_fetched = None, [], False
                return self
        r = await dtek_http.request("GET", PAGE_URL, use_backoff=self.use_backoff)
        token = re.search(r'<meta[^>]+name="csrf-token"[^>]+content="([^"]+)"', r.text)
        if not token or "DisconSchedule" not in r.text:
            await dtek_http.reset()
            if self.use_backoff:
                dtek_http.backoff("сторінка без csrf/DisconSchedule")
            raise dtek_http.Blocked("сторінка ДТЕК без csrf/DisconSchedule — схоже, антибот")
        update = re.search(r'DisconSchedule\.fact\s*=\s*\{.*?"update"\s*:\s*"([^"]+)"', r.text, re.S)
        self.update = update and update.group(1)
        self.csrf = token.group(1)
        self.attention = extract_attention(r.text)
        self.streets = parse_streets(r.text)
        self.page_fetched = True
        dtek_http.remember_page(self.csrf, self.update)
        return self

    async def __aexit__(self, *exc):
        return False

    async def street_list(self) -> list[str]:
        """method=getStreets — повний перелік вулиць м. Дніпро."""
        payload = await dtek_http.ajax({"method": "getStreets"}, self.csrf, use_backoff=self.use_backoff)
        return parse_streets_payload(payload)

    async def houses(self, street: str, cache: bool = True) -> list[House]:
        payload = await dtek_http.ajax(_form(street, self.update), self.csrf, use_backoff=self.use_backoff)
        if not payload.get("result", True) and not payload.get("data"):
            raise ValueError(f"ДТЕК: вулиця {street} — result=false")
        houses = parse_houses(street, payload)
        if cache:
            await _cache_houses(street, houses)
        return houses


async def _cache_houses(street: str, houses: list[House]):
    """Кожна успішна відповідь сайту -> довідник «вулиця -> будинки з чергами»: адреса знаходиться без
    запиту до сайту, навіть коли він закрився антиботом."""
    import db

    try:
        await db.save_houses(street, [(h.num, h.groups, h.cek) for h in houses])
    except Exception as e:  # довідник — не привід зламати перевірку
        log.warning("довідник будинків %s: %r", street, e)


async def _remember_streets(streets: list[str]):
    import db

    if streets:
        await db.upsert_streets(streets)


async def probe(streets: list[str] | None = None, pause: tuple[float, float] | None = None) -> Probe:
    """Сторінка (кукі + csrf) -> по запиту на кожну вулицю з паузою pause (3–5 с) між ними.
    Перша ж відмова сайту — зупиняємось (далі пауза DTEK_BACKOFF_SEC). Нічого не дав — DtekError/ValueError."""
    streets = list(dict.fromkeys(streets or DTEK_STREETS))
    if not streets:
        raise ValueError("немає вулиць для перевірки")
    lo, hi = pause or (DTEK_POLL_PAUSE_MIN, DTEK_POLL_PAUSE_MAX)
    out = Probe()
    async with _Session() as s:
        out.attention, out.streets, out.page_fetched = s.attention, s.streets, s.page_fetched
        for i, street in enumerate(streets):
            if i:
                await asyncio.sleep(lo + random.random() * max(hi - lo, 0))  # не пачкою і не рівним ритмом
            try:
                houses = await s.houses(street)
            except dtek_http.DtekError as e:
                log.warning("ДТЕК: вулиця %s — %r; решту вулиць не смикаємо", street, e)
                out.failed += streets[i:]
                break
            except ValueError as e:
                log.warning("ДТЕК: вулиця %s — %r", street, e)
                houses = []
            if not houses:
                out.failed.append(street)
            out.houses += houses
    await _remember_streets(out.streets)
    if not out.houses:
        raise ValueError(f"сайт ДТЕК не віддав жодного будинку ({len(streets)} вулиць)")
    return out


# ---------- адреса користувача ----------

BLOCK_SEC = DTEK_BACKOFF_SEC  # сайт показав антибот — 30 хв не смикаємо його запитами людей (адреса -> довідник / «чекає»)


async def site_blocked() -> bool:
    """Сайт зараз на паузі: остання спроба за DTEK_BACKOFF_SEC натрапила на помилку / антибот."""
    import db

    if dtek_http.backoff_left():
        return True
    s = json.loads(await db.kv_get("site_probe") or "null")
    return bool(s) and not s.get("ok") and time.time() - s.get("ts", 0) < BLOCK_SEC


async def mark_blocked(err: str):
    import db

    await db.kv_set("site_probe", json.dumps({"ok": False, "ts": int(time.time()), "err": err[:80]}, ensure_ascii=False))


async def street_candidates(street_query: str) -> list[str]:
    """Назва від людини -> назви вулиць ДТЕК: з довідника (dtek_streets), без нього — здогадки."""
    import db
    from address import guess_dtek_names, match_street

    known = await db.street_names()
    if known:
        best = match_street(street_query, known)
        return [best] if best else []
    return guess_dtek_names(street_query)


@dataclass
class Lookup:
    street: str | None = None  # назва вулиці як на сайті ДТЕК; None — вулицю не знайшли
    house: House | None = None  # None — будинку немає в даних ДТЕК (можливо, ЦЕК)
    houses_on_street: int = 0

    @property
    def no_queue(self) -> bool:
        """Будинок ДТЕК, у якого на сайті немає черги: стежимо за самим будинком."""
        return bool(self.house) and not self.house.cek and not self.house.groups


async def lookup_cached(street_query: str, house: str) -> Lookup | None:
    """Те саме, що lookup, але з довідника — 0 запитів до сайту. None — цієї вулиці ще не опитували.
    Стан будинку (є/нема світла) з довідника не знаємо — його покаже наступна перевірка сайту."""
    import db

    for name in await street_candidates(street_query):
        found = await db.find_house(name, house)
        if found:
            n = len(await db.cached_houses(name))
            return Lookup(name, House(name, found["num"], found["groups"], None, cek=found["cek"]), n)
        if rows := await db.cached_houses(name):
            return Lookup(name, None, len(rows))
        if await db.street_polled(name):
            return Lookup(name, None, 0)  # вулицю опитали — будинків ДТЕК на ній немає
    return None


async def lookup(street_query: str, house: str) -> Lookup:
    """«Яворницького», «3» -> вулиця і будинок на сайті ДТЕК (з групою і поточним станом).
    Сайт недоступний (антибот / пауза) — dtek_http.DtekError або ValueError."""
    import db
    from address import find_house, guess_dtek_names, match_street

    async with _Session() as s:
        await _remember_streets(s.streets)
        known = s.streets or await db.street_names()
        if known:
            best = match_street(street_query, known)
            candidates = [best] if best else []
        else:
            candidates = guess_dtek_names(street_query)
        for name in candidates:
            try:
                houses = await s.houses(name)
            except ValueError as e:
                log.warning("ДТЕК: вулиця %s — %r", name, e)
                continue
            if not houses:
                continue
            found = find_house({h.num: h for h in houses}, house)
            return Lookup(name, found[1] if found else None, len(houses))
    return Lookup()


REASON_SHORT = {
    "emergency": "екстрене відключення",
    "accident": "аварійні ремонтні роботи",
    "planned": "планові ремонтні роботи",
    "schedule": "відключення за графіком",
}


def reason(h: House) -> str:
    return REASON_SHORT.get(h.kind or "", (h.sub_type or "відключення")[:80])


def _house_off_text(street: str, num: str, h: House, head: str) -> str:
    lines = [f"🏠 <b>{head}</b>", f"{street}, {num}", f"⚡ {reason(h)}"]
    if h.end:
        since = f"з {short_dt(h.start).split(' ')[0]}, " if h.start else ""
        lines.append(f"⏱ {since}орієнтовно до <b>{short_dt(h.end)}</b>")
    lines.append("<i>За даними сайту ДТЕК (перевірка адреси)</i>")
    return "\n".join(lines)


def resolve_pending(probe_: Probe, pending: list[dict], cands: dict[int, list[str]]) -> list[tuple[dict, str, House | None]]:
    """Адреси, що чекали на сайт: [(людина, вулиця ДТЕК, будинок або None — вулицю бачили, будинку нема)].
    Ті, чиїх вулиць сайт цього разу не дав, — не в результаті (чекають далі)."""
    from address import find_house

    by_street: dict[str, dict[str, House]] = {}
    for h in probe_.houses:
        by_street.setdefault(h.street, {})[h.num] = h
    out = []
    for u in pending:
        _, _, house = (u.get("addr_q") or "").partition("|")
        seen = [st for st in cands.get(u["id"], []) if st in by_street]
        if not seen:
            continue
        hit = next(((st, f[1]) for st in seen if (f := find_house(by_street[st], house))), None)
        out.append((u, hit[0], hit[1]) if hit else (u, seen[0], None))
    return out


async def apply_users(probe_: Probe, users: list[dict]) -> list[tuple[int, str]]:
    """Будинки користувачів, які вказали адресу -> особисті пуші [(user_id, текст)] + стан для «Зараз».
    «Світло є» після відключення — лише після END_MISSES перевірок поспіль (сайт інколи блимає)."""
    import db
    from address import find_house

    by_street: dict[str, dict[str, House]] = {}
    for h in probe_.houses:
        by_street.setdefault(h.street, {})[h.num] = h
    out = []
    now = int(time.time())
    for u in users:
        street, house = u.get("street") or "", u.get("house") or ""
        if not street or not house or (u.get("provider") or "dtek") != "dtek" or street not in by_street:
            continue  # немає адреси, ЦЕК, або сайт по цій вулиці зараз нічого не дав — нічого не знаємо
        found = find_house(by_street[street], house)
        if not found or found[1].cek:  # будинок ЦЕК — у ДТЕК його стан не показується
            continue
        num, h = found
        off = bool(h.kind) and h.kind != "schedule"  # «за графіком» люди і так знають з графіка
        await db.kv_set(f"house_seen:{u['id']}", json.dumps(
            {"kind": h.kind, "reason": reason(h) if h.kind else "", "start": h.start, "end": h.end, "ts": now,
             "groups": h.groups, "street": street, "num": num}, ensure_ascii=False,
        ))
        if h.groups and h.groups[0] != u.get("grp"):  # ДТЕК переніс будинок в іншу чергу / дав чергу будинку без черги
            await db.set_user(u["id"], grp=h.groups[0])
            was = f"було {u['grp']}" if u.get("grp") else "раніше черги не було"
            out.append((u["id"], f"ℹ️ За даними ДТЕК твій будинок ({street}, {num}) тепер у групі <b>{h.groups[0]}</b> "
                                 f"({was}). Я оновив групу в налаштуваннях."))
        prev = json.loads(await db.kv_get(f"house:{u['id']}") or "null")
        if off:
            if not prev:
                head = "У твоєму будинку зараз немає світла"
            elif h.end and prev.get("end") and _shift_min(h.end, prev["end"]) >= SHIFT_MIN:
                head = "Відключення у твоєму будинку продовжено"
            elif h.end and prev.get("end") and _shift_min(h.end, prev["end"]) <= -SHIFT_MIN:
                head = "Відключення у твоєму будинку скорочено"
            else:
                head = None
            if head and u.get("notify_on", 1):
                out.append((u["id"], _house_off_text(street, num, h, head)))
            await db.kv_set(f"house:{u['id']}", json.dumps({"start": h.start, "end": h.end, "miss": 0}))
            continue
        if not prev:
            continue
        miss = prev.get("miss", 0) + 1
        if miss < END_MISSES:
            await db.kv_set(f"house:{u['id']}", json.dumps({**prev, "miss": miss}))
            continue
        await db.kv_set(f"house:{u['id']}", "")
        if u.get("notify_on", 1):
            out.append((u["id"], f"✅ <b>У твоєму будинку світло мало з'явитися</b>\n{street}, {num}\n"
                                 "<i>За даними сайту ДТЕК. Якщо світла ще немає — натисни «🙋 У мене є / нема світла».</i>"))
    return out


@dataclass
class GroupState:
    grp: str
    total: int = 0  # скільки контрольних будинків групи бачимо
    off: list[House] = field(default_factory=list)  # з них — екстрене зараз

    @property
    def start(self) -> str:
        return min((h.start for h in self.off if h.start), key=_dt_key, default="")

    @property
    def end(self) -> str:
        return max((h.end for h in self.off if h.end), key=_dt_key, default="")


def _dt_key(s: str):
    """«13:16 20.11.2026» -> сортування (рік, місяць, день, час)."""
    m = re.match(r"(\d{1,2}):(\d{2})\s+(\d{2})\.(\d{2})\.?(\d{4})?", s)
    if not m:
        return ("", "", "", s)
    h, mi, d, mo, y = m.groups()
    return (y or "", mo, d, f"{int(h):02d}:{mi}")


def group_states(houses: list[House]) -> dict[str, GroupState]:
    out: dict[str, GroupState] = {}
    for h in houses:
        if h.cek:  # будинки ЦЕК не рахуємо в групах ДТЕК
            continue
        for g in h.groups:
            st = out.setdefault(g, GroupState(g))
            st.total += 1
            if h.kind == "emergency":
                st.off.append(h)
    return out


def short_dt(s: str) -> str:
    """«17:16 20.11.2026» -> «17:16 20.11» (формат «до» в alerts)."""
    m = re.match(r"(\d{1,2}:\d{2})(?:\s+(\d{2}\.\d{2}))?", s or "")
    return " ".join(x for x in m.groups() if x) if m else ""


# ---------- стан по групах: що вже знаємо і що пушили ----------

MIN_SHARE = 0.2  # екстрене в 1 будинку з 30 — не привід смикати всю групу; від 20% адрес групи — так
END_MISSES = 2  # «світло є» за тими ж будинками — 2 перевірки поспіль, тоді «екстрене закінчилось»
SHIFT_MIN = 30  # «продовжено до …» — лише якщо час зсунувся від 30 хв


def _shift_min(new: str, old: str) -> int:
    from datetime import datetime

    try:
        a, b = (datetime.strptime(x, "%H:%M %d.%m.%Y") for x in (new, old))
    except ValueError:
        return 0
    return int((a - b).total_seconds() // 60)


def _off_text(grp: str, st: GroupState, head: str) -> str:
    lines = [f"🚨 <b>{head}</b> · група <b>{grp}</b>"]
    if st.end:
        since = f"з {short_dt(st.start).split(' ')[0]}, " if st.start else ""
        lines.append(f"⏱ {since}орієнтовно до <b>{short_dt(st.end)}</b>")
    lines.append(f"🏠 Без світла {len(st.off)} з {st.total} перевірених адрес групи")
    lines.append("Графік зараз не діє.")
    return "\n".join(lines)


async def apply(probe_: Probe) -> list[str]:
    """Результат перевірки -> alerts (їх розсилає notifier за ~15 с) + стан у kv. Повертає, що змінилось (для логів)."""
    import db

    changes = []
    states = group_states(probe_.houses)
    by_key = {h.key: h for h in probe_.houses}
    for grp in G.sort(set(states) | set(G.known())):
        st = states.get(grp)
        if st:  # частку бачимо завжди — навіть нижче порогу пуша («🏠 екстрене у 30 з 265 адрес групи»)
            await db.kv_set(f"site_grp:{grp}", json.dumps(
                {"total": st.total, "off": len(st.off), "end": st.end, "ts": int(time.time()),
                 "streets": list(dict.fromkeys(h.street for h in st.off))[:3],
                 "n_streets": len({h.street for h in st.off})}, ensure_ascii=False
            ))
        prev = json.loads(await db.kv_get(f"site_em:{grp}") or "null")
        if st and st.off and len(st.off) >= MIN_SHARE * st.total:
            if prev is None:
                head = "Екстрене відключення"
            elif st.end and prev.get("end") and _shift_min(st.end, prev["end"]) >= SHIFT_MIN:
                head = "Екстрене відключення продовжено"
            elif st.end and prev.get("end") and _shift_min(st.end, prev["end"]) <= -SHIFT_MIN:
                head = "Екстрене відключення скорочено"
            else:
                head = None
            if head:
                key = f"site:off:{grp}:{st.start}:{st.end}"
                # для «Зараз»; пуш групі шле інцидент (record_incidents -> notifier.send_incidents) — без дублів
                if await db.add_alert(key, "off", grp, short_dt(st.end) or None, _off_text(grp, st, head), "dtek",
                                      "dteksite", sent=1):
                    changes.append(f"{grp}: {head.lower()} до {st.end or '?'} ({len(st.off)}/{st.total})")
            await db.kv_set(f"site_em:{grp}", json.dumps(
                {"start": st.start, "end": st.end, "houses": [h.key for h in st.off], "miss": 0}, ensure_ascii=False
            ))
            continue
        if not prev:
            continue
        # тих самих будинків у відповіді немає (вулиця не відповіла) — не знаємо, чекаємо
        seen = [by_key[k] for k in prev.get("houses", []) if k in by_key]
        if not seen and not (st and st.total):
            continue
        miss = prev.get("miss", 0) + 1
        if miss < END_MISSES:
            await db.kv_set(f"site_em:{grp}", json.dumps({**prev, "miss": miss}, ensure_ascii=False))
            continue
        await db.kv_set(f"site_em:{grp}", "")
        text = (
            f"✅ Група <b>{grp}</b>: екстрене відключення закінчилось — за контрольними адресами "
            "на сайті ДТЕК світло вже є\nℹ️ Деяким адресам можуть дати трохи пізніше"
        )
        await db.add_alert(f"site:on:{grp}:{int(time.time()) // 60}", "on", grp, None, text, "dtek", "dteksite")
        changes.append(f"{grp}: екстрене закінчилось")
    return changes


def coverage(houses: list[House]) -> dict[str, int]:
    states = group_states(houses)
    return {g: states[g].total if g in states else 0 for g in G.sort(set(states) | set(G.known()))}


# ---------- аварії з часом ремонту -> інциденти по чергах ----------

INCIDENT_KINDS = ("emergency", "accident", "planned", "other")


def dt_ts(s: str) -> int | None:
    """«17:16 20.11.2026» -> unix-час (Київ)."""
    try:
        return int(datetime.strptime((s or "").strip(), "%H:%M %d.%m.%Y").replace(tzinfo=TZ).timestamp())
    except ValueError:
        return None


async def record_incidents(houses: list[House]) -> int:
    """Будинки з аварією і часом ремонту (end_date) -> group_incidents. Повертає, скільки нових інцидентів.
    Сусідні вулиці з тим самим (черга, причина, кінець) — один інцидент, групі — одне сповіщення."""
    import db

    now, new, seen = time.time(), 0, set()
    for h in houses:
        if h.cek or h.kind not in INCIDENT_KINDS or not h.groups:
            continue
        end = dt_ts(h.end)
        if not end or end <= now:
            continue
        for g in h.groups:
            iid, is_new = await db.upsert_incident("dtek", g, h.street, reason(h), dt_ts(h.start), end)
            seen.add(iid)
            new += is_new
    # на опитаних вулицях аварії вже немає (світло дали раніше) — інцидент закриваємо, «Зараз» його не показує
    await db.end_incidents_except("dtek", {h.street for h in houses}, seen)
    return new


async def _main(streets):
    logging.basicConfig(level=logging.INFO)
    p = await probe(streets or None)
    print(f"Будинків: {len(p.houses)}. Вулиці без відповіді: {', '.join(p.failed) or '—'}")
    for g, st in sorted(group_states(p.houses).items()):
        off = f" · ЕКСТРЕНЕ {len(st.off)} (з {st.start} до {st.end})" if st.off else ""
        print(f"  {g}: {st.total} адрес{off}")
    missing = [g for g, n in coverage(p.houses).items() if not n]
    print("Групи без контрольних адрес:", ", ".join(missing) or "немає — всі покриті")
    other = {h.sub_type for h in p.houses if h.kind and h.kind != "emergency"}
    if other:
        print("Інші причини:", "; ".join(sorted(other)))
    print("Шановні клієнти:", p.attention)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(_main(sys.argv[1:]))
