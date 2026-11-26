"""Спільне для запитів до сайту ДТЕК (графік і «Перевірити адресу").

Раніше кожен запит ішов з порожніми кукі і заголовком python-httpx: сайт щоразу відповідав 302 (перевірка
Incapsula) і інколи віддавав сторінку без даних. Тепер — заголовки звичайного браузера, кукі живуть між
запитами, на таймаут/5xx — ще одна спроба з паузою; якщо сайт віддав «порожню» сторінку — кукі скидаємо.
"""
import asyncio
import random

import httpx

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8,en-US;q=0.7,en;q=0.6",
}
TIMEOUT = 15

_cookies = httpx.Cookies()


def client(timeout: float = TIMEOUT) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=HEADERS, cookies=_cookies)


def keep(c: httpx.AsyncClient):
    """Кукі сесії — у спільну банку, щоб наступний запит не проходив перевірку заново."""
    for cookie in c.cookies.jar:
        _cookies.jar.set_cookie(cookie)


def reset():
    """Сайт віддав сторінку без даних (антибот) — наступного разу починаємо з чистими кукі."""
    _cookies.clear()


def _retryable(e: Exception) -> bool:
    if isinstance(e, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    return isinstance(e, httpx.HTTPStatusError) and e.response.status_code >= 500


async def request(c: httpx.AsyncClient, method: str, url: str, tries: int = 2, **kw) -> httpx.Response:
    for attempt in range(tries):
        try:
            r = await c.request(method, url, **kw)
            r.raise_for_status()
            return r
        except httpx.HTTPError as e:
            if attempt == tries - 1 or not _retryable(e):
                raise
            await asyncio.sleep(2 + random.random() * 2)
    raise AssertionError("unreachable")
