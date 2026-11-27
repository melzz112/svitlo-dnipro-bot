"""Фейкова сесія curl_cffi для тестів сайту ДТЕК: dtek_http.new_session -> FakeSession(respond).

respond(method, url, form) -> (status, текст) або (status, dict) — dict віддаємо як JSON."""
import json
from contextlib import contextmanager
from unittest.mock import patch

import dtek_http


class _Resp:
    def __init__(self, status: int, body):
        self.status_code = status
        self.text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        self.headers = {}


class _Jar(dict):
    """Як банка кукі curl_cffi: set(name, value, domain=...)."""

    def set(self, name, value, domain=None, **kw):
        self[name] = value


class FakeSession:
    def __init__(self, respond):
        self.respond = respond
        self.calls: list[tuple[str, str, dict, dict]] = []
        self.cookies = _Jar()
        self.headers: dict[str, str] = {}

    async def request(self, method, url, data=None, headers=None, **kw):
        form = dict(data or {})
        self.calls.append((method, url, form, dict(headers or {})))
        status, body = self.respond(method, url, form)
        return _Resp(status, body)

    async def close(self):
        pass


@contextmanager
def fake_dtek(respond):
    """Підміняє мережу сайту ДТЕК; паузу (backoff) і сесію скидаємо до і після."""
    session = FakeSession(respond)
    dtek_http.clear_backoff()
    dtek_http._session = None
    with patch.object(dtek_http, "new_session", lambda: session):
        try:
            yield session
        finally:
            dtek_http._session = None
            dtek_http.clear_backoff()
