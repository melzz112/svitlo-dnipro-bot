"""Екстрені відключення: текст ДТЕК про місто, перевірка адрес на сайті ДТЕК по групах, пуші, екран «Зараз».
Запуск: python -m unittest tests.test_emergency"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from datetime import datetime, timedelta

from config import TZ

import db
import dtek_site
import handlers
import keyboards as kb
import notifier
import providers
import status
from parse import emergency_in_city
from providers import Emergency, Slot

# Справжні тексти вікна «Шановні клієнти!» (outage-data-ua, 19–20.11.2026)
DNIPRO_NOW = (
    "За наказом НЕК Укренерго в м. Дніпро, Кам'янське, Самар, Павлоград, Самарівський, Дніпровський, "
    "Павлоградський, Солонянський, Царичанський р-ни діють екстрені відключення.\n"
    "Для решти області стабілізаційні відключення."
)
KYIV_REGION = (
    "За наказом НЕК Укренерго 20.11 з 06:00 до 23:59 діють стабілізаційні відключення електроенергії.\n"
    "Через аварію в мережі знеструмлено частково Броварський район. Тут графіки не діють.\n"
    "Будь ласка, розумно споживайте електроенергію — це допоможе зменшити навантаження на мережу та уникнути "
    "екстрених відключень."
)
KYIV = (
    "Через масовану атаку без світла залишилася частина мешканців Печерського, Солом’янського, Голосіївського, "
    "Дніпровського та Дарницького районів.\nВ інших районах столиці діють екстрені відключення через пошкодження "
    "обладнання та перевантаження мереж."
)

# Дати відносні: аварія почалась сьогодні і триває до завтра, тож тести не псуються, коли минає доба
_NOW = datetime.now(TZ)
D = f"{_NOW:%d.%m.%Y}"
T = f"{_NOW + timedelta(days=1):%d.%m.%Y}"
TS = T[:5]

EMERGENCY = "Екстренні відключення (Аварійне без застосування графіку погодинних відключень)"


def house(sub="", start="", end="", grp="1.2"):
    return {"sub_type": sub, "start_date": start, "end_date": end, "type": "2" if sub else "",
            "sub_type_reason": [f"GPV{grp}"], "voluntarily": None}


def street_payload(off_12=3, end=f"17:16 {T}"):
    """4 будинки групи 1.2 (off_12 з них — екстрене) і 2 будинки 3.1 зі світлом."""
    data = {str(n): house(EMERGENCY, f"13:16 {D}", end) if n <= off_12 else house() for n in range(1, 5)}
    data |= {"10": house(grp="3.1"), "12": house(grp="3.1")}
    return {"result": True, "data": data, "updateTimestamp": "13:48 20.11.2026"}


class CityNoticeTests(unittest.TestCase):
    def test_real_texts(self):
        self.assertTrue(emergency_in_city(DNIPRO_NOW))
        self.assertTrue(emergency_in_city(DNIPRO_NOW.split("\n")[0]))
        for text in (KYIV_REGION, KYIV, None, ""):
            self.assertFalse(emergency_in_city(text), text)

    def test_wording(self):
        self.assertTrue(emergency_in_city("У м.Дніпро застосовано графіки аварійних відключень (ГАВ)."))
        self.assertTrue(emergency_in_city("За наказом НЕК Укренерго в Дніпропетровській області діють екстрені відключення."))
        self.assertFalse(emergency_in_city("Екстрені відключення в м. Дніпро скасовано. Діють графіки стабілізаційних відключень."))
        self.assertFalse(emergency_in_city("В усіх районах області, крім м. Дніпро, діють екстрені відключення."))
        self.assertFalse(emergency_in_city("У Дніпрі діють стабілізаційні відключення. Для решти області екстрені відключення."))

    def test_attention_from_page(self):
        html = ('<div class="m-attention"><div class="m-attention__text"><p>За наказом НЕК Укренерго в м. Дніпро&nbsp;'
                'діють <b>екстрені</b> відключення.</p><p>Для решти області стабілізаційні.</p><div>x</div></div></div>')
        self.assertEqual(
            providers.extract_attention(html),
            "За наказом НЕК Укренерго в м. Дніпро діють екстрені відключення.\nДля решти області стабілізаційні.\nx",
        )


class SiteParseTests(unittest.TestCase):
    def test_houses_and_groups(self):
        houses = dtek_site.parse_houses("вул. Микитинська", street_payload())
        self.assertEqual(len(houses), 6)
        states = dtek_site.group_states(houses)
        self.assertEqual((states["1.2"].total, len(states["1.2"].off)), (4, 3))
        self.assertEqual((states["1.2"].start, states["1.2"].end), (f"13:16 {D}", f"17:16 {T}"))
        self.assertEqual((states["3.1"].total, states["3.1"].off), (2, []))

    def test_classify(self):
        self.assertEqual(dtek_site.classify(EMERGENCY), "emergency")
        self.assertEqual(dtek_site.classify("Стабілізаційне відключення (Згідно графіку погодинних відключень)"), "schedule")
        self.assertEqual(dtek_site.classify("Аварійне відключення"), "accident")
        self.assertIsNone(dtek_site.classify(""))


class SiteProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_then_one_request_per_street(self):
        posts = []
        page = ('<html><head><meta name="csrf-token" content="tok123"></head><body>'
                '<div class="m-attention__text">' + DNIPRO_NOW + '</div>'
                '<script>DisconSchedule.fact = {"data": {}, "update": "20.11.2026 13:35", "today": 1}</script></body></html>')

        def respond(request):
            if request.method == "GET":
                return httpx.Response(200, text=page)
            form = dict(httpx.QueryParams(request.content.decode()))
            posts.append((form, request.headers.get("x-csrf-token")))
            return httpx.Response(200, json=street_payload() if form["data[1][value]"] == "вул. Микитинська" else {"data": {}})

        async_client = httpx.AsyncClient
        with patch.object(dtek_site.httpx, "AsyncClient", side_effect=lambda **kw: async_client(
            transport=httpx.MockTransport(respond), **kw,
        )), patch.object(dtek_site.asyncio, "sleep", AsyncMock()):
            result = await dtek_site.probe(["вул. Микитинська", "вул. Неіснуюча"])
        self.assertEqual(len(result.houses), 6)
        self.assertEqual(result.failed, ["вул. Неіснуюча"])
        self.assertTrue(emergency_in_city(result.attention))
        form, token = posts[0]
        self.assertEqual(token, "tok123")
        self.assertEqual(form["method"], "getHomeNum")
        self.assertEqual((form["data[0][value]"], form["data[1][value]"], form["data[2][value]"]),
                         ("м. Дніпро", "вул. Микитинська", "20.11.2026 13:35"))

    async def test_antibot_page_is_an_error(self):
        async_client = httpx.AsyncClient
        with patch.object(dtek_site.httpx, "AsyncClient", side_effect=lambda **kw: async_client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text='<iframe src="/_Incapsula_Resource">')), **kw,
        )):
            with self.assertRaises(ValueError):
                await dtek_site.probe(["вул. Микитинська"])


class DbTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", str(Path(self.tmp.name) / "test.db"))
        self.db_patch.start()
        await db.init()
        await db.set_user(1, grp="1.2", provider="dtek")
        await db.set_user(2, grp="3.1", provider="dtek")
        await db.set_user(3, grp="1.2", provider="cek")
        self.bot = AsyncMock()

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    def sent(self):
        out = [(c.args[0], c.args[1]) for c in self.bot.send_message.await_args_list]
        self.bot.send_message.reset_mock()
        return out

    async def verdict(self, grp="1.2"):
        sch = {"today": {grp: [Slot(16.5, 20.5)]}}
        with patch.object(status, "get_schedule", AsyncMock(return_value=sch)), patch.object(status, "now_h", return_value=13.5):
            return await status.verdict(grp, "dtek")


class SiteAlertTests(DbTestCase):
    async def check(self, payload):
        await dtek_site.apply(dtek_site.Probe(dtek_site.parse_houses("вул. Микитинська", payload)))
        await notifier.send_alerts(self.bot)
        return self.sent()

    async def test_group_gets_pushed_and_now_says_no_power(self):
        sent = await self.check(street_payload())
        self.assertEqual([uid for uid, _ in sent], [1])  # лише ДТЕК 1.2; ні 3.1, ні ЦЕК 1.2
        text = sent[0][1]
        self.assertIn("Екстрене відключення</b> · група <b>1.2", text)
        self.assertIn(f"з 13:16, орієнтовно до <b>17:16 {TS}", text)
        self.assertIn("3 з 4", text)
        self.assertIn("сайт ДТЕК", text)

        now = await self.verdict()
        self.assertIn("🔴 <b>Світла немає</b>", now)
        self.assertIn("екстрене відключення", now)
        self.assertIn("до <b>17:16", now)
        self.assertIn("3 з 4 перевірених адрес групи (сайт ДТЕК)", now)
        self.assertNotIn("локальна аварія", now)

        self.assertEqual(await self.check(street_payload()), [])  # той самий стан — без повторів
        self.assertEqual(await self.check(street_payload(end=f"17:30 {T}")), [])  # +14 хв — дрібниця
        sent = await self.check(street_payload(end=f"19:00 {T}"))
        self.assertEqual(len(sent), 1)
        self.assertIn("продовжено", sent[0][1])

        self.assertEqual(await self.check(street_payload(off_12=0)), [])  # одна перевірка — ще не віримо
        sent = await self.check(street_payload(off_12=0))
        self.assertEqual([uid for uid, _ in sent], [1])  # офіційне «дали» — одразу, без 5 хв очікування
        self.assertIn("закінчилось", sent[0][1])
        self.assertNotIn("Світла немає", await self.verdict())

    async def test_one_house_of_many_is_not_a_group_outage(self):
        data = {str(n): house() for n in range(1, 11)} | {"11": house(EMERGENCY, f"13:16 {D}", f"17:16 {T}")}
        self.assertEqual(await self.check({"data": data}), [])

    async def test_people_saying_on_during_official_outage(self):
        """Офіційно екстрене до 17:16: «світло є» не пишемо — вмикають поступово (скрін 20.11, 16:42)."""
        await self.check(street_payload())
        for who in "abc":  # троє «дали» — ще не новина
            await db.add_chat_msg("chat", "1.2 дали", None, "on", ["1.2"], who=who)
        text = await self.verdict()
        self.assertNotIn("🟢", text)
        self.assertIn("🔴 <b>Світла немає</b>", text)
        self.assertIn("дехто пише, що дали", text)
        for who in "de":  # п'ятеро — «частково», але все одно не «є»
            await db.add_chat_msg("chat", "1.2 дали", None, "on", ["1.2"], who=who)
        text = await self.verdict()
        self.assertIn("🟡 <b>Частково є світло</b>", text)
        self.assertNotIn("🟢", text)


class CityEmergencyTests(DbTestCase):
    async def test_push_once_to_all_groups_then_lifted(self):
        em = AsyncMock(side_effect=lambda prov: Emergency(True, DNIPRO_NOW, "oua") if prov == "dtek" else None)
        with patch.object(providers, "get_emergency", em):
            await notifier.emergency_check(self.bot)
            await notifier.emergency_check(self.bot)
        sent = self.sent()
        self.assertEqual(sorted(uid for uid, _ in sent), [1, 2])  # усім групам ДТЕК, один раз
        self.assertIn("екстрені відключення", sent[0][1])
        self.assertIn("Графік зараз не діє", sent[0][1])

        off = AsyncMock(side_effect=lambda prov: Emergency(False, "", "oua") if prov == "dtek" else None)
        sch = AsyncMock(return_value={"today": {"1.2": [Slot(16.5, 20.5)], "3.1": []}})
        with patch.object(providers, "get_emergency", off), patch.object(notifier, "get_schedule", sch):
            await notifier.emergency_check(self.bot)
            self.assertEqual(self.sent(), [])  # одна відповідь «немає» — чекаємо 10 хв
            with patch.object(notifier.time, "time", return_value=time.time() + 11 * 60):
                await notifier.emergency_check(self.bot)
        sent = self.sent()
        self.assertEqual(sorted(uid for uid, _ in sent), [1, 2])
        self.assertIn("скасували", sent[0][1])
        self.assertIsNone(await db.get_emergency("dtek"))

    async def test_now_screen_during_city_emergency(self):
        await db.set_emergency("dtek", {"since": int(time.time()), "text": DNIPRO_NOW, "source": "oua"})
        now = await self.verdict()
        self.assertIn("🟠 <b>Екстрені відключення</b>", now)
        self.assertIn("графік зараз не діє", now)
        self.assertNotIn("16:30", now)  # «далі 16:30–20:30» не показуємо — графік не діє

        # як на скріні о 13:29: є — 3, нема — 12
        for i in range(3):
            await db.add_chat_msg("chat", "1.2 дали", None, "on", ["1.2"], who=f"on{i}")
        for i in range(12):
            await db.add_chat_msg("chat", "1.2 нема світла", None, "off", ["1.2"], who=f"off{i}")
        now = await self.verdict()
        self.assertIn("🔴 <b>Світла немає</b>", now)
        self.assertIn("екстрені відключення · люди підтверджують", now)

    async def test_schedule_warnings_still_sent_during_emergency(self):
        async def fetch(provider, *, today):
            return {"today": providers.DaySchedule({"1.2": [Slot(13.75, 16)]}, "dtek"), "tomorrow": providers.DaySchedule()}

        await db.kv_set("sched_init", "1")
        await db.set_emergency("dtek", {"since": int(time.time()), "text": "", "source": "oua"})
        with patch.object(providers, "get_schedule_days", fetch), patch.object(notifier, "now_h", return_value=13.5):
            await notifier.tick(self.bot)
        warned = [uid for uid, t in self.sent() if "Через 15 хв" in t]
        self.assertEqual(sorted(warned), [1, 3])  # і ДТЕК 1.2 (екстрені), і ЦЕК 1.2

    async def test_crowd_push_is_labelled_emergency(self):
        await db.set_emergency("dtek", {"since": int(time.time()), "text": "", "source": "oua"})
        await db.kv_set("crowd:1.2", "on")
        for who in "ab":  # під час екстрених вистачає двох людей
            await db.add_chat_msg("chat", "1.2 вирубили", None, "off", ["1.2"], who=who)
        with patch.object(notifier, "get_schedule", AsyncMock(return_value={"today": {}})):
            await notifier.crowd_check(self.bot)
        sent = self.sent()
        self.assertEqual([uid for uid, _ in sent], [1])
        self.assertIn("екстрене відключення", sent[0][1])


class ScheduleScreenTests(DbTestCase):
    async def open_sched(self, day):
        cb = AsyncMock()
        cb.from_user.id = 1
        days = {
            "today": providers.DaySchedule({"1.2": [Slot(7.5, 10), Slot(16.5, 20.5)]}, "dtek", "20.11.2026 13:35"),
            "tomorrow": providers.DaySchedule({"1.2": [Slot(6, 10)]}, "dtek", "20.11.2026 13:35"),
        }
        with patch.object(handlers, "get_schedule_days", AsyncMock(return_value=days)):
            await handlers.sched(cb, kb.Nav(to="sched", arg=day))
        return cb.message.edit_text.await_args.args[0]

    async def test_today_shows_emergency_instead_of_schedule(self):
        self.assertIn("16:30–20:30", await self.open_sched("today"))  # без екстрених — звичайний графік

        await db.set_emergency("dtek", {"since": int(time.time()), "text": DNIPRO_NOW, "source": "oua"})
        text = await self.open_sched("today")
        self.assertIn("Зараз діють екстрені відключення", text)
        self.assertIn("07:30–10:00", text)  # графік показуємо і під час екстрених
        self.assertIn("16:30–20:30", text)

        await dtek_site.apply(dtek_site.Probe(dtek_site.parse_houses("вул. Микитинська", street_payload())))
        text = await self.open_sched("today")
        self.assertIn("🔴 Група <b>1.2</b> зараз без світла", text)
        self.assertIn(f"з 13:16, орієнтовно до <b>17:16 {TS}</b>", text)
        self.assertIn("3 з 4", text)

        tomorrow = await self.open_sched("tomorrow")
        self.assertIn("06:00–10:00", tomorrow)
        self.assertIn("якщо їх скасують", tomorrow)

    async def test_group_emergency_alone_keeps_schedule(self):
        await dtek_site.apply(dtek_site.Probe(dtek_site.parse_houses("вул. Микитинська", street_payload())))
        text = await self.open_sched("today")
        self.assertIn("Зараз діють екстрені", text)
        self.assertIn("16:30–20:30", text)
        self.assertNotIn("16:30", await self.verdict())

    async def test_today_schedule_updates_are_pushed_during_emergency(self):
        async def fetch(provider, *, today):
            return {"today": providers.DaySchedule({"3.1": [Slot(18, 20)]}, "dtek"),
                    "tomorrow": providers.DaySchedule({"3.1": [Slot(8, 10)]}, "dtek")}

        await db.kv_set("sched_init", "1")
        await db.set_emergency("dtek", {"since": int(time.time()), "text": "", "source": "oua"})
        with patch.object(providers, "get_schedule_days", fetch), patch.object(notifier, "now_h", return_value=13.5):
            await notifier.tick(self.bot)
        texts = [t for uid, t in self.sent() if uid == 2]
        self.assertEqual(len(texts), 2)
        self.assertIn("на сьогодні", texts[0])
        self.assertIn("поза цим графіком", texts[0])
        self.assertIn("на завтра", texts[1])


class GetEmergencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_notice_from_outage_data_ua(self):
        data = {"lastUpdated": "2099-01-01T00:00:00Z", "lastUpdateStatus": {"ok": True},
                "attention": {"text": DNIPRO_NOW}, "fact": {"data": {}}}
        with (
            patch.multiple(providers, SOURCE="dtek", CITY="dnipro", _site_cache={}, _site_locks={}, _notices={}),
            patch.object(providers, "fetch_dtek", AsyncMock(side_effect=httpx.ConnectError("blocked"))),
            patch.object(providers.httpx, "AsyncClient", side_effect=lambda **kw: _client(data, **kw)),
            self.assertLogs("providers", "WARNING"),
        ):
            em = await providers.get_emergency("dtek")
        self.assertTrue(em.active)
        self.assertEqual(em.source, "oua")
        self.assertIn("м. Дніпро", em.text)


_real_client = httpx.AsyncClient


def _client(data, **kw):
    return _real_client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data)), **kw)


if __name__ == "__main__":
    unittest.main()
