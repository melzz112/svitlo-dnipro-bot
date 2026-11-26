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
    r"Чат-боти аварійних|🌐 Наш сайт|Підписатися на канал|Дніпро Аварійка \| підписатись|Please open Telegram|"
    r"[^\n|]{0,40}\|\s*(?:Подписаться|Подписаться на канал|Підписатися|Підписатись|Подпишись)"  # «⚡ Свет Днепр | Подписаться»
)


def norm(text: str) -> str:
    return text.replace("’", "'").replace("`", "'").replace("ʼ", "'").replace("ё", "е").replace("\xa0", " ")


# емодзі (з варіаційним селектором / ZWJ): «🛑», «‼️», «🔴»
_EMOJI = r"(?:[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u203C\u2049\u2190-\u21FF\u25A0-\u25FF][\uFE0F\u200D]*)"
EMOJI_RUN = re.compile(rf"({_EMOJI})(?:\s*{_EMOJI}){{2,}}")
PUNCT_RUN = re.compile(r"([!?.)(])\1{3,}")


def clean_chat_text(text: str) -> str:
    """Спам у чатах: «🛑🛑🛑…🛑1.2 Анд …🛑🛑🛑», «‼️‼️‼️‼️ група 1.2 …», «!!!!!!!» -> один символ / «!!!»."""
    t = EMOJI_RUN.sub(lambda m: m.group(1) + " ", URL_RE.sub("", text or ""))
    t = PUNCT_RUN.sub(lambda m: m.group(1) * 3, t)
    return re.sub(r"[ \t]+", " ", t).strip()


def chat_key(text: str) -> str:
    """Ключ «те саме повідомлення»: без емодзі, розділових знаків, пробілів і регістру."""
    return re.sub(r"\W+", "", norm(text).lower())[:200]


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
    t = t.lower()
    return next(
        (name for name, keys in DISTRICTS.items() if any(re.search(r"(?<!\w)" + k, t) for k in keys)), None
    )


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


def _post_date(t: str, today):
    """«на завтра, 20.11.2026» / «графік на 2026-11-19» / «на 20 листопада» / «на завтра» / без дати -> сьогодні."""
    from datetime import date, timedelta

    if m := re.search(r"(?<!\d)(\d{2})\.(\d{2})\.(\d{4})(?!\d)", t):
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    if m := re.search(r"(\d{4})-(\d{2})-(\d{2})", t):
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if m := re.search(r"(\d{1,2})\s+(січ|лют|берез|квіт|трав|черв|лип|серп|верес|жовт|листопад|груд)\w*", t):
        month = MONTHS[m.group(2)]
        year = today.year + (1 if month < today.month - 6 else 0)  # графік на січень, а зараз грудень
        return date(year, month, int(m.group(1)))
    if re.search(r"на завтра", t):
        return today + timedelta(days=1)
    return today


PROVIDER_RE = re.compile(r"цек|дтек|дтэк")
NO_OUTAGE_RE = re.compile(r"не прогнозу\w*|не плану\w*|відключень немає|без відключень|відключення відсутні")


