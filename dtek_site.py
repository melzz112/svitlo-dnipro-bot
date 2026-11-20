"""Сайт ДТЕК, «Перевірити адресу»: що зараз із контрольними адресами кожної групи.

Сайт на запит вулиці (POST /ua/ajax, method=getHomeNum) віддає всі її будинки:
    {"14": {"sub_type": "Екстренні відключення (Аварійне без застосування графіку погодинних відключень)",
            "start_date": "13:16 20.11.2026", "end_date": "17:16 20.11.2026", "sub_type_reason": ["GPV1.2"], ...}}
Тобто по кожному будинку видно його чергу і чи є зараз світло. Кілька довгих вулиць у різних районах —
це контрольні адреси всіх груп: бот бачить «по групі 1.2 вимкнули екстрено» раніше, ніж напишуть канали.

Перевірка вручну на сервері: python dtek_site.py [вулиця ...]
"""
import asyncio
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field

import httpx

from config import DTEK_STREETS, GROUPS

log = logging.getLogger(__name__)

PAGE_URL = "https://www.dtek-dnem.com.ua/ua/shutdowns"
AJAX_URL = "https://www.dtek-dnem.com.ua/ua/ajax"
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

    @property
    def key(self) -> str:
        return f"{self.street}|{self.num}"


@dataclass
class Probe:
    houses: list[House] = field(default_factory=list)
    attention: str | None = None  # вікно «Шановні клієнти!» з тієї ж сторінки
    failed: list[str] = field(default_factory=list)  # вулиці, по яких сайт нічого не дав
    streets: list[str] = field(default_factory=list)  # перелік вулиць м. Дніпро з поля «Введіть вулицю»


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
        groups = set()
        for reason in h.get("sub_type_reason") or []:
            if (m := re.fullmatch(r"GPV([1-6]\.[12])", str(reason).strip())) and m.group(1) in GROUPS:
                groups.add(m.group(1))
        sub = str(h.get("sub_type") or "").strip()
        out.append(House(street, str(num), sorted(groups), classify(sub), sub,
                         str(h.get("start_date") or "").strip(), str(h.get("end_date") or "").strip()))
    return out


def _form(street: str, update: str | None) -> dict[str, str]:
    fields = [("city", CITY), ("street", street)] + ([("updateFact", update)] if update else [])
    data = {"method": "getHomeNum"}
    for i, (name, value) in enumerate(fields):
        data[f"data[{i}][name]"] = name
        data[f"data[{i}][value]"] = value
    return data


def parse_streets(html: str) -> list[str]:
    """`DisconSchedule.streets = {...}` зі сторінки -> вулиці м. Дніпро. Формат не документований — розбираємо обережно."""
    m = re.search(r"DisconSchedule\.streets\s*=\s*", html)
    if not m:
        return []
    try:
        data, _ = json.JSONDecoder().raw_decode(html[m.end():])
    except ValueError:
        return []
    if isinstance(data, dict):
        city = next((v for k, v in data.items() if "Дніпро" in str(k) and "обл" not in str(k).lower()), None)
        data = city if city is not None else [x for v in data.values() for x in (v if isinstance(v, list) else [])]
    if isinstance(data, dict):
        data = list(data.keys()) if all(isinstance(v, (dict, list, int)) for v in data.values()) else list(data.values())
    out = []
    for x in data or []:
        name = x.get("name") or x.get("street") if isinstance(x, dict) else x
        if isinstance(name, str) and name.strip():
            out.append(name.strip())
    return out


class _Session:
    """Сторінка (кукі + csrf + перелік вулиць) і запити «Перевірити адресу» в одному клієнті."""

    async def __aenter__(self):
        from providers import extract_attention

        self.client = httpx.AsyncClient(timeout=10, follow_redirects=True)
        try:
            r = await self.client.get(PAGE_URL)
            r.raise_for_status()
            token = re.search(r'<meta[^>]+name="csrf-token"[^>]+content="([^"]+)"', r.text)
            if not token or "DisconSchedule" not in r.text:
                raise ValueError("сторінка ДТЕК без csrf/DisconSchedule — схоже, антибот")
        except BaseException:
            await self.client.aclose()
            raise
        update = re.search(r'DisconSchedule\.fact\s*=\s*\{.*?"update"\s*:\s*"([^"]+)"', r.text, re.S)
        self.update = update and update.group(1)
        self.attention = extract_attention(r.text)
        self.streets = parse_streets(r.text)
        self.headers = {"X-CSRF-Token": token.group(1), "X-Requested-With": "XMLHttpRequest", "Referer": PAGE_URL}
        return self

    async def __aexit__(self, *exc):
        await self.client.aclose()

    async def houses(self, street: str) -> list[House]:
        rr = await self.client.post(AJAX_URL, data=_form(street, self.update), headers=self.headers)
        rr.raise_for_status()
        return parse_houses(street, rr.json())


