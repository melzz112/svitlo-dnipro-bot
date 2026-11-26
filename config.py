import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SOURCE = os.getenv("SOURCE", "dtek")
CITY = os.getenv("CITY", "dnipro")
DB_PATH = os.getenv("DB_PATH", "svitlo.db")
TZ = ZoneInfo("Europe/Kyiv")

API_ID = int(os.getenv("API_ID") or 0)
API_HASH = os.getenv("API_HASH", "")
def _list(name):
    return [c.strip() for c in os.getenv(name, "").split(",") if c.strip()]


CHANNELS = _list("CHANNELS")  # канали-агрегатори: пости -> сповіщення; їх коментарі -> звіти людей
CHATS = _list("CHATS")  # додаткові чати, де пишуть люди
ADMIN_IDS = [int(x) for x in _list("ADMIN_IDS") if x.lstrip("-").isdigit()]  # Telegram ID тих, кому доступна /stats
CROWD_MIN = int(os.getenv("CROWD_MIN") or 3)  # скільки різних людей за 15 хв = «люди пишуть, що дали»

GROUPS = [f"{q}.{s}" for q in range(1, 7) for s in (1, 2)]  # 1.1 … 6.2

# «Перевірити адресу» на сайті ДТЕК: які вулиці дивитись. Сайт віддає по кожному будинку його чергу і чи є зараз
# світло (і чому нема — «Екстренні відключення…»), тож кілька довгих вулиць у різних районах = контрольні адреси
# всіх груп. Назви — точно як у полі «Введіть вулицю» на сайті. Яких груп бракує — видно в логах і в
# `python dtek_site.py`.
DTEK_STREETS = _list("DTEK_STREETS") or [
    "вул. Микитинська",
    "просп. Дмитра Яворницького",
    "просп. Олександра Поля",
    "просп. Слобожанський",
    "просп. Богдана Хмельницького",
    "просп. Героїв",
    "просп. Науки",
    "просп. Мануйлівський",
    "просп. Праці",
    "вул. Робоча",
    "вул. Березинська",
    "вул. Велика Діївська",
    "вул. Космічна",
    "вул. Тополина",
    "вул. Калинова",
    "вул. Набережна Перемоги",
]
# як часто перевіряти (0 — вимкнути). 3 хв: щохвилини ~1 МБ сторінки + запит на вулицю — так сайт швидше блокує
DTEK_PROBE_SEC = int(os.getenv("DTEK_PROBE_SEC") or 180)

# Райони/масиви Дніпра для розпізнавання в чатах (укр + рос + сленг з чатів).
# Ключі — regex, що шукаються з початку слова. Спершу масиви (точніше), потім райони.
DISTRICTS = {
    "Перемога": [r"перемог", r"перемоз", r"победа", r"побед"],
    "Тополя": [r"топол"],
    "Лівобережний": [r"лівобереж", r"левобереж", r"лб-?\d", r"лб(?!\w)"],
    "Сокіл": [r"сокіл", r"сокол"],
    "Фрунзенський": [r"фрунзен", r"фрз(?!\w)"],
    "Березинка": [r"березинк"],
    "Клочко": [r"клочк"],
    "Сонячний": [r"сонячн", r"солнечн"],
    "Парус": [r"парус"],
    "Північний": [r"північн", r"северн"],
    "Червоний Камінь": [r"червон\w* камін", r"красн\w* кам"],
    "Ігрень": [r"ігрен", r"игрен"],
    "Придніпровськ": [r"придніпровськ", r"приднепровск", r"придник"],
    "Самарівка": [r"самарівк", r"самаровк"],
    "Мандриківка": [r"мандриків", r"мандрыков"],
    "Ломівка": [r"ломівк", r"ломовк"],
    "Кайдаки": [r"кайдак"],
    "Діївка": [r"діївк", r"диевк"],
    "Сухачівка": [r"сухачів", r"сухачев"],
    "Шляхівка": [r"шляхівк", r"шляховк"],
    "Краснопілля": [r"краснопіл", r"краснопол"],
    "Центр": [r"центр"],
    "Соборний": [r"соборн", r"нагорк", r"нагірн"],
    "Шевченківський": [r"шевченківськ", r"шевченковск", r"кодак"],
    "Чечелівський": [r"чечелів", r"чечелов"],
    "Новокодацький": [r"новокодац"],
    "АНД": [r"амур", r"анд(?!\w)", r"нижньодніпров", r"нижнеднепров"],
    "Індустріальний": [r"індустріальн", r"индустриальн", r"комунар", r"коммунар"],
    "Самарський": [r"самарськ\w* район", r"самарск\w* район"],
}

# --- Світлобот: датчики світла (кожна точка — чийсь будинок) -> фактичний стан групи ---
SVITLOBOT_ENABLED = os.getenv("SVITLOBOT_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
SVITLOBOT_TOKEN_URL = os.getenv("SVITLOBOT_TOKEN_URL", "https://api.svitlobot.in.ua/website/getMapToken")
SVITLOBOT_CHANNELS_URL = os.getenv("SVITLOBOT_CHANNELS_URL", "https://api.svitlobot.in.ua/website/getChannelsForMap")
SVITLOBOT_POLL_INTERVAL = int(os.getenv("SVITLOBOT_POLL_INTERVAL") or 120)
# регіон у підписі групи точки: «Дніпро (ДТЕК) — група 5.1». «Група 1.1» без регіону — інші області
SVITLOBOT_REGION = os.getenv("SVITLOBOT_REGION", "Дніпро")
# лише точки цього міста (слово в адресі до «->»: «Дніпро -> Калинова 82»)
SVITLOBOT_CITY_FILTER = os.getenv("SVITLOBOT_CITY_FILTER", "Дніпро")
SVITLOBOT_MIN_SENSORS = int(os.getenv("SVITLOBOT_MIN_SENSORS") or 2)
SVITLOBOT_ON_THRESHOLD = float(os.getenv("SVITLOBOT_ON_THRESHOLD") or 0.65)
SVITLOBOT_OFF_THRESHOLD = float(os.getenv("SVITLOBOT_OFF_THRESHOLD") or 0.35)