def parse_schedule_posts(raw: str, today) -> list[tuple]:
    """Пост із графіком ДТЕК і/або ЦЕК -> [(provider, date, {"1.1": [(9.5, 13.0), ...]}), ...].
    Комбо-пости («СКАСОВАНО / Графік ДТЕК … / Графік для ЦЕК …») ріжемо на частини за назвою постачальника.
    Груп може бути не всі (коригування / «зміни для ЦЕК») — тоді оновлюються лише перелічені."""
    t = strip_footer(raw).lower()
    if not re.search(r"графік|график|зміни для", t):
        return []
    # межі частин: кожна згадка постачальника; сусідні однакові зливаються
    marks = [(m.start(), "cek" if m.group() == "цек" else "dtek") for m in PROVIDER_RE.finditer(t)]
    parts: list[tuple[str, int, int]] = []
    for i, (pos, prov) in enumerate(marks):
        if parts and parts[-1][0] == prov:
            continue
        parts.append((prov, pos, len(t)))
        if len(parts) > 1:
            p, s, _ = parts[-2]
            parts[-2] = (p, s, pos)
    out = []
    for prov, start, end in parts:
        # початок частини — з початку її рядка (там буває «Графік … на 20.11.2026»)
        seg = t[t.rfind("\n", 0, start) + 1:end]
        tokens = sorted(
            [(m.start(), "g", f"{m.group(1)}.{m.group(2)}") for m in GROUP_RE.finditer(seg)]
            + [(m.start(), "i", m.groups()) for m in INTERVAL_RE.finditer(seg)]
        )
        groups: dict[str, list] = {}
        cur = None
        for _, kind, val in tokens:
            if kind == "g":
                cur = val
                groups.setdefault(cur, [])
            elif cur:
                h1, m1, h2, m2 = map(int, val)
                groups[cur].append((h1 + m1 / 60, h2 + m2 / 60))
        n_intervals = sum(len(v) for v in groups.values())
        no_outages = len(NO_OUTAGE_RE.findall(seg))  # «Група 1.1 / Відключення не прогнозується»
        if not re.search(r"графік|график", seg):
            groups = {g: v for g, v in groups.items() if v}  # «зміни для» — лише групи з інтервалами
        if len(groups) < 3 or (n_intervals == 0 and no_outages < 3):  # не графік, а попередження/зведення
            continue
        out.append((prov, _post_date(seg, today), groups))
    return out


def parse_cek_schedule(raw: str, today) -> tuple | None:
    """Сумісність: лише частина ЦЕК."""
    return next(((d, g) for p, d, g in parse_schedule_posts(raw, today) if p == "cek"), None)


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


URL_RE = re.compile(r"(?:https?://|www\.|t\.me/|telegram\.me/)\S+", re.I)
MENTION_RE = re.compile(r"(?<![\w.])@\w{4,}")


def strip_links(text: str) -> str:
    """Пости каналів -> лише текст: без посилань (https://t.me/+…, www…), @каналів і рядків, які були лише
    підписом до посилання («Графік відключень - https://…» -> рядок прибираємо)."""
    out = []
    for line in text.splitlines():
        if not (URL_RE.search(line) or MENTION_RE.search(line)):
            out.append(line)
            continue
        line = re.sub(r"[ \t]{2,}", " ", MENTION_RE.sub("", URL_RE.sub("", line)))
        line = re.sub(r"[\s:–—\-|→👉⬇️⤵️]+$", "", line).strip()  # «Графік відключень -» без хвоста-стрілки
        if len(re.sub(r"\W+", "", line)) < 25:
            continue  # «Графік відключень», «Підписатись», «👉 @канал» — без посилання сенсу немає
        out.append(line)
    return "\n".join(out)


def _general(text: str) -> Alert:
    lines = [x.strip() for x in strip_links(text).splitlines() if x.strip()]
    if lines:
        lines[0] = re.sub(r"^📢\s*", "", lines[0])  # канал сам починає з 📢 — не дублюємо
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
        if parse_cek_schedule(raw, __import__("datetime").date.today()) or t.startswith("⚡ цек — графік"):
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
    # Графіки обробляє parse_schedule_posts окремо від сповіщень про аварії.
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
# Правила зібрані на ~135 живих повідомленнях з «Днепр свет/вода» і «Включатель чатя» (20.11.2026),
# приклади — у tests/test_parse_live.py.

