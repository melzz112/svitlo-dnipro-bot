"""Розбір повідомлень.

1) parse_channel — пости каналів-агрегаторів (формати описані в PHRASES.md):
   -> список Alert, які бот розсилає людям відповідної групи.
2) parse_user — короткі звіти людей у чатах/коментарях: «1.1 нема світла», «-свет», «3.2 +».
"""
import re
from dataclasses import dataclass

from config import DISTRICTS, GROUPS

# ---------- спільне ----------

# У каналах: лише «1.1». Не чіпає дати/час (19.11.2026, 16.30) і списки будинків «(1, 2, 5)».
GROUP_RE = re.compile(r"(?<![\d.:,/])([1-6])\.([12])(?!\d|:\d)")  # «1.1:» ок, «1.10»/«1.1:30» ні
# У людей: ще «1,1» (без пробілу) і «1. 1»; «1, 2» — це номери будинків
USER_GROUP_RE = re.compile(r"(?<![\d.:,/])([1-6])(?:\s?\.\s?|,)([12])(?!\d|:\d|,\s?\d)")
# «черга 4», «4 група», «очередь 4» -> вся черга (4.1 + 4.2)
QUEUE_RE = re.compile(
    r"(?:черг[аиіу]|очеред[ьи]|груп[аиу])\s*№?\s*([1-6])(?![\d.,])|(?<![\d.,])([1-6])\s*(?:черг|очеред|груп)"
)
TIME_RE = r"(\d{1,2}:\d{2})(?:\s+(\d{2}\.\d{2})(?:\.\d{4})?)?"

# Воєнна цензура: такі повідомлення не обробляємо взагалі
CENSOR = re.compile(
    r"прил[еіi]т|приліт|вибух|взрыв|ппо|пво|ракет|шахед|дрон|бпла|збил|збит|сбил|сбит|уламк|обломк|"
    r"тривог|тревог|пуск|летить|летит|ту-?160|ту-?95|калібр|калибр|стратегічн"
)
# Не про світло
OFF_TOPIC = re.compile(r"водопостач|вод[аиу]\b|тиск|газ\b|газу|опален|тепл|котел|інтернет|интернет|зв.яз|связь")

PROMO = re.compile(r"натискайте|завжди під рукою|обирайте свою черг|підписуйтесь|переходьте за посиланням|знижк|розпродаж")
OTHER_CITY = re.compile(r"самар|кам.янськ|павлоград|кривий ріг|кривой рог|нікопол|марганц|жовті води|синельников")

FOOTERS = re.compile(
    r"Чат-боти аварійних|🌐 Наш сайт|Підписатися на канал|Дніпро Аварійка \| підписатись|Please open Telegram"
)


def norm(text: str) -> str:
    return text.replace("’", "'").replace("`", "'").replace("ʼ", "'").replace("ё", "е").replace("\xa0", " ")


def strip_footer(text: str) -> str:
    return FOOTERS.split(norm(text))[0].strip()


def find_groups(t: str, user=False) -> list[str]:
    """user=True — режим повідомлень людей: ще «1,1» і «черга 4»."""
    found = {f"{a}.{b}" for a, b in (USER_GROUP_RE if user else GROUP_RE).findall(t)}
    if user:
        for a, b in QUEUE_RE.findall(t):
            q = a or b
            found |= {f"{q}.1", f"{q}.2"}
    return sorted(g for g in found if g in GROUPS)


def find_district(t: str) -> str | None:
    return next((name for name, keys in DISTRICTS.items() if any(k in t for k in keys)), None)


def ukey(u: str):
    """Ключ сортування 'HH:MM DD.MM' за (місяць, день, час)."""
    hm, _, dm = u.partition(" ")
    d, _, m = dm.partition(".")
    return (m, d, hm.zfill(5))


def _until(s: str) -> str | None:
    """Останній «до HH:MM [DD.MM]» у рядку -> 'HH:MM DD.MM'."""
    hits = re.findall(r"до\s*\+?-?\s*" + TIME_RE, s)
    if not hits:
        return None
    hm, dm = hits[-1]
    return f"{hm} {dm}".strip()


# ---------- 1) пости каналів ----------


@dataclass
class Alert:
    kind: str  # off | on | general
    grp: str  # "3.2" або "*" (для всіх)
    until: str | None
    text: str  # готовий текст для пуша
    provider: str = "dtek"  # dtek | cek | * (обом)


# ---------- ЦЕК ----------

MONTHS = {
    "січ": 1, "лют": 2, "берез": 3, "квіт": 4, "трав": 5, "черв": 6,
    "лип": 7, "серп": 8, "верес": 9, "жовт": 10, "листопад": 11, "груд": 12,
}
INTERVAL_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[—–-]\s*(\d{1,2}):(\d{2})")


