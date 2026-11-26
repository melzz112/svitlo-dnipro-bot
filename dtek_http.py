"""Спільне для запитів до сайту ДТЕК (графік і «Перевірити адресу").

Раніше кожен запит ішов з порожніми кукі і заголовком python-httpx: сайт щоразу відповідав 302 (перевірка
Incapsula) і інколи віддавав сторінку без даних. Тепер — заголовки звичайного браузера, кукі живуть між
запитами, на таймаут/5xx — ще одна спроба з паузою; якщо сайт віддав «порожню» сторінку — кукі скидаємо.

Сам запит іде через curl_cffi з відбитком TLS/HTTP2 справжнього Chrome (impersonate): Incapsula дивиться
не лише на заголовки, а й на те, як клієнт «рукостискається», і python-ssl від httpx видає бота одразу.
Зверху лишається httpx (кукі, редиректи, raise_for_status, винятки httpx.*) — решта коду не змінилась.
Немає curl_cffi — працює звичайний httpx.

DTEK_PROXY у .env — усі запити до ДТЕК ідуть через проксі (з VPS Incapsula блокує сам IP).
"""
import asyncio
import logging
import random

import httpx

from config import DTEK_PROXY

try:
    from curl_cffi import requests as curl_requests
    from curl_cffi.requests import exceptions as curl_exc
except ImportError:  # pragma: no cover — без curl_cffi лишається звичайний httpx
    curl_requests = None

log = logging.getLogger(__name__)

IMPERSONATE = "chrome"  # останній Chrome, який знає встановлений curl_cffi
HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8,en-US;q=0.7,en;q=0.6",
}
# Без curl_cffi сами вдаємо браузер заголовком; з ним User-Agent ставить impersonate — у парі з відбитком TLS.
FALLBACK_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/131.0.0.0 Safari/537.36")
TIMEOUT = 15

# Ці заголовки curl ставить сам, як справжній Chrome (і сам розпаковує gzip/br), — від httpx їх не передаємо.
_DROP_REQ = {"host", "connection", "accept-encoding", "user-agent", "content-length", "transfer-encoding"}
# Тіло вже розпаковане curl'ом — щоб httpx не розпаковував удруге і не звіряв довжину.
_DROP_RESP = {"content-encoding", "content-length", "transfer-encoding"}

_cookies = httpx.Cookies()


class CurlTransport(httpx.AsyncBaseTransport):
    """httpx-транспорт поверх curl_cffi: кукі й редиректи — на боці httpx, curl лише ходить у мережу."""

    def __init__(self, impersonate: str = IMPERSONATE, proxy: str | None = None):
        self._s = curl_requests.AsyncSession(impersonate=impersonate, proxy=proxy)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        headers = [(k, v) for k, v in request.headers.multi_items() if k.lower() not in _DROP_REQ]
        t = request.extensions.get("timeout") or {}
        timeout = (t.get("connect") or TIMEOUT, t.get("read") or TIMEOUT)
        body = await request.aread()
        self._s.cookies.clear()  # кукі веде httpx (спільна банка _cookies) — curl свої не підмішує
        try:
            r = await self._s.request(
                request.method, str(request.url), headers=headers, data=body or None,
                timeout=timeout, allow_redirects=False,
            )
        except curl_exc.Timeout as e:
            raise httpx.ReadTimeout(str(e), request=request) from e
        except curl_exc.RequestException as e:
            raise httpx.ConnectError(str(e), request=request) from e
        out = [(k, v) for k, v in r.headers.multi_items() if k.lower() not in _DROP_RESP]
        return httpx.Response(r.status_code, headers=out, content=r.content, request=request)

    async def aclose(self):
        await self._s.close()


def client(timeout: float = TIMEOUT) -> httpx.AsyncClient:
    kw = {"timeout": timeout, "follow_redirects": True, "headers": dict(HEADERS), "cookies": _cookies}
    if curl_requests:
        kw["transport"] = CurlTransport(proxy=DTEK_PROXY)
    else:
        kw["headers"]["User-Agent"] = FALLBACK_UA
        kw["proxy"] = DTEK_PROXY
    return httpx.AsyncClient(**kw)


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