OFF_PHRASES = [
    "нема світла", "немає світла", "нема света", "нету света", "нету свет", "нет света", "нет свет",
    "світла нема", "світла немає", "света нет", "света нету", "без світла", "без света",
    "вимкнули", "вимкнуло", "виключили", "выключили", "вирубили", "вырубили", "вырубило", "вирубило",
    "відключили", "отключили", "отключило", "отрубили", "отрубило", "офнули", "офнуло",
    "пропал свет", "пропало світло", "пропало", "пропал", "зникло світло", "зникло", "погасло", "погас",
    "тьма", "темно", "минус свет", "мінус світло", "знову вимкнули", "опять вырубили", "опять отключили",
    "сидимо без", "сидим без", "у меня нет", "у меня нету", "у нас нет", "у нас нету", "у мене нема", "у нас нема",
    "офф", "до сих пор нет", "нет до сих пор", "досі нема", "досі немає", "опять нет", "снова нет", "знову нема",
    "знову немає", "и нету", "і нема",
]
ON_PHRASES = [
    "є світло", "есть свет", "свет есть", "світло є", "дали світло", "дали свет", "дали", "включили",
    "увімкнули", "ввімкнули", "вімкнули", "з'явилось", "з'явилося", "появился свет", "появилось", "світло прийшло",
    "свет пришел", "плюс свет", "плюс світло", "засвітились", "загорелся", "дали наконец",
    "повернули світло", "повернули", "вернули свет", "вернули",
]
# «бан дали», «дали график» — «дали» не про світло
DALI_OTHER = re.compile(
    r"(бан|графік|график|пизд\w*|денег|гроші|номер|ответ|відповідь|інфо\w*|инфо\w*)\s+дали|"
    r"дали\s+(бан|графік|график|пизд\w*|денег|гроші|ответ|відповідь|номер|(?:в|у)\s+(?:дтек|дтэк|цек))"
)
QUESTION = re.compile(
    r"\?|(?<!\w)(коли|когда|где|де|кому|у кого|у когось|хтось|кто-?нибудь|кто-?то|підкаж\w*|подскаж\w*|"
    r"не знаете|не знаєте|кому-?нибудь|кому-?небудь)(?![\w-])|(?:комусь|кумусь|кому-?то)\s+дали"
)
# узагальнення/скарги: «мы всегда без света», «Днепр всегда без света» — не звіт про «зараз»
GENERAL = re.compile(
    r"(?<!\w)(всегда|завжди|вечно|вечн\w+|постоянно|постійно|как обычно|як завжди|почти всегда|круглосуточно|цілодобово)(?!\w)"
)
# «дали на 5 минут», «включили на полчаса» — уже, найімовірніше, знову вимкнули; без явного «вимкнули» — не звіт
SHORT_ON = re.compile(
    r"(?:дали|включили|увімкнули|ввімкнули)\s+(?:\w+\s+)?на\s+(?:\d+[.,]?\d*\s*(?:мин|хв|сек)|минут|хвилин|"
    r"пару|полчаса|пів\s?години|півгодини|секунд)"
)
# «дали с 5 до 5:40», «свет был 4 часа», «спустя сутки дали на полтора часа» — про минуле, не про «зараз»
PAST_ON = re.compile(
    r"(?:дали|включили|давали)\s+(?:\w+\s+){0,2}(?:с|з)\s*\d{1,2}(?:[:.]\d{2})?\s*(?:до|по)\s*\d{1,2}|"
    r"(?:с|з)\s*\d{1,2}(?:[:.]\d{2})?\s*(?:до|по)\s*\d{1,2}(?:[:.]\d{2})?\s+(?:дали|давали|был)|(?<!\w)(?:был|была|было|були|був|було|була)\s+(?:свет|світло)|"
    r"(?:свет|світло)\s+(?:был|було|був)(?!\w)|(?<!\w)(?:позавчера|позавчора|вчера|вчора)(?!\w)"
)
# «не у всех свет есть», «дома напротив есть свет» — про інших
OTHERS_ON = re.compile(
    r"не у всех|не у всіх|не всім|не всем|у соседей|у сусідів|напротив|навпроти|рядом|поруч|сусідн|соседн|у других|"
    r"в других|в інших|у інших|вам уже|вам дали|им дали|їм дали|у них"
)
# «дали … и через 10 мин офф» — останній стан важливіший
SEQ = re.compile(r"(?<!\w)(?:и|і|й|потом|потім|через|опять|снова|знову|а потом|а потім|уже|вже)(?!\w)")
# «не знаю как у 3.2, но у 5.1 выключили», «5.1 нет света, зато 5.2 …» — групу з такої частини не беремо
CONTRAST_SPLIT = re.compile(r"[,;.!]*\s+(?:но|але|а|зато|проте|однак|тоді як|в отличие от)\s+|[;!]+|\.\s")
UNKNOWN = re.compile(r"не знаю|не в курсе|не в курсі|хз|без понятия|хто зна|кто знает")
ELSEWHERE = re.compile(r"элитн|еліт|других|інших|чужих|у них|им(?!\w)|їм(?!\w)")
SIGN_OFF = re.compile(r"(?:^|\s)[-−–]\s*(?:свет|світло|св)(?!\w)|^\s*[-−–]{1,3}\s*$")
SIGN_ON = re.compile(r"(?:^|\s)\+\s*(?:свет|світло|св)(?!\w)|^\s*\+{1,3}\s*$")
BARE_SIGN = re.compile(r"^\s*([+\-−–]{1,3})\s*[!)]*\s*$")  # «+», «++», «-» без групи — це реакція на чуже
# «у меня тоже», «і в нас», «аналогічно» — сенс бере з повідомлення, на яке відповіли
AGREE = re.compile(
    r"^\s*(\+{1,3}|тоже|теж|також|и у (меня|нас)|і в (мене|нас)|у (меня|нас) тоже|у (мене|нас) теж|"
    r"аналогично|аналогічно|подтверждаю|підтверджую|same|такая же история|те саме)\W*$"
)
NEGATED_ON = re.compile(r"(?<!\w)(не|ще не|еще не|досі не|так и не|так і не)\s+(дали|включили|увімкнули|ввімкнули|з'явил\w*)")
# «не выключили», «не відключали» — світло є, але це не звіт «дали»; краще пропустити
NEGATED_OFF = re.compile(r"(?<!\w)не\s+(вы|ви)?(ключили|ключали)(?!\w)|(?<!\w)не\s+(відключ|отключ|вимкн|вируб|выруб)\w*")
# Побажання і здогадки — точно не звіт (відкидаємо завжди): «хоть бы не отключили», «думаю», «небось»
STRONG_WISH = re.compile(
    r"(?<!\w)(хоть бы|хотя бы|лишь бы|только бы|что бы не|чтоб не|чтобы не|аби не|щоб не|якби|если бы|надеюсь|"
    r"сподіваюсь|сподіваюся|хочу|хочется|хочеться|желаю|бажаю|думаю|небось|видимо|наверное|наверно|мабуть|напевно)(?!\w)"
)
# М'якші ознаки прогнозу — відкидаємо лише БЕЗ групи (з групою люди пишуть по суті:
# «1.2 Амур дали, может скоро и вам дадут», «1.2 выключили, не будут говорили они»)
WISH = re.compile(r"(?<!\w)(может|аби|навряд)(?!\w)")
FUTURE = re.compile(
    r"(?<!\w)(дадут|дадуть|включат|увімкнуть|відключать|вимкнуть|отключат|выключат|вырубят|вирублять|"
    r"будет|буде|будем|будемо|будут|будуть)(?!\w)"
)
# інші міста: «Львов без света», «в КР подстанцию», «Франківськ»
OTHER_CITY_USER = re.compile(
    r"льв[оі]в|франк[іо]вськ|франковск|ки[еє]в|київ|харк[іо]в|харьков|одес|запор[іо]ж|кривой рог|кривий ріг|"
    r"(?<!\w)кр(?!\w)|кам.янськ|каменск|павлоград|нікопол|никопол|марганц|западн|захід"
)
MAX_USER_LEN = 120  # довші без групи — майже завжди обговорення, не звіт
MAX_USER_LEN_GROUP = 200  # з номером групи люди інколи розписують детальніше — і це звіт
# «1-2» у людей — група 1.2, але не «1-2 часа», «1-2 рази»
DASH_GROUP_RE = re.compile(
    r"(?<![\d.:,/-])([1-6])\s?-\s?([12])(?!\d|\s*(?:год|час|хв|мин|дн|доб|сут|раз|чел|люд|шт|поверх|этаж|тис|тыс))"
)


