"""Адреса користувача: геопозиція / текст -> вулиця + будинок у форматі сайту ДТЕК.

- Геопозиція -> OpenStreetMap Nominatim (reverse): «проспект Дмитра Яворницького», «Соколина вулиця», будинок (не завжди).
  Координати нікуди не зберігаємо — лише вулицю і будинок. Політика Nominatim: ≤1 запит/с, свій User-Agent.
- Назва вулиці -> назва з поля «Введіть вулицю» на сайті ДТЕК («просп. Дмитра Яворницького», «вул. Соколина»):
  порівнюємо «ядро» назви без типу (вулиця/вул./проспект…), без регістру й апострофів.
- Номер будинку -> як ключ у відповіді сайту («14», «14А», «14/2»).
"""
import asyncio
import difflib
import re
import time

import httpx

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "esvitlo-dnipro-bot/1.0 (telegram bot with power outage schedules)"

# тип вулиці -> як пише ДТЕК
STREET_TYPES = {
    "вулиця": "вул.", "вул": "вул.", "ул": "вул.", "улица": "вул.",
    "проспект": "просп.", "просп": "просп.", "пр-т": "просп.", "пр": "просп.",
    "провулок": "пров.", "пров": "пров.", "переулок": "пров.", "пер": "пров.",
    "бульвар": "бульв.", "бульв": "бульв.", "б-р": "бульв.",
    "площа": "пл.", "пл": "пл.", "площадь": "пл.",
    "шосе": "шосе", "шоссе": "шосе",
    "узвіз": "узвіз", "спуск": "узвіз",
    "набережна": "наб.", "наб": "наб.",
    "тупик": "тупик", "туп": "тупик",  # ДТЕК пише повністю: «тупик Орільський»
    "проїзд": "проїзд", "проезд": "проїзд",
    "майдан": "майдан",
}
_TYPE_RE = re.compile(r"(?<![\w'])(" + "|".join(sorted(map(re.escape, STREET_TYPES), key=len, reverse=True)) + r")\.?(?![\w'])")
_LAT_TO_CYR = str.maketrans("ABCEHIKMOPTXaceiopxy", "АВСЕНІКМОРТХасеіорху")


def _norm_text(s: str) -> str:
    s = s.replace("’", "'").replace("ʼ", "'").replace("`", "'").replace("ё", "е").replace("Ё", "Е")
    return re.sub(r"\s+", " ", s).strip()


def street_type(name: str) -> str | None:
    m = _TYPE_RE.search(_norm_text(name).lower())
    return STREET_TYPES[m.group(1)] if m else None


_RU_TO_UK = str.maketrans({"ы": "и", "э": "е", "ъ": "'"})


def street_core(name: str) -> str:
    """«Соколина вулиця» / «вул. Соколина» -> «соколина»; «просп. Дмитра Яворницького» -> «дмитра яворницького».
    Лише для порівняння: регістр, тип і апострофи не важать, рос. «Янтарный» = «Янтарний».
    У запит до сайту йде назва з переліку ДТЕК як є."""
    t = _TYPE_RE.sub(" ", _norm_text(name).lower()).translate(_RU_TO_UK)
    t = re.sub(r"[^\w' -]", " ", t)
    return re.sub(r"\s+", " ", t).strip(" -")


def _core_tokens(name: str) -> tuple[str, ...]:
    return tuple(sorted(street_core(name).split()))


def match_street(query: str, streets: list[str]) -> str | None:
    """Найкраща назва вулиці з переліку ДТЕК для назви з OSM чи тексту людини. None — не знайшли."""
    q_core, q_tokens, q_type = street_core(query), _core_tokens(query), street_type(query)
    if not q_core:
        return None
    exact = [s for s in streets if street_core(s) == q_core or _core_tokens(s) == q_tokens]
    if exact:
        same_type = [s for s in exact if street_type(s) == q_type]
        return (same_type or exact)[0]
    # «Яворницького» -> «просп. Дмитра Яворницького»: усі слова запиту є в назві, і вона однозначна
    contains = [s for s in streets if set(q_tokens) <= set(_core_tokens(s))]
    if len(contains) == 1:
        return contains[0]
    if len(contains) > 1:
        same_type = [s for s in contains if street_type(s) == q_type]
        if len(same_type) == 1:
            return same_type[0]
    cores = {street_core(s): s for s in streets}
    close = difflib.get_close_matches(q_core, list(cores), n=1, cutoff=0.88)
    return cores[close[0]] if close else None