def _cek_date(t: str, today):
    """«графік на 2026-11-19» / «на 20 листопада» / без дати -> сьогодні."""
    from datetime import date

    if m := re.search(r"(\d{4})-(\d{2})-(\d{2})", t):
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if m := re.search(r"(\d{1,2})\s+(січ|лют|берез|квіт|трав|черв|лип|серп|верес|жовт|листопад|груд)\w*", t):
        month = MONTHS[m.group(2)]
        year = today.year + (1 if month < today.month - 6 else 0)  # графік на січень, а зараз грудень
        return date(year, month, int(m.group(1)))
    return today


def parse_cek_schedule(raw: str, today) -> tuple | None:
    """Пост з графіком ЦЕК -> (date, {"1.1": [(9.5, 13.0), ...]}). Груп може бути не всі (зміни)."""
    t = strip_footer(raw).lower()
    if "цек" not in t or not re.search(r"графік|зміни для", t):
        return None
    t = t[t.rfind("\n", 0, t.index("цек")) + 1:]  # у комбо-постах «ДТЕК + ЦЕК» беремо лише частину ЦЕК
    groups: dict[str, list] = {}
    cur = None
    for line in t.splitlines():
        if gs := find_groups(line):
            cur = gs[0]
            groups.setdefault(cur, [])
        if cur:
            for h1, m1, h2, m2 in INTERVAL_RE.findall(line):
                groups[cur].append((int(h1) + int(m1) / 60, int(h2) + int(m2) / 60))
    groups = {g: v for g, v in groups.items() if v or "графік" in t}
    if len(groups) < 3:  # це не графік, а одне попередження
        return None
    return _cek_date(t, today), groups


def _parse_cek(text: str, t: str) -> list[Alert]:
    """Не-графікові пости ЦЕК: зміни в ГПВ, скасування."""
    if re.search(r"скасуван|скасован", t):
        return [Alert("general", "*", None, "📢 " + _general(text).text[2:], "cek")]
    # «ЦЕК - зміни в ГПВ на 19 ЛИСТОПАДА … з 10:20 до 12:30 відключаються черги: 2.1, 2.2, 5.1»
    if "зміни" in t and (m := re.search(r"з\s+(\d{1,2}:\d{2})\s+до\s+(\d{1,2}:\d{2})", t)):
        frm, until = m.groups()
        return [
            Alert("off", g, until, f"⚠️ <b>ЦЕК: зміни в графіку</b> · група <b>{g}</b>\n⏱ Відключення з <b>{frm}</b> до <b>{until}</b>", "cek")
            for g in find_groups(t)
        ]
    return []


def _off(grp, until, label="Аварійне/екстрене відключення"):
    tail = f"\n⏱ Орієнтовно до <b>{until}</b>" if until else ""
    return Alert("off", grp, until, f"🚨 <b>{label}</b> · група <b>{grp}</b>{tail}")


def _on(grp, label):
    return Alert("on", grp, None, f"✅ Група <b>{grp}</b>: {label}")


def _general(text: str) -> Alert:
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    short = "\n".join(lines[:4])
    if len(lines) > 4 or len(short) > 300:
        short = short[:300] + "…"
    return Alert("general", "*", None, f"📢 {short}")


def dedup_text(text: str) -> str:
    """Канали постять одне й те саме двічі (з переносами і «сплющено») — ключ без пробілів."""
    return re.sub(r"\W+", "", strip_footer(text).lower())[:160]


def _dnipro_until(t: str) -> str | None:
    """Найпізніший «до …» саме для м. Дніпро (у постах купа сіл і Кривий Ріг)."""
    cands = [
        f"{hm} {dm}".strip()
        for hm, dm, places in re.findall(r"\(до " + TIME_RE + r"\)\s*:\s*([^\n]*)", t)
        if "м. дніпро" in places
    ]
    section = ""
    for line in t.splitlines():
        s = line.strip()
        if s.endswith(":") and not s.startswith("•"):
            section = s
        elif s.startswith("•") and "дніпро" in section and not re.search(r"(?<!\w)с\.|с-ще|смт", s):
            if u := _until(s):
                cands.append(u)
    if not cands and "дніпро" in t:
        cands = [u for u in [_until(t.split("\n", 1)[0])] if u]
    return max(cands, key=ukey) if cands else None


def parse_channel(raw: str) -> list[Alert]:
    alerts = _parse_channel(raw)
    t = raw.lower()
    for a in alerts:  # загальні без згадки ДТЕК (накази Укренерго тощо) — обом постачальникам
        if a.kind == "general" and a.provider == "dtek" and not re.search(r"дтек|дтэк", t):
            a.provider = "*"
    return alerts