def _has(t: str, phrases: list[str]) -> bool:
    return any(re.search(r"(?<!\w)" + re.escape(p) + r"(?!\w)", t) for p in phrases)


def _spans(t: str, phrases: list[str]) -> list[tuple[int, int]]:
    out = []
    for p in phrases:
        out += [m.span() for m in re.finditer(r"(?<!\w)" + re.escape(p) + r"(?!\w)", t)]
    return out


def _last_state(t: str, t_on: str) -> str | None:
    """«дали … и через 10 мин офф» -> off; «вирубили, а потім дали» -> on. Лише якщо між ними є «і/потім/через…»
    і мова не про інших («у нас нема, а сусідам дали» — не розбираємо)."""
    on, off = _spans(t_on, ON_PHRASES), _spans(t, OFF_PHRASES)
    if not on or not off or OTHERS_ON.search(t):
        return None
    last_on, last_off = max(e for _, e in on), max(e for _, e in off)
    first_on, first_off = min(s for s, _ in on), min(s for s, _ in off)
    if last_off > last_on and first_on < first_off and SEQ.search(t[first_on:max(s for s, _ in off)]):
        return "off"
    if last_on > last_off and first_off < first_on and SEQ.search(t[first_off:max(s for s, _ in on)]):
        return "on"
    return None


