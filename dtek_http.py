"""Запити до сайту ДТЕК (сторінка «Відключення» і «Перевірити адресу»).

Клієнт — curl_cffi.requests.AsyncSession(impersonate="chrome"): відбиток TLS/HTTP2 справжнього Chrome, тож
Incapsula не відрізняє бота від браузера за «рукостисканням». Напряму, без проксі. Одна сесія на процес: кукі (Incapsula, csrf)
живуть між запитами, заголовки — як у браузера, AJAX-запити — з X-Requested-With / Origin / Referer.

Бережний режим (сервер): будь-яка помилка чи антибот -> пауза DTEK_BACKOFF_SEC (30 хв), за цей час жоден
запит до сайту не йде (Backoff піднімається одразу, без мережі). Сідер довідника (scripts/seed_streets.py)
ходить з use_backoff=False і сам вирішує, скільки чекати.

Браузерна сесія (DTEK_BROWSER_SESSION_ENABLED): AJAX-ендпоінти хочуть кукі, які Incapsula видає лише після
своєї JS-перевірки (visid_incap_*, incap_ses_*, reese84). refresh_browser_session() на кілька секунд піднімає
headless Chromium (nodriver), відкриває сторінку відключень, бере всі кукі домену, User-Agent і csrf, і в finally
гарантовано закриває браузер і прибирає тимчасовий профіль. Одночасно — не більше одного браузера (asyncio.Lock),
не частіше раза на DTEK_BROWSER_COOLDOWN_SEC. Кукі живуть DTEK_COOKIE_TTL_SEC, кеш — у файлі DTEK_COOKIE_CACHE.
Оновлення: перед запитом, якщо кукі протухли; і коли AJAX/сторінка повернули заглушку замість даних —
кукі позначаються протухлими, браузер оновлює їх, запит повторюється один раз.
"""
import asyncio
import inspect
import json as _json
import logging
import os
import random
import re
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field

from curl_cffi import CurlError
from curl_cffi.requests import AsyncSession

from config import (CHROME_EXECUTABLE_PATH, DTEK_BACKOFF_SEC, DTEK_BROWSER_COOLDOWN_SEC, DTEK_BROWSER_SESSION_ENABLED,
                    DTEK_BROWSER_TIMEOUT_SEC, DTEK_COOKIE_CACHE, DTEK_COOKIE_TTL_SEC)

log = logging.getLogger(__name__)

ORIGIN = "https://www.dtek-dnem.com.ua"
PAGE_URL = f"{ORIGIN}/ua/shutdowns"
AJAX_URL = f"{ORIGIN}/ua/ajax"

IMPERSONATE = "chrome"  # останній Chrome, який знає встановлений curl_cffi
TIMEOUT = 20
HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9",
}
AJAX_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "uk-UA,uk;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": ORIGIN,
    "Referer": PAGE_URL,
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}


class DtekError(Exception):
    """Сайт ДТЕК не відповів або відповів не тим."""


class Blocked(DtekError):
    """Антибот (Incapsula), 403 чи 429."""


class Backoff(DtekError):
    """Після помилки сайт не смикаємо DTEK_BACKOFF_SEC — запит навіть не надсилали."""


ERRORS = (DtekError, ValueError)  # що ловити навколо запитів до ДТЕК


@dataclass
class Response:
    status_code: int
    text: str
    headers: dict = field(default_factory=dict)

    def json(self):
        try:
            return _json.loads(self.text)
        except ValueError as e:
            raise ValueError(f"ДТЕК: не JSON ({self.text[:60]!r})") from e


def new_session() -> AsyncSession:
    """Окрема функція — щоб тести підставляли фейкову сесію."""
    return AsyncSession(impersonate=IMPERSONATE, headers=dict(HEADERS), timeout=TIMEOUT)


_session = None
_backoff_until = 0.0
_backoff_reason = ""


def session():
    global _session
    if _session is None:
        _session = new_session()
        _apply_browser(_session)  # нова сесія curl одразу з кукі й User-Agent браузера
    return _session


# ---------- браузерна сесія (nodriver): кукі Incapsula і User-Agent ----------

BROWSER_ENABLED = DTEK_BROWSER_SESSION_ENABLED
BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--lang=uk-UA",
                "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--mute-audio"]
WAIT_SEC = (5.0, 8.0)  # скільки чекати фонову JS-ініціалізацію сторінки


@dataclass
class BrowserSession:
    cookies: dict[str, str]
    user_agent: str
    ts: float
    csrf: str | None = None
    domains: dict[str, str] = field(default_factory=dict)  # кука -> домен (для curl)
    update: str | None = None  # DisconSchedule.fact.update зі сторінки (поле updateFact у запитах)
    stale: bool = False

    def fresh(self) -> bool:
        return not self.stale and time.time() - self.ts < DTEK_COOKIE_TTL_SEC


