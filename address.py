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
    "ж/м": "ж/м", "жм": "ж/м", "масив": "ж/м", "житловий масив": "ж/м", "жилмассив": "ж/м",
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


# Перейменовані вулиці Дніпра: люди досі пишуть старі назви, а в ДТЕК — лише нові
OLD_NAMES = {
    "карла маркса": "просп. Яворницького",
    "кірова": "просп. Олександра Поля", "кирова": "просп. Олександра Поля",
    "калініна": "просп. Мануйлівський", "калинина": "просп. Мануйлівський",
    "петровського": "просп. Богдана Хмельницького", "петровского": "просп. Богдана Хмельницького",
    "комсомольська": "вул. Старокозацька", "комсомольская": "вул. Старокозацька",
    "газети правди": "просп. Слобожанський", "газеты правды": "просп. Слобожанський",
    "запорожское": "Запорізьке шосе", "запорожское шоссе": "Запорізьке шосе",
}


def _skel(s: str) -> str:
    """Спільний «скелет» укр./рос. написання: «Героев»~«Героїв», «Мазепы»~«Мазепи», «Хмельницкого»~«Хмельницького»."""
    t = _norm_text(s).lower().replace("'", "").replace("ь", "").replace("ъ", "")
    t = t.replace("ськ", "ск").replace("цьк", "цк").replace("ґ", "г")
    return t.translate(str.maketrans({"і": "и", "ї": "и", "ы": "и", "є": "е", "э": "е", "ё": "е"}))


def _tokens_close(a: str, b: str) -> bool:
    x, y = _skel(a), _skel(b)
    if x == y:
        return True
    common = next((i for i, (p, q) in enumerate(zip(x, y)) if p != q), min(len(x), len(y)))
    if min(len(x), len(y)) >= 5 and common >= max(5, min(len(x), len(y)) - 3):
        return True  # «космическая»~«космічна»: відрізняються лише закінченням
    return min(len(x), len(y)) >= 5 and difflib.SequenceMatcher(None, x, y).ratio() >= 0.8


def _old_name(query: str) -> str:
    core = street_core(query)
    for old, new in OLD_NAMES.items():
        if core == old or _skel(core) == _skel(old):
            return new
    return query


def _buckets(query: str, streets: list[str]) -> tuple[list[str], list[str], list[str]]:
    """(точний збіг, усі слова запиту є в назві, схожі слова — рос. написання / закінчення)."""
    q_core, q_tokens = street_core(query), _core_tokens(query)
    if not q_core:
        return [], [], []
    exact = [s for s in streets if street_core(s) == q_core or _core_tokens(s) == q_tokens]
    contains = [s for s in streets if s not in exact and set(q_tokens) <= set(_core_tokens(s))]
    seen = set(exact) | set(contains)
    if not exact and not contains and len(q_tokens) > 1:
        contains = [s for s in streets if (t := set(_core_tokens(s))) and t < set(q_tokens)
                    and max(len(x) for x in t) >= 5]
    if exact or contains:  # є справжній збіг — схожі назви («Тилова» для «Титова») не беремо
        return exact, contains, []
    fuzzy = [s for s in streets if s not in seen
             and all(any(_tokens_close(q, t) for t in _core_tokens(s)) for q in q_tokens)]
    # «Героев» -> «просп. Героїв» (одне слово, як у запиті) раніше за «вул. Героїв Дніпра»
    fuzzy.sort(key=lambda s: (len(_core_tokens(s)) != len(q_tokens), len(s)))
    return exact, contains, fuzzy


def street_matches(query: str, streets: list[str], limit: int = 8) -> list[str]:
    """Усі правдоподібні вулиці ДТЕК, найкращі першими: спершу того ж типу («просп.»), потім точні, потім
    «усі слова є в назві», потім схожі. Пошук будинку перебирає їх і бере ту, де такий будинок справді є."""
    query = _old_name(query)
    q_type = street_type(query)
    exact, contains, fuzzy = _buckets(query, streets)
    ordered = [s for b in (exact, contains, fuzzy) for s in b if q_type and street_type(s) == q_type]
    ordered += [s for b in (exact, contains, fuzzy) for s in b]
    return list(dict.fromkeys(ordered))[:limit]


def match_street(query: str, streets: list[str]) -> str | None:
    """Найкраща назва вулиці з переліку ДТЕК для назви з OSM чи тексту людини. None — не знайшли."""
    query = _old_name(query)
    q_type = street_type(query)
    exact, contains, fuzzy = _buckets(query, streets)
    if not (exact or contains or fuzzy):
        cores = {street_core(s): s for s in streets}
        close = difflib.get_close_matches(street_core(query), list(cores), n=1, cutoff=0.88)
        return cores[close[0]] if close else None
    if q_type:  # «просп. Яворницького» — проспект, навіть якщо є й «вул. Яворницького»
        for b in (exact, contains, fuzzy):
            same = [s for s in b if street_type(s) == q_type]
            if len(same) == 1 or (b is exact and same):
                return same[0]
    if exact:
        return exact[0]
    # «Яворницького» -> «просп. Дмитра Яворницького»: усі слова запиту є в назві, і вона однозначна
    for b in (contains, fuzzy):
        if len(b) == 1:
            return b[0]
        same = [s for s in b if street_type(s) == q_type]
        if len(same) == 1:
            return same[0]
    return None


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

    found = list(dict.fromkeys(street_matches(query, streets, limit) + [s for s in streets if has_all(s)]))
    if not found:
        cores = {street_core(s): s for s in streets}
        found = [cores[c] for c in difflib.get_close_matches(street_core(query), list(cores), n=limit, cutoff=0.75)]
        return found[:limit]
    first = street_matches(query, streets, limit)
    rest = sorted((s for s in found if s not in first), key=lambda s: (street_type(s) != q_type, len(s), s))
    return (first + rest)[:limit]


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