def street_options(query: str, streets: list[str], limit: int = 6) -> list[str]:
    """Варіанти вулиць ДТЕК для неточної назви — кнопками людині. «Шевченка» -> вул. / пров. Шевченка;
    «шевч» -> усе, де є слово, що так починається; одруківка -> найближчі за написанням."""
    q_tokens = _core_tokens(query)
    q_type = street_type(query)
    if not q_tokens:
        return []

    def has_all(s: str) -> bool:
        tokens = _core_tokens(s)
        return all(any(t.startswith(q) for t in tokens) for q in q_tokens)

    found = [s for s in streets if has_all(s)]
    if not found:
        cores = {street_core(s): s for s in streets}
        found = [cores[c] for c in difflib.get_close_matches(street_core(query), list(cores), n=limit, cutoff=0.75)]
        return found[:limit]
    return sorted(found, key=lambda s: (street_type(s) != q_type, len(s), s))[:limit]


def guess_dtek_names(query: str) -> list[str]:
    """Коли переліку вулиць ДТЕК немає: «Соколина вулиця» -> ["вул. Соколина"]; без типу — кілька варіантів."""
    words = _TYPE_RE.sub(" ", _norm_text(query)).split()
    core = " ".join(w[:1].upper() + w[1:] for w in words if w)
    if not core:
        return []
    t = street_type(query)
    return [f"{t} {core}"] if t else [f"вул. {core}", f"просп. {core}", f"пров. {core}"]


def norm_house(h: str) -> str:
    """«14 а» / «14-а» / «14a» -> «14А»; «14/2» лишається; «буд. 5» -> «5»."""
    s = _norm_text(str(h)).upper().translate(_LAT_TO_CYR)
    s = re.sub(r"^(БУД|Д|ДОМ|БУДИНОК)\.?\s*", "", s)
    s = re.sub(r"\s*([/\\])\s*", "/", s)
    s = re.sub(r"(\d)[\s-]+([А-ЯІЇЄҐ])\b", r"\1\2", s)
    return s.replace(" ", "")


def find_house(houses: dict, house: str):
    """houses: {номер_як_на_сайті: дані}. Шукаємо за нормалізованим номером."""
    want = norm_house(house)
    for num, data in houses.items():
        if norm_house(num) == want:
            return num, data
    return None


# «Яворницького 3», «вул. Шевченка, 10а», «просп. Гагаріна 5/2», «Соколина 12 кв 4»
_TEXT_RE = re.compile(r"^(?P<street>.*?[^\d\s,][^,]*?)[,\s]+(?:буд\.?\s*|д\.?\s*)?(?P<house>\d+[\w/-]*)\b")


def parse_text_address(text: str) -> tuple[str, str | None]:
    """«просп. Яворницького, 3» -> ("просп. Яворницького", "3"); без номера -> (вулиця, None)."""
    t = _norm_text(text)
    t = re.sub(r"(?i)\b(м\.?|місто|г\.?|город)\s*дніпро\b,?|\bдніпро\b,?|\bднепр\b,?", " ", t).strip(" ,")
    t = re.sub(r"(?i)[,\s]+(кв|квартира|под|під'їзд|подъезд)\.?\s*\d+.*$", "", t)
    m = _TEXT_RE.match(t)
    if m:
        return m.group("street").strip(" ,"), m.group("house")
    return t.strip(" ,"), None


# ---------- геокодування ----------

_geo_lock = asyncio.Lock()
_geo_last = 0.0


async def reverse_geocode(lat: float, lon: float) -> tuple[str | None, str | None, str | None]:
    """-> (вулиця як в OSM, будинок або None, місто). Координати не логуємо і не зберігаємо."""
    global _geo_last
    async with _geo_lock:  # Nominatim: не частіше 1 запиту на секунду
        wait = 1.1 - (time.monotonic() - _geo_last)
        if wait > 0:
            await asyncio.sleep(wait)
        _geo_last = time.monotonic()
        params = {"format": "jsonv2", "lat": f"{lat:.6f}", "lon": f"{lon:.6f}", "zoom": 18,
                  "addressdetails": 1, "accept-language": "uk"}
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": USER_AGENT}) as c:
            r = await c.get(NOMINATIM_URL, params=params)
            r.raise_for_status()
    a = r.json().get("address") or {}
    road = a.get("road") or a.get("pedestrian") or a.get("residential") or a.get("square")
    city = a.get("city") or a.get("town") or a.get("village")
    return road, a.get("house_number"), city
