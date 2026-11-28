"""Вулиця з повідомлення в чаті -> черга за довідником адрес ДТЕК (dtek_streets / dtek_houses).

Люди рідко пишуть номер групи (≈40% повідомлень), а часто — старий (1.2, 5.1). Зате пишуть вулицю:
«на Калиновій нема», «ул Тополиная 13 дали», «Яворницкого света нет». Тут:
- вулицю шукаємо за довідником (2246 назв): за основою слова, тож «Калинова/Калиновій/Калиновая» — одна вулиця;
- щоб звичайні слова («робоча», «нова», «садова») не ставали вулицями, вулицю беремо лише якщо поруч тип
  («вул», «ул», «улица», «просп», «пров», «на вулиці»…), або одразу після неї номер будинку, або назва рідкісна
  й довга (≥ 9 літер, як «Яворницького»);
- черга: є номер будинку — з довідника саме цього будинку; немає — переважна черга вулиці (≥ 80% будинків).
"""
import re
import time

from address import norm_house, street_core

TYPE_WORDS = re.compile(
    r"(?<!\w)(?:вул|вулиц\w*|ул|улиц\w*|просп\w*|пр-т|пр|пров\w*|пер|переул\w*|бульв\w*|б-р|шосе|шоссе|"
    r"площ\w*|пл|узвіз|спуск|набережн\w*|наб|тупик|проїзд|проезд)\.?$"
)
HOUSE_AFTER = re.compile(r"^[\s,.]*(?:буд\.?|д\.?|дом)?\s*(\d{1,4}(?:\s?[а-яієїґa-z](?![а-яієїґa-z]))?(?:/\d+)?)(?!\d)")
WORD = re.compile(r"[а-яієїґa-z'’]+", re.I)
_RU_TO_UK = str.maketrans({"ы": "и", "э": "е", "ъ": "", "ё": "е"})

LIVE_MIN_HOUSES = 30  # черга «жива» в Дніпрі, якщо на ній стільки будинків довідника (1.2 / 5.1 — ні)
DOMINANT_SHARE = 0.8
REBUILD_SEC = 6 * 3600
RARE_LEN = 9


def _district_res():
    from config import DISTRICTS

    return [re.compile(r"^" + k) for keys in DISTRICTS.values() for k in keys]


DISTRICT_RES = _district_res()  # «Березинка», «Тополь-3», «Перемога» — масиви, не вулиці

def _norm(token: str) -> str:
    t = token.lower().replace("’", "'").translate(_RU_TO_UK)
    t = t.replace("ськ", "ск").replace("цьк", "цк").replace("'", "")
    return t.replace("і", "и").replace("ї", "и").replace("є", "е")


def _stem(token: str) -> str:
    """Основа для пошуку кандидатів: «калинова/калиновій/калиновая» -> «калино»."""
    t = _norm(token)
    return t[:6] if len(t) >= 7 else t[:max(4, len(t) - 1)]


def _close(word: str, key: str) -> bool:
    """Відрізняються лише закінченням: «березинская»~«березинська», але «новокадацкий»≁«новокаховська»."""
    a, b = _norm(word), _norm(key)
    common = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    return common >= max(5, min(len(a), len(b)) - 3)


class StreetIndex:
    def __init__(self):
        self.by_stem: dict[str, list[dict]] = {}
        self.live: set[str] = set()
        self.built = 0.0

    async def build(self):
        import db

        rows = await db._all(
            "SELECT s.raw_name, h.group_name g, h.operator o, COUNT(*) n FROM dtek_streets s "
            "JOIN dtek_houses h ON h.street_id=s.id GROUP BY s.id, h.group_name, h.operator")
        streets: dict[str, dict] = {}
        totals: dict[str, int] = {}
        for r in rows:
            st = streets.setdefault(r["raw_name"], {"name": r["raw_name"], "groups": {}, "houses": 0})
            st["houses"] += r["n"]
            if r["g"] and r["o"] == "dtek":
                st["groups"][r["g"]] = st["groups"].get(r["g"], 0) + r["n"]
                totals[r["g"]] = totals.get(r["g"], 0) + r["n"]
        self.live = {g for g, n in totals.items() if n >= LIVE_MIN_HOUSES}
        by_stem: dict[str, list[dict]] = {}
        for st in streets.values():
            tokens = [t for t in street_core(st["name"]).split() if len(t) >= 4 and not t.isdigit()]
            if not tokens:
                continue
            key = max(tokens, key=len)  # «дмитра яворницького» -> «яворницького»
            g, n = max(st["groups"].items(), key=lambda x: x[1], default=(None, 0))
            st["dominant"] = g if st["houses"] and n >= DOMINANT_SHARE * st["houses"] else None
            st["key_len"] = len(key)
            st["key"] = key
            st["stems"] = [_stem(t) for t in tokens]
            by_stem.setdefault(_stem(key), []).append(st)
        self.by_stem = by_stem
        self.built = time.time()

    async def ensure(self):
        if time.time() - self.built > REBUILD_SEC:
            await self.build()

    async def match(self, text: str) -> tuple[str, str | None, str | None] | None:
        """-> (назва вулиці як у ДТЕК, черга або None, номер будинку або None). None — вулиці не знайшли."""
        import db

        await self.ensure()
        low = (text or "").lower()
        words = list(WORD.finditer(low))
        stems = [_stem(w.group()) for w in words]
        for i, w in enumerate(words):
            if len(w.group()) < 4 or TYPE_WORDS.search(w.group()):
                continue  # «набережна», «проспект» — тип вулиці, не назва
            cands = [c for c in self.by_stem.get(stems[i], []) if _close(w.group(), c["key"])]
            if not cands:
                continue
            # повна назва поруч («дмитра яворницького») — перевіряємо першою; «яворницького» саме по собі
            # підходить і вулиці, і проспекту — тоді без номера будинку не вгадуємо (різні черги)
            near = set(stems[max(0, i - 3):i + 4])
            cands = sorted(cands, key=lambda c: -(len(c["stems"]) > 1 and all(s in near for s in c["stems"])))
            before = low[:w.start()].rstrip()
            typed = bool(TYPE_WORDS.search(before.split()[-1] if before.split() else "")) or bool(
                re.search(r"(?:на|по)\s+(?:вул\w*|ул\w*)\s*$", before))
            m_house = HOUSE_AFTER.match(low[w.end():])
            house = norm_house(m_house.group(1)) if m_house else None
            rare = len(w.group()) >= RARE_LEN
            if any(r.match(w.group()) for r in DISTRICT_RES) and not typed:
                continue  # «на Березинке», «Тополь 3» — масив, а не вул. Березинська / с-т Тополь
            if not (typed or house or rare):
                continue
            groups = {c.get("dominant") for c in cands}
            if house:
                for c in cands:
                    if (h := await db.find_house(c["name"], house)) and h["operator"] == "dtek":
                        return c["name"], h["group_name"], house
            if len(cands) == 1 or len(groups) == 1:
                return cands[0]["name"], cands[0].get("dominant"), house
        return None


index = StreetIndex()


async def apply(res: dict, text: str) -> dict:
    """Розібране повідомлення (parse_user) -> вулиця з довідника і черга за нею, якщо групи немає чи вона стара."""
    hit = await index.match(text)
    if not hit:
        return res
    name, grp, _house = hit
    res = {**res, "street": name}
    if grp and (not res.get("groups") or all(g not in index.live for g in res["groups"])):
        res["groups"] = [grp]
        res["provider"] = res.get("provider") or "dtek"
    return res