def _parse_channel(raw: str) -> list[Alert]:
    text = strip_footer(raw)
    t = text.lower()
    first = t.split("\n", 1)[0]
    if not text or CENSOR.search(t):
        return []
    if PROMO.search(t):  # реклама інших ботів/каналів
        return []
    if "цек" in t:
        # «ЦЕК /ДТЭК аварійні, графіки не працюють» — обом; інакше — лише абонентам ЦЕК
        if re.search(r"дтек|дтэк", t) and re.search(r"аварій|екстрен|экстрен|не працюють|не работают", t):
            return [Alert("general", "*", None, _general(text).text, "*")]
        if parse_cek_schedule(raw, __import__("datetime").date.today()):
            return []  # графік — зберігає userbot, пуш «графік оновився» шле notifier
        if t.startswith("⚠️ увага цек") or "готуємось до відключення" in t:
            return []  # нагадування за графіком — бот і сам нагадує
        return _parse_cek(text, t)
    if "дніпр" not in t and "днепр" not in t and OTHER_CITY.search(t):  # пост лише про інше місто
        return []
    if not re.search(r"світл|свет|відключ|отключ|відновл|знеструм|електро|черг|груп|графік|дтек", t):
        return []
    if OFF_TOPIC.search(t):
        return []

    # «Екстрені відключення СКАСОВАНО» / «Графік ДТЕК скасовано» — важливо всім (далі може йти графік)
    if re.search(r"(екстрен\w*|аварійн\w*) відключ\w*\s+скасован|графік\w*[^\n]{0,15}скасован", t):
        return [_general(text.split("\n", 1)[0] if "скасован" in first else text)]
    # графіки на завтра/сьогодні беремо з сайту ДТЕК, а не з каналу
    if re.search(r"графік[^\n]{0,40}на (завтра|\d{1,2}\.\d{2})|коригування графіку", t) or first.startswith("графік"):
        return []
    # адресні відключення (планові або «за наступними адресами») — окрема фіча (пошук по вулиці)
    if re.search(r"планов|за (наступними |такими )?адресами", t) and "екстрен" not in t:
        return []

    # ✅ ДТЕК: активне відключення не підтверджується / Група: 1.2
    if "не підтверджується" in t:  # ДТЕК перевіряє лише контрольні адреси — це часто «частково»
        label = "за контрольними адресами ДТЕК відключення не підтверджується\nℹ️ Частині адрес могли дати світло, а частині — ще ні"
        return [_on(g, label) for g in find_groups(t)]

    # 🚨 ДТЕК: аварійне / екстрене відключення світла / Група: 5.1 / ⏱️ з … — до 22:25 19.11.2026
    if re.search(r"аварійне\s*/\s*екстрене", t):
        return [_off(g, _until(t)) for g in find_groups(t)]

    # 🔁 зміни / 🚨 поточні: «• Черга 3.1 · аварійне: ➕ до 18:25 19.11» / «було … → стало 21:05 19.11»
    if re.search(r"зміни по відключеннях|поточні відключення", t):
        best: dict[str, str] = {}
        for line in t.splitlines() if "\n" in t else t.split("•"):
            m = re.search(r"стало\s+" + TIME_RE, line)
            until = f"{m.group(1)} {m.group(2) or ''}".strip() if m else _until(line)
            for g in find_groups(line):
                if until and (g not in best or ukey(until) > ukey(best[g])):
                    best[g] = until
        return [_off(g, u) for g, u in best.items()]

    # ОПЕРАТИВНЕ ЗВЕДЕННЯ (Група 1.1) / «По лінії групи ДТЕК 5.1 — м. Дніпро: • До 13:15 …» / «По групі ДТЕК 6.2»
    if re.search(r"оперативне зведення|по лінії|по групі", t):
        gs = find_groups(first) or find_groups(t[:80])
        until = _dnipro_until(t)
        return [_off(g, until) for g in gs] if until else []

    groups = find_groups(t)

    # «По групах 3.1 3.2 … можлива затримка відновлення до 21:00» / «очікуєм до 22:30»
    if groups and re.search(r"затримка|очіку[єю]м?|очікується|термін відновлення", t):
        return [_off(g, _until(t), "Світла ще немає, відновлення очікується") for g in groups]

    # «Група ДТЕК 1.1 1.2 розпочато відновлення» / «поступово триває відновлення»
    if groups and "відновлен" in t and not re.search(r"знеструм|відключ[^\n]*до", t):
        return [_on(g, "триває відновлення електроенергії") for g in groups]

    # Списки адрес без груп — окрема фіча (пошук по вулиці)
    if not groups and t.count("вул.") >= 3:
        return []

    # Загальне: «Діють екстрені відключення…», «ЭКСТРЕННЫЕ ОТКЛЮЧЕНИЯ», «Споживачі які знеструмлені … відновлення до …»
    if not groups and re.search(
        r"екстрен\w* відключ|экстренн\w* отключ|вимикатимуть по групах|відключення по групах|знеструмлен", t
    ):
        return [_general(text)]
    return []


