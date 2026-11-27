"""Браузерна сесія ДТЕК (nodriver): один браузер за раз, гарантоване закриття, кукі й User-Agent -> curl_cffi."""
import asyncio
import json
import os
import sys
import time
import types
import unittest
from unittest.mock import patch

import dtek_http
from fake_dtek import fake_dtek

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
HEADLESS_UA = UA.replace("Chrome/", "HeadlessChrome/")
PAGE = '<html><head><meta name="csrf-token" content="browser-tok"></head><body>DisconSchedule</body></html>'


class FakeCookie:
    def __init__(self, name, value, domain=".dtek-dnem.com.ua"):
        self.name, self.value, self.domain = name, value, domain


class FakeNodriver(types.ModuleType):
    """Замість nodriver: рахує запуски, одночасні браузери, закриття і тимчасові профілі."""

    def __init__(self, fail_get: Exception | None = None, cookies=None):
        super().__init__("nodriver")
        self.cdp = types.SimpleNamespace(
            browser=types.SimpleNamespace(get_version=lambda: ("get_version",)),
            network=types.SimpleNamespace(set_user_agent_override=lambda **kw: ("override", kw)))
        self.ua = HEADLESS_UA
        self.fail_get = fail_get
        self.starts = self.active = self.max_active = self.stops = 0
        self.profiles: list[str] = []
        self.kwargs: list[dict] = []
        self.cookie_list = cookies if cookies is not None else [
            FakeCookie("visid_incap_123", "v1"), FakeCookie("incap_ses_45_123", "s1"),
            FakeCookie("reese84", "r1"), FakeCookie("XSRF-TOKEN", "x1"), FakeCookie("_ga", "g", ".google.com")]

    async def start(self, **kw):
        self.starts += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.kwargs.append(kw)
        self.profiles.append(kw["user_data_dir"])
        await asyncio.sleep(0.05)  # браузер стартує не миттєво — інші виклики встигають прийти
        return _Browser(self)


class _Tab:
    def __init__(self, nd):
        self.nd = nd

    async def send(self, cmd):
        if cmd[0] == "get_version":
            return ("1.3", "HeadlessChrome/131.0.0.0", "rev", self.nd.ua, "js")
        self.nd.ua = cmd[1]["user_agent"]  # Network.setUserAgentOverride

    async def get(self, url):
        assert url == dtek_http.PAGE_URL
        if self.nd.fail_get:
            raise self.nd.fail_get
        return self

    async def evaluate(self, expr, return_by_value=False):
        assert expr == "navigator.userAgent" and return_by_value
        return self.nd.ua

    async def get_content(self):
        return PAGE


class _Cookies:
    def __init__(self, nd):
        self.nd = nd

    async def get_all(self, requests_cookie_format=False):
        return list(self.nd.cookie_list)


class _Browser:
    def __init__(self, nd):
        self.nd = nd
        self.cookies = _Cookies(nd)

    async def get(self, url):
        assert url == "about:blank"
        return _Tab(self.nd)

    def stop(self):
        self.nd.stops += 1
        self.nd.active -= 1


class BrowserCase(unittest.IsolatedAsyncioTestCase):
    def use(self, nd: FakeNodriver):
        for p in (patch.dict(sys.modules, {"nodriver": nd}), patch.object(dtek_http, "BROWSER_ENABLED", True),
                  patch.object(dtek_http, "CHROME_EXECUTABLE_PATH", "/usr/bin/chromium"),
                  patch.object(dtek_http, "WAIT_SEC", (0.0, 0.0))):
            p.start()
            self.addCleanup(p.stop)
        return nd


class LockTests(BrowserCase):
    async def test_concurrent_refresh_runs_one_browser(self):
        nd = self.use(FakeNodriver())
        results = await asyncio.gather(*(dtek_http.refresh_browser_session() for _ in range(5)))
        self.assertEqual(nd.starts, 1)  # п'ятеро попросили — браузер один
        self.assertEqual(nd.max_active, 1)
        self.assertTrue(all(r is results[0] for r in results))
        self.assertEqual(results[0].user_agent, UA)  # «HeadlessChrome» підмінено на звичайний Chrome
        self.assertEqual(results[0].csrf, "browser-tok")
        self.assertEqual(set(results[0].cookies), {"visid_incap_123", "incap_ses_45_123", "reese84", "XSRF-TOKEN"})
        kw = nd.kwargs[0]
        self.assertTrue(kw["headless"])
        self.assertEqual(kw["browser_executable_path"], "/usr/bin/chromium")
        for arg in ("--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"):
            self.assertIn(arg, kw["browser_args"])
        self.assertEqual(nd.stops, 1)
        self.assertFalse(os.path.exists(nd.profiles[0]))  # тимчасовий профіль прибрано

    async def test_cooldown_between_launches(self):
        nd = self.use(FakeNodriver())
        await dtek_http.refresh_browser_session()
        dtek_http.mark_browser_stale()
        self.assertIsNone(await dtek_http.refresh_browser_session())  # протухли, але ще 5 хв cooldown
        self.assertEqual(nd.starts, 1)
        with patch.object(dtek_http, "_browser_last_try", time.time() - 301):
            self.assertIsNotNone(await dtek_http.refresh_browser_session())
        self.assertEqual(nd.starts, 2)


