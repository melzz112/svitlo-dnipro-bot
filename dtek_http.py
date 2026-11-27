"""Запити до сайту ДТЕК (сторінка «Відключення» і «Перевірити адресу»).

Клієнт — curl_cffi.requests.AsyncSession(impersonate="chrome"): відбиток TLS/HTTP2 справжнього Chrome, тож
Incapsula не відрізняє бота від браузера за «рукостисканням». Одна сесія на процес: кукі (Incapsula, csrf)
живуть між запитами, заголовки — як у браузера, AJAX-запити — з X-Requested-With / Origin / Referer.

Бережний режим (сервер): будь-яка помилка чи антибот -> пауза DTEK_BACKOFF_SEC (30 хв), за цей час жоден
запит до сайту не йде (Backoff піднімається одразу, без мережі). Сідер довідника (scripts/seed_streets.py)
ходить з use_backoff=False і сам вирішує, скільки чекати.
"""
import asyncio
import json as _json
import logging
import random
import time
from dataclasses import dataclass, field

from curl_cffi import CurlError
from curl_cffi.requests import AsyncSession

from config import DTEK_BACKOFF_SEC, DTEK_PROXY

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
    return AsyncSession(impersonate=IMPERSONATE, proxy=DTEK_PROXY, headers=dict(HEADERS), timeout=TIMEOUT)


_session = None
_backoff_until = 0.0
_backoff_reason = ""


def session():
    global _session
    if _session is None:
        _session = new_session()
    return _session


async def reset():
    """Антибот — наступного разу починаємо з чистими кукі (нова сесія)."""
    global _session
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
                  use_backoff: bool = True) -> Response:
    """Запит через спільну сесію. Помилка / антибот -> DtekError (і пауза, якщо use_backoff)."""
    if use_backoff and backoff_left():
        raise Backoff(f"пауза ще {int(backoff_left())} с ({_backoff_reason})")

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
            fail(Blocked(f"антибот / HTTP {status}"))
        if status >= 400:
            fail(DtekError(f"HTTP {status}"))
        return Response(status, text, dict(r.headers or {}))
    raise AssertionError("unreachable")


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
    headers = dict(AJAX_HEADERS)
    if csrf:
        headers["X-CSRF-Token"] = csrf
    r = await request("POST", AJAX_URL, data=form, headers=headers, use_backoff=use_backoff)
    try:
        payload = r.json()
    except ValueError:
        await reset()
        if use_backoff:
            backoff("AJAX без JSON — схоже, антибот")
        raise Blocked("AJAX без JSON — схоже, антибот") from None
    if not isinstance(payload, dict):
        raise DtekError("AJAX: неочікувана відповідь")
    return payload