_browser: BrowserSession | None = None
_browser_loaded = False  # кеш з файлу вже підтягнули
_browser_last_try = 0.0
_browser_locks: dict = {}


def _lock() -> asyncio.Lock:
    """Lock на поточний event loop (у тестах і в сідері loop буває не один)."""
    loop = asyncio.get_running_loop()
    return _browser_locks.setdefault(id(loop), asyncio.Lock())


def browser_session() -> BrowserSession | None:
    _load_cache()
    return _browser


def _load_cache():
    global _browser, _browser_loaded
    if _browser_loaded:
        return
    _browser_loaded = True
    try:
        with open(DTEK_COOKIE_CACHE, encoding="utf-8") as f:
            data = _json.load(f)
        _browser = BrowserSession(**{k: data[k] for k in ("cookies", "user_agent", "ts", "csrf", "domains", "update") if k in data})
    except (OSError, ValueError, TypeError, KeyError):
        _browser = None


def _save_cache(bs: BrowserSession):
    try:
        os.makedirs(os.path.dirname(DTEK_COOKIE_CACHE) or ".", exist_ok=True)
        tmp = DTEK_COOKIE_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump({k: v for k, v in asdict(bs).items() if k != "stale"}, f, ensure_ascii=False)
        os.replace(tmp, DTEK_COOKIE_CACHE)
    except OSError as e:  # кеш — не привід зламати запити
        log.warning("ДТЕК: кеш кукі не збережено: %r", e)


def _apply_browser(s):
    """Кукі й User-Agent браузера -> сесія curl_cffi."""
    bs = browser_session()
    if not bs or bs.stale:
        return
    for name, value in bs.cookies.items():
        try:
            s.cookies.set(name, value, domain=bs.domains.get(name) or ".dtek-dnem.com.ua")
        except Exception:  # pragma: no cover — формат банки кукі різних версій curl_cffi
            s.cookies[name] = value
    if bs.user_agent:
        s.headers["User-Agent"] = bs.user_agent


def mark_browser_stale():
    if _browser:
        _browser.stale = True


def _csrf(html: str) -> str | None:
    m = re.search(r'<meta[^>]+name="csrf-token"[^>]+content="([^"]+)"', html or "")
    return m.group(1) if m else None


async def _launch_browser() -> BrowserSession:
    """Headless Chromium на кілька секунд: сторінка відключень -> кукі домену, User-Agent, csrf.
    Браузер і тимчасовий профіль прибираємо у finally — навіть якщо сторінка не завантажилась."""
    import nodriver as uc

    if not CHROME_EXECUTABLE_PATH:
        raise DtekError("браузер не знайдено (CHROME_EXECUTABLE_PATH)")
    profile = tempfile.mkdtemp(prefix="dtek-chrome-")
    browser = None
    try:
        browser = await uc.start(headless=True, browser_executable_path=CHROME_EXECUTABLE_PATH,
                                 browser_args=list(BROWSER_ARGS), user_data_dir=profile, sandbox=False, lang="uk-UA")
        tab = await browser.get("about:blank")
        await _plain_user_agent(tab)
        tab = await tab.get(PAGE_URL)
        await asyncio.sleep(random.uniform(*WAIT_SEC))  # JS Incapsula ставить reese84 / incap_ses_*
        user_agent = await tab.evaluate("navigator.userAgent", return_by_value=True)
        html = await tab.get_content()
        cookies, domains = {}, {}
        for c in await browser.cookies.get_all():
            if "dtek" in (c.domain or ""):
                cookies[c.name] = c.value
                domains[c.name] = c.domain
        if not cookies:
            raise DtekError("браузер не отримав кукі сайту ДТЕК")
        upd = re.search(r'DisconSchedule\.fact\s*=\s*\{.*?"update"\s*:\s*"([^"]+)"', html or "", re.S)
        return BrowserSession(cookies, str(user_agent or ""), time.time(), _csrf(html), domains, upd and upd.group(1))
    finally:
        if browser is not None:
            try:
                res = browser.stop()
                if inspect.isawaitable(res):
                    await res
            except Exception as e:  # pragma: no cover
                log.warning("ДТЕК: браузер не закрився чисто: %r", e)
        shutil.rmtree(profile, ignore_errors=True)