def _clause_event(c: str) -> str | None:
    on, off = _has(DALI_OTHER.sub(" ", c), ON_PHRASES), _has(c, OFF_PHRASES)
    if on and NEGATED_ON.search(c):
        on, off = False, True
    return None if on == off else "on" if on else "off"


def _groups_for(t: str, groups: list[str], event: str) -> list[str]:
    """Кілька груп у повідомленні — лишаємо ті, про які саме цей стан: «не знаю як у 3.2, але в 5.1 вимкнули»
    -> 5.1; «5.1 нет света, зато 5.2 графики» -> 5.1."""
    if len(groups) < 2 and not UNKNOWN.search(t):
        return groups
    clauses = CONTRAST_SPLIT.split(t)
    if len(clauses) < 2:
        return groups
    keep = set()
    for i, c in enumerate(clauses):
        cg = _user_groups(c)
        if not cg:
            continue
        ev = _clause_event(c)
        if ev == event or (ev is None and i == 0 and not UNKNOWN.search(c) and not ELSEWHERE.search(c)):
            keep.update(cg)
    return sorted(keep)


def _user_groups(t: str) -> list[str]:
    found = set(find_groups(t, user=True)) | {f"{a}.{b}" for a, b in DASH_GROUP_RE.findall(t)}
    return sorted(g for g in found if g in GROUPS)


def _parse_one(raw: str) -> dict | None:
    t = norm(raw).lower().strip()
    if not t:
        return None
    groups = _user_groups(t)
    # Повідомлення з номером групи на живих даних точні (~95%), без групи — ~50% шуму:
    # для них фільтри суворіші.
    if len(t) > (MAX_USER_LEN_GROUP if groups else MAX_USER_LEN):
        return None
    if CENSOR.search(t) or OFF_TOPIC.search(t) or QUESTION.search(t) or STRONG_WISH.search(t):
        return None
    if not groups and (WISH.search(t) or FUTURE.search(t) or GENERAL.search(t)):
        return None
    if OTHER_CITY_USER.search(t) and not re.search(r"дніпр|днепр", t):
        return None
    if not groups and re.search(r"дніпр|днепр", t):  # «Днепр без света, а Львов…» — про місто загалом
        return None
    if NEGATED_OFF.search(t) and not _has(t, ON_PHRASES):  # «его со вчера не выключили»
        return None
    rest = DASH_GROUP_RE.sub(" ", QUEUE_RE.sub(" ", USER_GROUP_RE.sub(" ", t))).strip()  # «1.1 -» -> «-»
    if BARE_SIGN.match(rest) and not groups:
        return None  # «+» без групи — реакція; розбирається лише як відповідь (parse_user з reply_raw)
    t_on = DALI_OTHER.sub(" ", t)
    off = _has(t, OFF_PHRASES) or bool(SIGN_OFF.search(rest))
    on = _has(t_on, ON_PHRASES) or bool(SIGN_ON.search(rest))
    if on and NEGATED_ON.search(t):  # «не дали», «ще не включили» -> це «нема»
        on, off = False, True
    if on and off:
        state = _last_state(t, t_on)
        if not state:
            return None
        on, off = state == "on", state == "off"
    if on == off:
        return None
    if on and (SHORT_ON.search(t) or PAST_ON.search(t) or OTHERS_ON.search(t) or GENERAL.search(t)):
        return None
    if groups:
        groups = _groups_for(t, groups, "on" if on else "off")
        if not groups and not _has(t, ["у меня", "у мене", "у нас", "в нас"]):
            return None
    provider = "cek" if "цек" in t else "dtek" if re.search(r"дтек|дтэк", t) else ""
    return {
        "event": "on" if on else "off", "groups": groups, "district": find_district(t),
        "street": find_street(raw), "provider": provider,
    }