class CleanupTests(BrowserCase):
    async def test_browser_stopped_when_page_fails(self):
        nd = self.use(FakeNodriver(fail_get=RuntimeError("net::ERR_CONNECTION_RESET")))
        with self.assertLogs("dtek_http", "WARNING"):
            self.assertIsNone(await dtek_http.refresh_browser_session())
        self.assertEqual((nd.starts, nd.stops, nd.active), (1, 1, 0))
        self.assertFalse(os.path.exists(nd.profiles[0]))
        self.assertIsNone(dtek_http.browser_session())

    async def test_browser_stopped_when_no_cookies(self):
        nd = self.use(FakeNodriver(cookies=[]))
        with self.assertLogs("dtek_http", "WARNING"):
            self.assertIsNone(await dtek_http.refresh_browser_session())
        self.assertEqual(nd.stops, 1)

    async def test_browser_stopped_on_timeout(self):
        nd = self.use(FakeNodriver())
        with patch.object(dtek_http, "WAIT_SEC", (5.0, 5.0)), patch.object(dtek_http, "DTEK_BROWSER_TIMEOUT_SEC", 0.2), \
                self.assertLogs("dtek_http", "WARNING"):
            self.assertIsNone(await dtek_http.refresh_browser_session())
        self.assertEqual((nd.stops, nd.active), (1, 0))
        self.assertFalse(os.path.exists(nd.profiles[0]))


class CurlTests(BrowserCase):
    async def test_cookies_and_user_agent_go_to_curl(self):
        """Кукі ще немає -> перед першим запитом браузер; далі curl_cffi з його кукі, User-Agent і csrf."""
        nd = self.use(FakeNodriver())
        with fake_dtek(lambda m, u, f: (200, {"result": True, "data": {"1": {}}})) as s:
            payload = await dtek_http.ajax(dtek_http.ajax_form("getHomeNum", [("street", "вул. Робоча")]), "tok")
        self.assertEqual(payload["data"], {"1": {}})
        self.assertEqual(nd.starts, 1)
        self.assertEqual(s.cookies["reese84"], "r1")
        self.assertEqual(s.cookies["incap_ses_45_123"], "s1")
        self.assertNotIn("_ga", s.cookies)  # чужі домени не беремо
        self.assertEqual(s.headers["User-Agent"], UA)
        _, _, _, headers = s.calls[-1]
        self.assertEqual(headers["User-Agent"], UA)
        self.assertEqual(headers["X-Requested-With"], "XMLHttpRequest")
        cache = json.load(open(dtek_http.DTEK_COOKIE_CACHE, encoding="utf-8"))
        self.assertEqual((cache["user_agent"], cache["cookies"]["reese84"]), (UA, "r1"))

    async def test_stub_instead_of_json_refreshes_and_retries(self):
        nd = self.use(FakeNodriver())
        # кукі з кешу свіжі, але сайт їх уже не приймає
        with open(dtek_http.DTEK_COOKIE_CACHE, "w", encoding="utf-8") as f:
            json.dump({"cookies": {"reese84": "old"}, "user_agent": "OldUA", "ts": time.time(), "csrf": "old"}, f)
        answers = iter([(200, '<html><iframe src="/_Incapsula_Resource?x"></iframe></html>'),
                        (200, {"result": True, "data": {"7": {}}})])
        with fake_dtek(lambda m, u, f: next(answers)) as s:
            payload = await dtek_http.ajax({"method": "getHomeNum"}, "page-tok")
        self.assertEqual(payload["data"], {"7": {}})
        self.assertEqual(nd.starts, 1)  # заглушка -> рівно один браузер
        (_, _, _, first), (_, _, _, second) = s.calls
        self.assertEqual(first["User-Agent"], "OldUA")
        self.assertEqual(first["X-CSRF-Token"], "page-tok")
        self.assertEqual(second["User-Agent"], UA)  # повтор — з новими кукі, UA і csrf зі сторінки браузера
        self.assertEqual(second["X-CSRF-Token"], "browser-tok")
        self.assertEqual(s.cookies["reese84"], "r1")
        self.assertEqual(dtek_http.backoff_left(), 0)

    async def test_still_stub_after_refresh_is_blocked(self):
        nd = self.use(FakeNodriver())
        with fake_dtek(lambda m, u, f: (200, "<html>not json</html>")) as s:
            with self.assertRaises(dtek_http.Blocked):
                await dtek_http.ajax({"method": "getHomeNum"}, "tok")
            self.assertGreater(dtek_http.backoff_left(), 1700)
        self.assertEqual(nd.starts, 1)  # браузер раз на старті; повторний — cooldown 5 хв
        self.assertEqual(len(s.calls), 1)

    async def test_fresh_cache_skips_browser_stale_cache_refreshes(self):
        nd = self.use(FakeNodriver())
        with open(dtek_http.DTEK_COOKIE_CACHE, "w", encoding="utf-8") as f:
            json.dump({"cookies": {"reese84": "cached"}, "user_agent": UA, "ts": time.time() - 60}, f)
        with fake_dtek(lambda m, u, f: (200, "<html>" + "x" * 30000 + "</html>")) as s:
            await dtek_http.request("GET", dtek_http.PAGE_URL)
        self.assertEqual(nd.starts, 0)  # кукі молодші за DTEK_COOKIE_TTL_SEC — браузер не потрібен
        self.assertEqual(s.cookies["reese84"], "cached")
        with patch.object(dtek_http, "_browser", None), patch.object(dtek_http, "_browser_loaded", False):
            with open(dtek_http.DTEK_COOKIE_CACHE, "w", encoding="utf-8") as f:
                json.dump({"cookies": {"reese84": "cached"}, "user_agent": UA, "ts": time.time() - 1300}, f)
            with fake_dtek(lambda m, u, f: (200, "<html>" + "x" * 30000 + "</html>")) as s:
                await dtek_http.request("GET", dtek_http.PAGE_URL)
            self.assertEqual(nd.starts, 1)  # старші за 20 хв — новий браузер
            self.assertEqual(s.cookies["reese84"], "r1")


if __name__ == "__main__":
    unittest.main()