async def _remember_streets(streets: list[str]):
    import db

    if streets:
        await db.kv_set("dtek_streets", json.dumps(streets, ensure_ascii=False))


async def probe(streets: list[str] | None = None) -> Probe:
    """Сторінка (кукі + csrf) -> по запиту на кожну вулицю. Сайт закрився антиботом — ValueError/HTTPError."""
    streets = streets or DTEK_STREETS
    out = Probe()
    async with _Session() as s:
        out.attention, out.streets = s.attention, s.streets
        for street in streets:
            try:
                houses = await s.houses(street)
            except (httpx.HTTPError, ValueError) as e:
                log.warning("ДТЕК: вулиця %s — %r", street, e)
                houses = []
            if not houses:
                out.failed.append(street)
            out.houses += houses
            await asyncio.sleep(0.3)  # не смикаємо сайт пачкою
    await _remember_streets(out.streets)
    if not out.houses:
        raise ValueError(f"сайт ДТЕК не віддав жодного будинку ({len(streets)} вулиць)")
    return out


# ---------- адреса користувача ----------


@dataclass
class Lookup:
    street: str | None = None  # назва вулиці як на сайті ДТЕК; None — вулицю не знайшли
    house: House | None = None  # None — будинку немає в даних ДТЕК (можливо, ЦЕК)
    houses_on_street: int = 0


async def lookup(street_query: str, house: str) -> Lookup:
    """«Яворницького», «3» -> вулиця і будинок на сайті ДТЕК (з групою і поточним станом).
    Сайт недоступний (антибот) — ValueError/HTTPError."""
    import db
    from address import find_house, guess_dtek_names, match_street

    async with _Session() as s:
        await _remember_streets(s.streets)
        known = s.streets or json.loads(await db.kv_get("dtek_streets") or "[]")
        if known:
            best = match_street(street_query, known)
            candidates = [best] if best else []
        else:
            candidates = guess_dtek_names(street_query)
        for name in candidates:
            try:
                houses = await s.houses(name)
            except (httpx.HTTPError, ValueError) as e:
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
        if not found:
            continue
        num, h = found
        off = bool(h.kind) and h.kind != "schedule"  # «за графіком» люди і так знають з графіка
        await db.kv_set(f"house_seen:{u['id']}", json.dumps(
            {"kind": h.kind, "reason": reason(h) if h.kind else "", "start": h.start, "end": h.end, "ts": now,
             "groups": h.groups, "street": street, "num": num}, ensure_ascii=False,
        ))
        if h.groups and u.get("grp") and h.groups[0] != u["grp"]:  # ДТЕК переніс будинок в іншу чергу
            await db.set_user(u["id"], grp=h.groups[0])
            out.append((u["id"], f"ℹ️ За даними ДТЕК твій будинок ({street}, {num}) тепер у групі <b>{h.groups[0]}</b> "
                                 f"(було {u['grp']}). Я оновив групу в налаштуваннях."))
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
    for grp in GROUPS:
        st = states.get(grp)
        if st:  # частку бачимо завжди — навіть нижче порогу пуша («🏠 екстрене у 30 з 265 адрес групи»)
            await db.kv_set(f"site_grp:{grp}", json.dumps(
                {"total": st.total, "off": len(st.off), "end": st.end, "ts": int(time.time())}, ensure_ascii=False
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
                if await db.add_alert(key, "off", grp, short_dt(st.end) or None, _off_text(grp, st, head), "dtek", "dteksite"):
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
    return {g: states[g].total if g in states else 0 for g in GROUPS}


async def _main(streets):
    logging.basicConfig(level=logging.INFO)
    p = await probe(streets or None)
    print(f"Будинків: {len(p.houses)}. Вулиці без відповіді: {', '.join(p.failed) or '—'}")
    for g, st in sorted(group_states(p.houses).items()):
        off = f" · ЕКСТРЕНЕ {len(st.off)} (з {st.start} до {st.end})" if st.off else ""
        print(f"  {g}: {st.total} адрес{off}")
    missing = [g for g, n in coverage(p.houses).items() if not n]
    print("Групи без контрольних адрес:", ", ".join(missing) or "немає — всі 12 покриті")
    other = {h.sub_type for h in p.houses if h.kind and h.kind != "emergency"}
    if other:
        print("Інші причини:", "; ".join(sorted(other)))
    print("Шановні клієнти:", p.attention)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(_main(sys.argv[1:]))
