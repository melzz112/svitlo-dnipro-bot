"""Адреса людини: геопозиція/текст -> будинок на сайті ДТЕК -> група і точні пуші по будинку.
Запуск: python -m unittest discover -s tests -p "test_address.py" (з PYTHONPATH=.)"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import address
import db
import dtek_site
import handlers
import notifier
import status
from test_emergency import EMERGENCY, house

STREETS = ["вул. Соколина", "просп. Дмитра Яворницького", "просп. Петра Калнишевського", "вул. Шевченка",
           "пров. Шевченка", "вул. Березинська", "просп. Олександра Поля", "бульв. Слави"]


class StreetMatchTests(unittest.TestCase):
    def test_osm_and_typed_names(self):
        cases = {
            "Соколина вулиця": "вул. Соколина",  # так пише OSM
            "проспект Дмитра Яворницького": "просп. Дмитра Яворницького",
            "Яворницького": "просп. Дмитра Яворницького",  # люди пишуть коротко
            "провулок Шевченка": "пров. Шевченка",
            "Шевченка": "вул. Шевченка",  # без типу — вулиця
            "Березинская": "вул. Березинська",  # рос./одруківка
            "Олександра Поля проспект": "просп. Олександра Поля",
            "Слави бульвар": "бульв. Слави",
            "Неіснуюча": None,
        }
        for q, exp in cases.items():
            self.assertEqual(address.match_street(q, STREETS), exp, q)

    def test_house_numbers(self):
        for raw, exp in {"14 а": "14А", "14-а": "14А", "14a": "14А", "14/2": "14/2", "буд. 5": "5", "3": "3"}.items():
            self.assertEqual(address.norm_house(raw), exp, raw)
        self.assertEqual(address.find_house({"14А": 1, "3": 2}, "14 а"), ("14А", 1))
        self.assertIsNone(address.find_house({"3": 2}, "4"))

    def test_text_address(self):
        cases = {
            "Яворницького 3": ("Яворницького", "3"),
            "вул. Шевченка, 10а": ("вул. Шевченка", "10а"),
            "просп. Гагаріна 5/2": ("просп. Гагаріна", "5/2"),
            "м. Дніпро, вул. Соколина 12 кв 4": ("вул. Соколина", "12"),
            "Соколина": ("Соколина", None),
        }
        for text, exp in cases.items():
            self.assertEqual(address.parse_text_address(text), exp, text)

    def test_guess_names_without_street_list(self):
        self.assertEqual(address.guess_dtek_names("Соколина вулиця"), ["вул. Соколина"])
        self.assertEqual(address.guess_dtek_names("Яворницького")[0], "вул. Яворницького")


def page(streets=STREETS):
    return ('<html><head><meta name="csrf-token" content="tok"></head><body><script>'
            'DisconSchedule.fact = {"data": {}, "update": "20.11.2026 13:35", "today": 1}</script><script>'
            f'DisconSchedule.streets = {json.dumps({"м. Дніпро": streets}, ensure_ascii=False)};</script></body></html>')


def yavornytskoho(num3=None):
    """Будинки на просп. Яворницького: 3 (група 1.2, стан num3), 5 (1.2, світло є)."""
    return {"result": True, "data": {"3": num3 or house(), "5": house(), "14А": house(grp="3.1")}}


def mock_site(street_houses: dict, page_html=None):
    posts = []

    def respond(request):
        if request.method == "GET":
            return httpx.Response(200, text=page_html or page())
        form = dict(httpx.QueryParams(request.content.decode()))
        posts.append(form["data[1][value]"])
        return httpx.Response(200, json=street_houses.get(form["data[1][value]"], {"data": {}}))

    client = httpx.AsyncClient
    return posts, patch.object(dtek_site.httpx, "AsyncClient", side_effect=lambda **kw: client(
        transport=httpx.MockTransport(respond), **kw))


class DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", str(Path(self.tmp.name) / "t.db"))
        self.db_patch.start()
        await db.init()

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()


class LookupTests(DbCase):
    async def test_finds_street_from_page_list_and_house_group(self):
        posts, p = mock_site({"просп. Дмитра Яворницького": yavornytskoho()})
        with p:
            res = await dtek_site.lookup("Яворницького", "14 а")
        self.assertEqual(res.street, "просп. Дмитра Яворницького")
        self.assertEqual((res.house.num, res.house.groups), ("14А", ["3.1"]))
        self.assertEqual(posts, ["просп. Дмитра Яворницького"])  # один запит — на правильну вулицю
        self.assertIn("вул. Соколина", json.loads(await db.kv_get("dtek_streets")))

    async def test_house_not_in_dtek(self):
        _, p = mock_site({"просп. Дмитра Яворницького": yavornytskoho()})
        with p:
            res = await dtek_site.lookup("Яворницького", "99")
        self.assertEqual((res.street, res.house), ("просп. Дмитра Яворницького", None))

    async def test_unknown_street(self):
        posts, p = mock_site({})
        with p:
            res = await dtek_site.lookup("Неіснуюча", "1")
        self.assertIsNone(res.street)
        self.assertEqual(posts, [])

    async def test_no_street_list_on_page_guesses_names(self):
        bare = '<meta name="csrf-token" content="tok"><script>DisconSchedule.fact = {"update": "x"}</script>'
        posts, p = mock_site({"вул. Соколина": {"data": {"7": house(grp="5.1")}}}, page_html=bare)
        with p:
            res = await dtek_site.lookup("Соколина вулиця", "7")
        self.assertEqual((res.street, res.house.groups), ("вул. Соколина", ["5.1"]))


class HouseWatchTests(DbCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await db.set_user(1, grp="1.2", provider="dtek", street="просп. Дмитра Яворницького", house="3")
        await db.set_user(2, grp="1.2", provider="dtek")  # без адреси — особистих пушів немає
        self.users = await db.users_with_address()

    async def run_probe(self, num3):
        houses = dtek_site.parse_houses("просп. Дмитра Яворницького", yavornytskoho(num3))
        return await dtek_site.apply_users(dtek_site.Probe(houses), self.users)

    async def test_off_extended_and_back(self):
        em = lambda end: house(EMERGENCY, "13:16 20.11.2026", end)  # noqa: E731
        out = await self.run_probe(em("18:05 20.11.2026"))
        self.assertEqual([uid for uid, _ in out], [1])
        self.assertIn("У твоєму будинку зараз немає світла", out[0][1])
        self.assertIn("просп. Дмитра Яворницького, 3", out[0][1])
        self.assertIn("екстрене відключення", out[0][1])
        self.assertIn("до <b>18:05 20.11</b>", out[0][1])
        self.assertEqual(await self.run_probe(em("18:05 20.11.2026")), [])  # без змін — тиша
        self.assertEqual(await self.run_probe(em("18:20 20.11.2026")), [])  # +15 хв — не новина
        out = await self.run_probe(em("19:00 20.11.2026"))
        self.assertIn("продовжено", out[0][1])
        self.assertEqual(await self.run_probe(None), [])  # перший раз «світло є» — ще чекаємо (сайт блимає)
        out = await self.run_probe(None)
        self.assertIn("світло мало з'явитися", out[0][1])
        self.assertEqual(await self.run_probe(None), [])

    async def test_schedule_outage_is_not_pushed_but_seen(self):
        sched = house("Стабілізаційне відключення (Згідно графіку погодинних відключень)", "16:30 20.11.2026", "20:30 20.11.2026")
        self.assertEqual(await self.run_probe(sched), [])
        seen = json.loads(await db.kv_get("house_seen:1"))
        self.assertEqual(seen["kind"], "schedule")

    async def test_group_moved_by_dtek(self):
        out = await self.run_probe(house(grp="2.1"))
        self.assertIn("тепер у групі <b>2.1</b>", out[0][1])
        self.assertEqual((await db.get_user(1))["grp"], "2.1")

    async def test_now_screen_house_block(self):
        await self.run_probe(house(EMERGENCY, "13:16 20.11.2026", "18:05 20.11.2026"))
        block = await status.house_block(await db.get_user(1))
        self.assertIn("Твій будинок", block)
        self.assertIn("🔴 екстрене відключення до <b>18:05", block)
        self.assertIn("сайт ДТЕК", block)
        await db.kv_set("house_seen:1", json.dumps({"kind": None, "ts": int(time.time())}))
        self.assertIn("✅ відключень немає", await status.house_block(await db.get_user(1)))
        await db.kv_set("house_seen:1", json.dumps({"kind": None, "ts": int(time.time()) - 3600}))
        self.assertIn("не відповідає", await status.house_block(await db.get_user(1)))
        self.assertIsNone(await status.house_block(await db.get_user(2)))  # без адреси

    async def test_partial_group_share_on_now(self):
        payload = {"data": {str(n): house(EMERGENCY, "10:11 20.11.2026", "18:05 20.11.2026") if n <= 3 else house()
                            for n in range(1, 21)}}  # 3 з 20 = 15% — нижче порогу пуша
        await dtek_site.apply(dtek_site.Probe(dtek_site.parse_houses("вул. Х", payload)))
        self.assertIsNone(await db.last_alert("off", "1.2", 60, "dtek"))  # групі пуша немає
        with patch.object(status, "get_schedule", AsyncMock(return_value={"today": {"1.2": []}})):
            text = await status.verdict("1.2", "dtek")
        self.assertIn("🏠 Сайт ДТЕК: екстрене у 3 з 20 перевірених адрес групи, до <b>18:05", text)

    async def test_site_check_probes_user_streets_and_pushes(self):
        bot = AsyncMock()
        houses = dtek_site.parse_houses("просп. Дмитра Яворницького",
                                        yavornytskoho(house(EMERGENCY, "13:16 20.11.2026", "18:05 20.11.2026")))
        probe = AsyncMock(return_value=dtek_site.Probe(houses))
        with patch.object(dtek_site, "probe", probe), patch.multiple(notifier, _site_next=0.0, SOURCE="dtek", CITY="dnipro"), \
                patch.object(notifier.providers, "set_notice"):
            await notifier.site_check(bot)
        streets = probe.await_args.args[0]
        self.assertEqual(streets[0], "просп. Дмитра Яворницького")  # вулиці людей — першими
        sent = [c.args for c in bot.send_message.await_args_list]
        self.assertEqual([s[0] for s in sent], [1])
        self.assertIn("У твоєму будинку зараз немає світла", sent[0][1])


class FakeMsg:
    def __init__(self, text=None, location=None):
        self.text, self.location = text, location
        self.from_user = type("U", (), {"id": 1})()
        self.answers = []

    async def answer(self, text, reply_markup=None, **kw):
        self.answers.append((text, reply_markup))


class DialogTests(DbCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await db.set_user(1)
        self.state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))

    async def test_text_address_sets_group_and_address(self):
        res = dtek_site.Lookup("просп. Дмитра Яворницького",
                               dtek_site.parse_houses("просп. Дмитра Яворницького", yavornytskoho())[0], 3)
        await self.state.set_state(handlers.Addr.waiting)
        m = FakeMsg("Яворницького 3")
        with patch.object(dtek_site, "lookup", AsyncMock(return_value=res)) as lk:
            await handlers.addr_text(m, self.state)
        lk.assert_awaited_once_with("Яворницького", "3")
        u = await db.get_user(1)
        self.assertEqual((u["street"], u["house"], u["grp"], u["provider"]), ("просп. Дмитра Яворницького", "3", "1.2", "dtek"))
        self.assertIsNone(await self.state.get_state())
        self.assertTrue(any("Знайшов" in t and "група <b>1.2</b>" in t for t, _ in m.answers))

    async def test_location_without_house_asks_number(self):
        await self.state.set_state(handlers.Addr.waiting)
        loc = type("L", (), {"latitude": 48.46, "longitude": 35.04})()
        m = FakeMsg(location=loc)
        with patch.object(handlers, "reverse_geocode", AsyncMock(return_value=("Соколина вулиця", None, "Дніпро"))):
            await handlers.addr_location(m, self.state)
        self.assertEqual(await self.state.get_state(), handlers.Addr.house.state)
        self.assertIn("номер будинку", m.answers[-1][0])
        res = dtek_site.Lookup("вул. Соколина", dtek_site.parse_houses("вул. Соколина", {"data": {"12": house(grp="5.1")}})[0])
        m2 = FakeMsg("12")
        with patch.object(dtek_site, "lookup", AsyncMock(return_value=res)) as lk:
            await handlers.addr_house(m2, self.state)
        lk.assert_awaited_once_with("Соколина вулиця", "12")
        self.assertEqual((await db.get_user(1))["grp"], "5.1")

    async def test_house_not_in_dtek_suggests_cek(self):
        await self.state.set_state(handlers.Addr.waiting)
        m = FakeMsg("Яворницького 99")
        with patch.object(dtek_site, "lookup", AsyncMock(return_value=dtek_site.Lookup("просп. Дмитра Яворницького", None, 3))):
            await handlers.addr_text(m, self.state)
        self.assertTrue(any("ЦЕК" in t for t, _ in m.answers))
        self.assertEqual((await db.get_user(1))["street"], "")

    async def test_site_down(self):
        await self.state.set_state(handlers.Addr.waiting)
        m = FakeMsg("Яворницького 3")
        with patch.object(dtek_site, "lookup", AsyncMock(side_effect=ValueError("антибот"))):
            await handlers.addr_text(m, self.state)
        self.assertTrue(any("не відповідає" in t for t, _ in m.answers))
        self.assertIsNone(await self.state.get_state())

    async def test_other_city(self):
        await self.state.set_state(handlers.Addr.waiting)
        loc = type("L", (), {"latitude": 50.45, "longitude": 30.52})()
        m = FakeMsg(location=loc)
        with patch.object(handlers, "reverse_geocode", AsyncMock(return_value=("Хрещатик", "1", "Київ"))):
            await handlers.addr_location(m, self.state)
        self.assertIn("не Дніпро", m.answers[-1][0])


if __name__ == "__main__":
    unittest.main()
