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
CROWD_MIN = int(os.getenv("CROWD_MIN") or 3)
QUIET_FROM = int(os.getenv("QUIET_FROM") or 23)  # тихі години (київський час), якщо людина їх увімкнула
QUIET_TO = int(os.getenv("QUIET_TO") or 7)
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR") or 7)  # ранкове зведення  # скільки різних людей за 15 хв = «люди пишуть, що дали»

from groups import DEFAULT as GROUPS  # noqa: E402 — типові 1.1 … 6.2; реальні черги (до 18.1+) — groups.known()

# Сайт ДТЕК, «Перевірити адресу». Повний довідник вулиць і будинків збирає scripts/seed_streets.py (з ПК з
# українським IP) у таблиці dtek_streets / dtek_houses; на сервері сайт смикаємо бережно:
# - лише вулиці, де є підписники (адреси людей, будинки без черги, адреси «⏳ чекає на сайт»);
# - раз на DTEK_POLL_INTERVAL_SEC (10 хв; під час аварій — DTEK_POLL_FAST_SEC, 5 хв), з паузою DTEK_POLL_PAUSE_MIN…MAX с між запитами;
# - помилка чи антибот -> DTEK_BACKOFF_SEC тиші (жодного запиту до сайту).
DTEK_STREETS = _list("DTEK_STREETS")  # додаткові контрольні вулиці (за замовчуванням — жодної)
DTEK_POLL_INTERVAL_SEC = int(os.getenv("DTEK_POLL_INTERVAL_SEC") or 600)  # спокійно — раз на 10 хв
# екстрені в місті, у когось із підписників зараз нема світла чи аварія по його черзі — раз на 5 хв
DTEK_POLL_FAST_SEC = int(os.getenv("DTEK_POLL_FAST_SEC") or 300)
if os.getenv("DTEK_PROBE_SEC", "").strip() == "0":  # стара змінна: лише «0 — вимкнути»; 900 з неї вже не діє
    DTEK_POLL_INTERVAL_SEC = 0
DTEK_POLL_PAUSE_MIN = float(os.getenv("DTEK_POLL_PAUSE_MIN") or 3)
DTEK_POLL_PAUSE_MAX = float(os.getenv("DTEK_POLL_PAUSE_MAX") or 5)
DTEK_BACKOFF_SEC = int(os.getenv("DTEK_BACKOFF_SEC") or 1800)

# Браузерна сесія для сайту ДТЕК: AJAX (getStreets / getHomeNum) хоче кукі після JS-перевірки Incapsula.
# Chromium (nodriver, headless) запускається на кілька секунд лише по кукі й User-Agent і одразу закривається.
DTEK_BROWSER_SESSION_ENABLED = os.getenv("DTEK_BROWSER_SESSION_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
DTEK_COOKIE_TTL_SEC = int(os.getenv("DTEK_COOKIE_TTL_SEC") or 1200)  # кукі старші за 20 хв — оновлюємо
DTEK_BROWSER_COOLDOWN_SEC = int(os.getenv("DTEK_BROWSER_COOLDOWN_SEC") or 300)  # браузер — не частіше раза на 5 хв
DTEK_BROWSER_TIMEOUT_SEC = int(os.getenv("DTEK_BROWSER_TIMEOUT_SEC") or 60)  # жорсткий ліміт на весь запуск


def _find_chrome() -> str | None:
    """Docker: /usr/bin/chromium; локально — Chromium/Chrome з PATH або стандартних місць Windows/Linux/macOS."""
    import shutil

    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome"):
        if path := shutil.which(name):
            return path
    candidates = [
        "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome", "/snap/bin/chromium",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ] + [os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
         for base in filter(None, (os.getenv("PROGRAMFILES"), os.getenv("PROGRAMFILES(X86)"), os.getenv("LOCALAPPDATA")))]
    return next((p for p in candidates if os.path.isfile(p)), None)


CHROME_EXECUTABLE_PATH = os.getenv("CHROME_EXECUTABLE_PATH", "").strip() or _find_chrome()
# кукі й User-Agent з браузера переживають перезапуск бота (поруч із БД)
DTEK_COOKIE_CACHE = os.getenv("DTEK_COOKIE_CACHE", "").strip() or os.path.join(
    os.path.dirname(os.path.abspath(DB_PATH)), "dtek_cookies.json")

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