def parse_user(raw: str, reply_raw: str | None = None) -> dict | None:
    """«1.1 нема світла» -> {"event": "off", "groups": ["1.1"], ...}.
    reply_raw — текст повідомлення людини, на яке це відповідь: «+» / «у меня тоже» беруть сенс звідти,
    а звіт без групи бере групу звідти («1.2 дали» -> відповідь «а нам не дали» = 1.2, нема)."""
    t = norm(raw).lower().strip()
    parent = _parse_one(reply_raw) if reply_raw else None
    if AGREE.match(t):
        if not parent:
            return None
        return {**parent, "street": None, "district": find_district(t) or parent["district"]}
    res = _parse_one(raw)
    if res and not res["groups"] and parent and parent["groups"]:
        res["groups"] = parent["groups"]
        res["provider"] = res["provider"] or parent["provider"]
    return res


# «вул. Шевченка», «ул Титова 12», «просп. Гагаріна», «пер. Любарського»
STREET_RE = re.compile(
    r"(?:вул\.?|вулиц[яіею]|ул\.?|улиц[аеуы]|просп\.?|проспект[іеу]?|пр-т|пров\.?|провулок|пер\.|переулок|"
    r"бульв\.?|бульвар|шосе|шоссе)"
    r"\s+([A-ZА-ЯІЇЄҐ][\w'’\-]+(?:\s+[A-ZА-ЯІЇЄҐ][\w'’\-]+)?(?:\s+\d+[а-яА-Я]?)?)"
)


def find_street(raw: str) -> str | None:
    m = STREET_RE.search(norm(raw))
    return m.group(1).strip() if m else None


# ---------- 3) вікно «Шановні клієнти!» на сайті ДТЕК ----------
# «За наказом НЕК Укренерго в м. Дніпро, Кам'янське, Самар … р-ни діють екстрені відключення.
#  Для решти області стабілізаційні відключення.» — графік у місті тоді не діє.
EMERGENCY_RE = re.compile(
    r"екстрен\w*\s+(?:відключ|знеструм)|графік\w*\s+аварійних|(?<!\w)гав(?!\w)|графік\w*\s+(?:зараз\s+)?не\s+ді"
)
# «скасовано», «щоб уникнути екстрених відключень», «можливі екстрені» — режиму зараз немає
NOT_ACTIVE_RE = re.compile(
    r"скасов|припинен|завершен|відмін|уникн|запобіг|не допуст|можлив|екстрен\w*\s+відключ\w*\s+не\s+(?:ді|застосов)"
)
CITY_RE = re.compile(r"(?<!\w)дніпр[оіа](?!\w)")  # м. Дніпро / у Дніпрі, але не «Дніпровський р-н»
WHOLE_REGION_RE = re.compile(r"област|(?<!\w)(?:всій|усій)(?!\w)")
EXCEPT_RE = re.compile(r"(?<!\w)(?:крім|окрім|за винятком|решт\w*|інш\w*)(?!\w)")


def emergency_in_city(text: str | None) -> bool:
    """Текст вікна «Шановні клієнти!» -> чи діють екстрені відключення саме в м. Дніпро.
    Дивимось по реченнях: «… діють екстрені. Для решти області стабілізаційні» — так;
    «екстрені по області, крім м. Дніпро» / «для решти області екстрені» — ні."""
    if not text:
        return False
    t = re.sub(r"(?<!\w)(м|с|смт|вул|обл)\.", r"\1 ", norm(text).lower())  # «м. Дніпро» не ріже речення
    for s in re.split(r"[.!;\n]+", t):
        if not EMERGENCY_RE.search(s) or NOT_ACTIVE_RE.search(s):
            continue
        if city := CITY_RE.search(s):
            if not EXCEPT_RE.search(s[: city.start()][-40:]):
                return True
        elif WHOLE_REGION_RE.search(s) and not EXCEPT_RE.search(s):
            return True
    return False