async def _plain_user_agent(tab):
    """Headless Chrome представляється «HeadlessChrome» — найявніша ознака бота. До відкриття сторінки
    підміняємо на звичайний «Chrome» тієї ж версії: і перевірка Incapsula, і curl далі — з тим самим UA."""
    try:
        from nodriver import cdp

        real = (await tab.send(cdp.browser.get_version()))[3]
        if "HeadlessChrome" in real:
            await tab.send(cdp.network.set_user_agent_override(
                user_agent=real.replace("HeadlessChrome", "Chrome"), accept_language="uk-UA,uk;q=0.9"))
    except Exception as e:  # не вийшло — працюємо з тим, що є
        log.warning("ДТЕК: User-Agent браузера не підмінено: %r", e)


async def refresh_browser_session(force: bool = False) -> BrowserSession | None:
    """Нові кукі з браузера. Одночасно — лише один браузер: решта чекає на Lock і отримує його результат.
    Не частіше DTEK_BROWSER_COOLDOWN_SEC (force — лише для ручного запуску). Не вдалось — None."""
    global _browser, _browser_last_try
    if not BROWSER_ENABLED:
        return None
    asked = time.time()
    async with _lock():
        bs = browser_session()
        if bs and bs.ts >= asked and not bs.stale:
            return bs  # поки чекали, інший виклик уже оновив
        if not force and time.time() - _browser_last_try < DTEK_BROWSER_COOLDOWN_SEC:
            return bs if bs and not bs.stale else None
        _browser_last_try = time.time()
        started = time.monotonic()
        try:
            async with asyncio.timeout(DTEK_BROWSER_TIMEOUT_SEC):
                new = await _launch_browser()
        except Exception as e:
            log.warning("ДТЕК: браузерна сесія не вдалась: %r", e)
            return None
        _browser = new
        _save_cache(new)
        await reset(keep_page=True)  # наступний запит — нова сесія curl з цими кукі
        if new.csrf:  # токен зі сторінки браузера — під його кукі; сторінку curl'ом не тягнемо
            remember_page(new.csrf, new.update)
        log.info("ДТЕК: браузерна сесія оновлена за %.1f с (%d кукі: %s)", time.monotonic() - started,
                 len(new.cookies), ", ".join(sorted(new.cookies))[:200])
        return new


# ---------- сторінка відключень: csrf під поточні кукі (сторінку ~800 КБ тягнемо не щоциклу) ----------

_page: dict | None = None  # {"ts", "csrf", "update"} — з останньої сторінки curl або браузера


def remember_page(csrf: str, update: str | None = None):
    global _page
    _page = {"ts": time.time(), "csrf": csrf, "update": update if update is not None else (_page or {}).get("update")}


def cached_page() -> dict | None:
    """Свіжий (< DTEK_COOKIE_TTL_SEC) токен під поточні кукі — AJAX можна слати без завантаження сторінки."""
    if _page and _page.get("csrf") and time.time() - _page["ts"] < DTEK_COOKIE_TTL_SEC:
        return _page
    return None


def current_csrf(default: str | None) -> str | None:
    """Кукі могли оновитись посеред циклу (браузер) — тоді й токен беремо новий."""
    return (cached_page() or {}).get("csrf") or default


async def ensure_browser_session():
    """Перед запитом: кукі немає чи вони старші за DTEK_COOKIE_TTL_SEC — оновлюємо (з урахуванням cooldown)."""
    if not BROWSER_ENABLED:
        return
    bs = browser_session()
    if bs is None or not bs.fresh():
        await refresh_browser_session()


async def reset(keep_page: bool = False):
    """Антибот — наступного разу починаємо з чистими кукі (нова сесія). Кукі нові — і csrf під них теж
    потрібен новий (keep_page — лише коли щойно взяли і кукі, і токен з браузера)."""
    global _session, _page
    if not keep_page:
        _page = None
    s, _session = _session, None
    if s is not None:
        try:
            await s.close()
        except Exception:  # pragma: no cover — закриття не важливе
            pass


def backoff_left() -> float:
    return max(0.0, _backoff_until - time.time())


def backoff(reason: str, sec: float | None = None):
    global _backoff_until, _backoff_reason
    sec = DTEK_BACKOFF_SEC if sec is None else sec
    _backoff_until = max(_backoff_until, time.time() + sec)
    _backoff_reason = reason[:120]
    log.warning("ДТЕК: пауза %d с — %s", sec, _backoff_reason)


def clear_backoff():
    global _backoff_until, _backoff_reason
    _backoff_until, _backoff_reason = 0.0, ""