# ---------- 2) повідомлення людей ----------

OFF_PHRASES = [
    "нема світла", "немає світла", "нема света", "нету света", "нету свет", "нет света", "нет свет",
    "світла нема", "світла немає", "света нет", "света нету", "без світла", "без света",
    "вимкнули", "вимкнуло", "виключили", "выключили", "вирубили", "вырубили", "вырубило", "вирубило",
    "відключили", "отключили", "отключило", "отрубили", "пропал свет", "пропало світло", "пропало", "пропал",
    "зникло світло", "зникло", "погасло", "погас", "тьма", "темно", "минус свет", "мінус світло",
    "знову вимкнули", "опять вырубили", "опять отключили", "сидимо без", "сидим без",
]
ON_PHRASES = [
    "є світло", "есть свет", "свет есть", "світло є", "дали світло", "дали свет", "дали", "включили",
    "увімкнули", "ввімкнули", "вімкнули", "з'явилось", "з'явилося", "появился свет", "появилось", "світло прийшло",
    "свет пришел", "плюс свет", "плюс світло", "засвітились", "загорелся", "дали наконец",
]
QUESTION = re.compile(
    r"\?|(?<!\w)(коли|когда|у кого|у когось|хтось|кто-?нибудь|кто-?то|підкаж\w*|подскаж\w*|не знаете|не знаєте)(?!\w)"
)
SIGN_OFF = re.compile(r"(?:^|\s)[-−–]\s*(?:свет|світло|св)(?!\w)|^\s*[-−–]{1,3}\s*$")
SIGN_ON = re.compile(r"(?:^|\s)\+\s*(?:свет|світло|св)(?!\w)|^\s*\+{1,3}\s*$")
NEGATED_ON = re.compile(r"(?<!\w)(не|ще не|еще не|досі не|так и не|так і не)\s+(дали|включили|увімкнули|ввімкнули|з'явил\w*)")
# «не выключили», «не відключали» — світло є, але це не звіт «дали»; краще пропустити
NEGATED_OFF = re.compile(r"(?<!\w)не\s+(вы|ви)?(ключили|ключали)(?!\w)|(?<!\w)не\s+(відключ|отключ|вимкн|вируб|выруб)\w*")
# побажання/страхи: «хоть бы не отключили», «аби не вимкнули», «надеюсь дадут»
WISH = re.compile(
    r"(?<!\w)(хоть бы|хотя бы|лишь бы|только бы|аби не|аби|щоб не|чтоб не|чтобы не|якби|если бы|надеюсь|сподіваюсь|сподіваюся|мабуть|наверное|может|хочу|хочется|хочеться)(?!\w)"
)


def _has(t: str, phrases: list[str]) -> bool:
    return any(re.search(r"(?<!\w)" + re.escape(p) + r"(?!\w)", t) for p in phrases)


def parse_user(raw: str) -> dict | None:
    """«1.1 нема світла» -> {"event": "off", "groups": ["1.1"], "district": None}."""
    t = norm(raw).lower().strip()
    if not t or len(t) > 200 or CENSOR.search(t) or OFF_TOPIC.search(t) or QUESTION.search(t) or WISH.search(t):
        return None
    if NEGATED_OFF.search(t) and not _has(t, ON_PHRASES):  # «его со вчера не выключили»
        return None
    groups = find_groups(t, user=True)
    rest = QUEUE_RE.sub(" ", USER_GROUP_RE.sub(" ", t)).strip()  # без номерів, щоб «1.1 -» дало «-»
    off = _has(t, OFF_PHRASES) or bool(SIGN_OFF.search(rest))
    on = _has(t, ON_PHRASES) or bool(SIGN_ON.search(rest))
    if on and NEGATED_ON.search(t):  # «не дали», «ще не включили» -> це «нема»
        on, off = False, True
    if on == off:
        return None
    provider = "cek" if "цек" in t else "dtek" if re.search(r"дтек|дтэк", t) else ""
    return {
        "event": "on" if on else "off", "groups": groups, "district": find_district(t),
        "street": find_street(raw), "provider": provider,
    }


# «вул. Шевченка», «на Слобожанском проспекте», «ул Титова 12», «просп. Гагаріна»
STREET_RE = re.compile(
    r"(?:вул\.?|вулиц[яіею]|ул\.?|улиц[аеуы]|просп\.?|проспект[іеу]?|пр-т|пров\.?|провулок|переулок|бульв\.?|бульвар|шосе|шоссе)"
    r"\s+([A-ZА-ЯІЇЄҐ][\w'’\-]+(?:\s+[A-ZА-ЯІЇЄҐ][\w'’\-]+)?(?:\s+\d+[а-яА-Я]?)?)"
)


def find_street(raw: str) -> str | None:
    m = STREET_RE.search(norm(raw))
    return m.group(1).strip() if m else None