def is_antibot(text: str) -> bool:
    """Заглушка Incapsula — коротка сторінка з її iframe/скриптом. Справжня сторінка (~800 КБ) теж тягне
    скрипт /_Incapsula_Resource, тож сам маркер ще не антибот."""
    t = text or ""
    if "Incapsula incident" in t or "Request unsuccessful. Incapsula" in t:
        return True
    return "_Incapsula_Resource" in t and len(t) < 20_000


async def request(method: str, url: str, *, data=None, headers=None, tries: int = 2,
                  use_backoff: bool = True, _retry: bool = True) -> Response:
    """Запит через спільну сесію. Заглушка антибота — оновлюємо кукі браузером і пробуємо ще раз;
    далі помилка / антибот -> DtekError (і пауза, якщо use_backoff)."""
    if use_backoff and backoff_left():
        raise Backoff(f"пауза ще {int(backoff_left())} с ({_backoff_reason})")
    await ensure_browser_session()
    headers = _with_ua(headers)

    def fail(err: DtekError):
        if use_backoff:
            backoff(str(err))
        raise err

    for attempt in range(tries):
        try:
            r = await session().request(method, url, data=data, headers=headers, allow_redirects=True)
        except (CurlError, asyncio.TimeoutError, OSError) as e:
            if attempt < tries - 1:
                await asyncio.sleep(2 + random.random() * 2)
                continue
            fail(DtekError(f"мережа: {e!r}"))
        status, text = r.status_code, r.text
        if status >= 500 and attempt < tries - 1:
            await asyncio.sleep(2 + random.random() * 2)
            continue
        if status in (403, 429) or is_antibot(text):
            await reset()
            if _retry and await _refresh_after_block():
                bs = browser_session()
                if headers and "X-CSRF-Token" in headers and bs and bs.csrf:  # csrf прив'язаний до кукі сесії
                    headers = {**headers, "X-CSRF-Token": bs.csrf}
                return await request(method, url, data=data, headers=headers, tries=tries,
                                     use_backoff=use_backoff, _retry=False)
            fail(Blocked(f"антибот / HTTP {status}"))
        if status >= 400:
            fail(DtekError(f"HTTP {status}"))
        return Response(status, text, dict(r.headers or {}))
    raise AssertionError("unreachable")


def _with_ua(headers: dict | None) -> dict | None:
    """User-Agent браузера — і в кожен запит явно (curl з impersonate інакше ставить свій)."""
    bs = browser_session()
    if not bs or bs.stale or not bs.user_agent:
        return headers
    return {**(headers or {}), "User-Agent": bs.user_agent}


async def _refresh_after_block() -> bool:
    """Замість даних прийшла заглушка: кукі протухли -> браузер (не частіше раза на 5 хв)."""
    if not BROWSER_ENABLED:
        return False
    mark_browser_stale()
    return await refresh_browser_session() is not None


async def page() -> Response:
    return await request("GET", PAGE_URL)


def ajax_form(method: str, fields: list[tuple[str, str]]) -> dict[str, str]:
    """Як jQuery serializeArray: method=getHomeNum&data[0][name]=city&data[0][value]=м. Дніпро…"""
    data = {"method": method}
    for i, (name, value) in enumerate(fields):
        data[f"data[{i}][name]"] = name
        data[f"data[{i}][value]"] = value
    return data


async def ajax(form: dict, csrf: str | None, *, use_backoff: bool = True) -> dict:
    """POST /ua/ajax -> JSON. Замість JSON прийшла сторінка (антибот) -> Blocked."""
    if use_backoff and backoff_left():
        raise Backoff(f"пауза ще {int(backoff_left())} с ({_backoff_reason})")
    await ensure_browser_session()  # кукі оновлюємо до того, як обрати csrf під них
    headers = dict(AJAX_HEADERS)
    if token := current_csrf(csrf):
        headers["X-CSRF-Token"] = token
    r = await request("POST", AJAX_URL, data=form, headers=headers, use_backoff=use_backoff)
    try:
        payload = r.json()
    except ValueError:
        await reset()
        payload = None
        if await _refresh_after_block():  # нові кукі -> той самий запит ще раз, з csrf зі сторінки браузера
            bs = browser_session()
            if token := current_csrf(bs and bs.csrf):
                headers["X-CSRF-Token"] = token
            r = await request("POST", AJAX_URL, data=form, headers=headers, use_backoff=use_backoff, _retry=False)
            try:
                payload = r.json()
            except ValueError:
                await reset()
        if payload is None:
            if use_backoff:
                backoff("AJAX без JSON — схоже, антибот")
            raise Blocked("AJAX без JSON — схоже, антибот") from None
    if not isinstance(payload, dict):
        raise DtekError("AJAX: неочікувана відповідь")
    return payload
