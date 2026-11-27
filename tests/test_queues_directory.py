"""Черги до 18.1+, будинки без черги, локальний довідник вулиць, аварії по групах, зміни графіків YASNO, сідер."""
import importlib.util
import json
import sqlite3
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import cek_site
import db
import dtek_http
import dtek_site
import groups as G
import handlers
import keyboards as kb
import notifier
import parse
import providers
import status
import userbot
from config import TZ
from fake_dtek import fake_dtek
from providers import DaySchedule, Slot, parse_yasno_schedule

NEW_QUEUES = ["14.1", "15.1", "18.1"]
TOMORROW = f"{datetime.now(TZ) + timedelta(days=1):%d.%m.%Y}"
EMERGENCY = "Екстренні відключення (Аварійне без застосування графіку погодинних відключень)"


def dtek_house(grp: str | None = "14.1", sub="", start="", end="", cek=None):
    return {"sub_type": sub, "start_date": start, "end_date": end, "type": "2" if sub else "",
            "sub_type_reason": [f"GPV{grp}"] if grp else [], "voluntarily": None, "cek": cek}


def page(streets):
    data = {"смт Дніпровське": ["вул. Степова"], "м. Дніпро": streets}
    return ('<html><head><meta name="csrf-token" content="tok"></head><body><script>'
            'DisconSchedule.fact = {"data": {}, "update": "20.11.2026 13:35", "today": 1}</script><script>'
            f'DisconSchedule.streets = {json.dumps(data, ensure_ascii=False)}</script></body></html>')


class DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", str(Path(self.tmp.name) / "t.db"))
        self.db_patch.start()
        await db.init()
        G.reset()

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()
        G.reset()


class QueueCodeTests(unittest.TestCase):
    def test_any_queue_code(self):
        for code in ["1.1", "6.2", *NEW_QUEUES, "21.3", "1.10"]:
            self.assertTrue(G.is_group(code), code)
        for bad in ["", "1", "123.1", "1.123", "a.b", "0.1", "1.0", "1,1", "GPV14.1", " 14.1"]:
            self.assertFalse(G.is_group(bad), bad)
        self.assertEqual(G.norm("GPV14.1"), "14.1")
        self.assertEqual(G.norm("14.01"), "14.1")
        self.assertEqual(G.sort(["18.1", "2.1", "14.1", "6.2", "15.1", "1.2"]), ["1.2", "2.1", "6.2", "14.1", "15.1", "18.1"])

    def test_dtek_house_queues_and_no_queue(self):
        houses = dtek_site.parse_houses("вул. Березинська", {"data": {
            "1": dtek_house("14.1"), "2": dtek_house("15.1"), "3": dtek_house("18.1"), "4": dtek_house(None),
            "5": dtek_house("6.2"), "6": dtek_house(None, cek="GPV10014.1"),
        }})
        self.assertEqual([h.groups for h in houses], [["14.1"], ["15.1"], ["18.1"], [], ["6.2"], []])
        self.assertEqual(dtek_site.cek_group(houses[5]), "14.1")
        self.assertEqual(set(dtek_site.group_states(houses)), {"14.1", "15.1", "18.1", "6.2"})

    def test_yasno_any_queue(self):
        today = date(2026, 11, 20)
        slot = {"start": 600, "end": 720, "type": "Definite"}
        data = {g: {"today": {"date": "2026-11-20T00:00:00+02:00", "status": "ScheduleApplies", "slots": [slot]},
                    "updatedOn": "2026-11-20T08:14:00+00:00"} for g in ["1.1", *NEW_QUEUES]}
        data["junk"] = {"today": {}}
        result = parse_yasno_schedule(data, today)
        self.assertEqual(list(result["today"].groups), ["1.1", *NEW_QUEUES])
        self.assertTrue(all(G.is_known(g) for g in NEW_QUEUES))  # з'явились у реєстрі -> кнопки і розбір текстів

    def test_dtek_fact_any_queue(self):
        fact = {"GPV1.1": {"1": "no"}, "GPV14.1": {"2": "no"}, "GPV18.1": {"3": "first"}, "GPV10015.1": {"4": "no"}}
        self.assertEqual(list(providers._fact_groups(fact)), ["1.1", "14.1", "18.1"])
        self.assertEqual(list(providers._fact_groups(fact, "cek")), ["15.1"])

    def test_cek_site_any_queue(self):
        html = ("<p>📢 Повідомляємо про зміни у ГПВ 20 ЛИСТОПАДА з 14:00 до 24:00 діятимуть наступні черги:</p>"
                "<p>🔹 Черга 1.1<br />16:30-23:30</p><p>🔹 Черга 14.1<br />13:00-16:30</p><p>🔹 Черга 18.1<br />20:00-22:00</p>")
        st = cek_site.parse_page(html, date(2026, 11, 20))
        self.assertEqual(set(st.days[date(2026, 11, 20)].groups), {"1.1", "14.1", "18.1"})

    def test_keyboard_built_from_data(self):
        G.register(NEW_QUEUES)
        markup = kb.groups_kb("15.1")
        texts = [b.text for row in markup.inline_keyboard for b in row]
        for g in ["1.1", "6.2", "14.1", "18.1"]:
            self.assertIn(g, texts)
        self.assertIn("✅ 15.1", texts)
        self.assertTrue(all(len(row) <= 4 for row in markup.inline_keyboard))

    def test_free_text_only_known_queues(self):
        self.assertEqual(parse.find_groups("14.1 нема світла", user=True), [])  # ще не бачили в даних
        G.register(NEW_QUEUES + ["14.2"])
        self.assertEqual(parse.find_groups("14.1 нема світла", user=True), ["14.1"])
        self.assertEqual(parse.find_groups("черга 14 без світла", user=True), ["14.1", "14.2"])
        self.assertEqual(parse.find_groups("з 16.30 до 20.11.2026 вимкнули 18.1"), ["18.1"])
        self.assertEqual(parse.find_groups("на 20.11 о 16.30"), [])

    def test_schedule_post_with_new_labeled_queue(self):
        text = "Графік ДТЕК на завтра\nГрупа 1.1\n10:00-12:00\nГрупа 1.2\n12:00-14:00\nГрупа 17.1\n14:00-16:00"
        [(prov, _, grps)] = parse.parse_schedule_posts(text, date(2026, 11, 20))
        self.assertEqual((prov, sorted(grps)), ("dtek", ["1.1", "1.2", "17.1"]))


class DirectoryTests(DbCase):
    async def test_houses_with_any_queue_and_no_queue(self):
        await db.save_houses("вул. Березинська", [("45", [], ""), ("43", ["14.1"], ""), ("41 а", ["18.1"], ""),
                                                 ("2", [], "ЦЕК GPV10015.1")])
        rows = {r["num"]: r for r in await db.cached_houses("вул. Березинська")}
        self.assertEqual((rows["45"]["group_name"], rows["45"]["is_no_queue"]), (None, 1))
        self.assertEqual((rows["43"]["group_name"], rows["43"]["is_no_queue"]), ("14.1", 0))
        self.assertEqual((rows["2"]["operator"], rows["2"]["group_name"]), ("cek", "15.1"))
        self.assertEqual((await db.find_house("вул. Березинська", "41А"))["groups"], ["18.1"])
        self.assertTrue(await db.street_polled("вул. Березинська"))
        await db.save_houses("вул. Березинська", [("45", ["14.1"], "")])  # ДТЕК дав будинку чергу — upsert
        self.assertEqual((await db.find_house("вул. Березинська", "45"))["group_name"], "14.1")
        self.assertEqual(len(await db.cached_houses("вул. Березинська")), 4)
        G.reset()
        self.assertIn("18.1", await db.load_known_groups())

    async def test_lookup_from_directory_makes_no_requests(self):
        await db.upsert_streets(["вул. Березинська", "просп. Дмитра Яворницького"])
        await db.save_houses("вул. Березинська", [("45", [], ""), ("43", ["14.1"], "")])
        with fake_dtek(lambda m, u, f: (500, "")) as s:
            res = await dtek_site.lookup_cached("Березинська", "45")
            self.assertEqual((res.street, res.house.num, res.no_queue), ("вул. Березинська", "45", True))
            res = await dtek_site.lookup_cached("березинская", "43")
            self.assertEqual(res.house.groups, ["14.1"])
            self.assertEqual((await dtek_site.lookup_cached("Березинська", "99")).house, None)
            self.assertIsNone(await dtek_site.lookup_cached("Яворницького", "3"))  # вулицю ще не опитували
        self.assertEqual(s.calls, [])

    async def test_no_queue_house_bound_without_network(self):
        await db.upsert_streets(["вул. Березинська"])
        await db.save_houses("вул. Березинська", [("45", [], ""), ("43", ["14.1"], "")])
        await db.set_user(1, grp="1.2", provider="dtek")
        state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
        await state.set_state(handlers.Addr.waiting)
        m = _Msg("Березинська 45")
        with fake_dtek(lambda *a: (500, "")) as s, patch.object(dtek_site, "lookup", AsyncMock()) as lk:
            await handlers.addr_text(m, state)
        lk.assert_not_awaited()
        self.assertEqual(s.calls, [])  # 0 мережевих запитів
        u = await db.get_user(1)
        self.assertEqual((u["street"], u["house"], u["grp"], u["provider"]), ("вул. Березинська", "45", None, "dtek"))
        self.assertTrue(any("черги немає — стежу за самим будинком" in t for t, _ in m.answers))
        # будинок без черги — серед тих, за чиїми вулицями стежить сервер
        streets, *_ = await notifier.subscriber_streets()
        self.assertEqual(streets, ["вул. Березинська"])
        self.assertIn("без черги", handlers._menu_text(u))

    async def test_no_queue_house_gets_personal_push(self):
        await db.set_user(1, provider="dtek", street="вул. Березинська", house="45")
        off = dtek_house(None, EMERGENCY, f"13:16 {TOMORROW}", f"18:00 {TOMORROW}")
        houses = dtek_site.parse_houses("вул. Березинська", {"data": {"45": off}})
        out = await dtek_site.apply_users(dtek_site.Probe(houses), await db.users_with_address())
        self.assertEqual([uid for uid, _ in out], [1])
        self.assertIn("немає світла", out[0][1])

    async def test_migrates_old_house_cache(self):
        path = Path(self.tmp.name) / "old.db"
        con = sqlite3.connect(path)
        con.executescript(
            "CREATE TABLE dtek_houses(street TEXT, num TEXT, groups TEXT, cek TEXT, ts INTEGER, PRIMARY KEY(street, num));"
            "CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT);"
            "INSERT INTO dtek_houses VALUES('вул. Березинська','43','2.1','',1700000000);"
            "INSERT INTO dtek_houses VALUES('вул. Березинська','45','','',1700000000);"
            "INSERT INTO kv VALUES('dtek_streets', '[\"вул. Березинська\", \"вул. Соколина\"]');")
        con.commit()
        con.close()
        with patch.object(db, "DB_PATH", str(path)):
            await db.init()
            self.assertEqual((await db.find_house("вул. Березинська", "43"))["group_name"], "2.1")
            self.assertEqual((await db.find_house("вул. Березинська", "45"))["is_no_queue"], 1)
            self.assertEqual(await db.street_names(), ["вул. Березинська", "вул. Соколина"])
            self.assertEqual(await db.unpolled_streets(), ["вул. Соколина"])
            self.assertEqual((await db.find_house("вул. Березинська", "43"))["ts"], 1700000000)  # старий час
            seed = Path(self.tmp.name) / "seed.db"
            with patch.object(db, "DB_PATH", str(seed)):
                await db.init()
                await db.save_houses("вул. Березинська", [("43", ["14.1"], ""), ("45", ["14.1"], "")])
            _load_seed().import_from(str(seed), str(path))  # свіжий довідник перезаписує старий кеш
            self.assertEqual((await db.find_house("вул. Березинська", "45"))["group_name"], "14.1")
            await db.init()  # повторний старт — нічого не ламає


class _Msg:
    def __init__(self, text):
        self.text, self.location = text, None
        self.from_user = type("U", (), {"id": 1})()
        self.answers = []

    async def answer(self, text, reply_markup=None, **kw):
        self.answers.append((text, reply_markup))


class GentlePollTests(DbCase):
    async def test_polls_only_subscriber_streets(self):
        await db.set_user(1, grp="14.1", provider="dtek", street="вул. Березинська", house="43")
        await db.set_user(2, provider="dtek", street="вул. Робоча", house="5")  # без черги
        await db.set_user(3, grp="1.1", addr_q="Соколина|7")  # чекає на сайт
        await db.set_user(4, grp="1.1")  # без адреси — його вулиць не смикаємо
        await db.upsert_streets(["вул. Соколина", "вул. Березинська", "вул. Робоча", "вул. Космічна"])
        streets, *_ = await notifier.subscriber_streets()
        self.assertEqual(streets, ["вул. Соколина", "вул. Березинська", "вул. Робоча"])

    async def test_no_subscribers_no_requests_and_backoff(self):
        bot = AsyncMock()
        with fake_dtek(lambda *a: (403, "blocked")) as s, \
                patch.multiple(notifier, _site_next=0.0, SOURCE="dtek", CITY="dnipro", DTEK_POLL_INTERVAL_SEC=1800):
            await notifier.site_check(bot)
            self.assertEqual(s.calls, [])  # підписників з адресою немає — сайт не смикаємо
            await db.set_user(1, grp="14.1", provider="dtek", street="вул. Березинська", house="43")
            notifier._site_next = 0.0
            await notifier.site_check(bot)
            self.assertEqual(len(s.calls), 1)  # 403 на сторінці — і все
            self.assertGreater(dtek_http.backoff_left(), 1700)
            notifier._site_next = 0.0
            await notifier.site_check(bot)
            self.assertEqual(len(s.calls), 1)  # пауза: жодного запиту


class IncidentTests(DbCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        G.register(NEW_QUEUES)
        await db.set_user(1, grp="14.1", provider="dtek")
        await db.set_user(2, grp="14.1", provider="dtek")
        await db.set_user(3, grp="15.1", provider="dtek")
        await db.set_user(4, grp="14.1", provider="cek")
        await db.set_user(5, grp="14.1", provider="dtek", street="вул. Березинська", house="43")
        self.bot = AsyncMock()

    def sent(self):
        out = [(c.args[0], c.args[1]) for c in self.bot.send_message.await_args_list]
        self.bot.send_message.reset_mock()
        return out

    async def test_one_summary_per_group_incident_across_streets(self):
        end = f"18:00 {TOMORROW}"
        acc = "Аварійне відключення"
        b = dtek_site.parse_houses("вул. Березинська", {"data": {"43": dtek_house("14.1", acc, f"13:00 {TOMORROW}", end)}})
        k = dtek_site.parse_houses("вул. Космічна", {"data": {"7": dtek_house("14.1", acc, f"13:30 {TOMORROW}", end),
                                                                  "9": dtek_house("15.1", acc, f"13:30 {TOMORROW}", end)}})
        # жителю будинку — особистий пуш
        personal = await dtek_site.apply_users(dtek_site.Probe(b), await db.users_with_address())
        self.assertEqual([uid for uid, _ in personal], [5])
        self.assertEqual(await dtek_site.record_incidents(b + k), 2)  # (14.1, …) і (15.1, …)
        self.assertEqual(await dtek_site.record_incidents(k), 0)  # сусідня вулиця з тим самим — не новий
        await notifier.send_incidents(self.bot)
        sent = self.sent()
        self.assertEqual(sorted(uid for uid, _ in sent), [1, 2, 3])  # ЦЕК 14.1 — ні; житель (5) — уже особисто
        text = dict(sent)[1]
        self.assertIn("група 14.1", text)
        self.assertIn("аварійні ремонтні роботи", text)
        self.assertIn("до <b>18:00 (завтра)", text)
        await dtek_site.record_incidents(b + k)
        await notifier.send_incidents(self.bot)
        self.assertEqual(self.sent(), [])  # без дублів

        now = await _verdict("14.1")
        self.assertIn("🚧 Аварія по групі 14.1: аварійні ремонтні роботи", now)

    async def test_channel_and_site_same_incident_once(self):
        end = datetime.now(TZ).replace(second=0, microsecond=0) + timedelta(hours=3)
        until = end.strftime("%H:%M %d.%m")
        a = parse.Alert("off", "14.1", until, "🚨 <b>Аварійне/екстрене відключення</b> · група <b>14.1</b>")
        self.assertEqual(await userbot.channel_incident(a, extension=False), 1)  # алерт — лише для стану
        houses = dtek_site.parse_houses("вул. Космічна", {"data": {"7": dtek_house(
            "14.1", EMERGENCY, end.strftime("%H:%M %d.%m.%Y"), end.strftime("%H:%M %d.%m.%Y"))}})
        self.assertEqual(await dtek_site.record_incidents(houses), 0)  # той самий (група, причина, кінець)
        await notifier.send_incidents(self.bot)
        self.assertEqual(sorted(uid for uid, _ in self.sent()), [1, 2, 5])

    async def test_shifted_end_merged_then_extended(self):
        def at(hh):
            return dtek_site.parse_houses("вул. Космічна", {"data": {"7": dtek_house(
                "15.1", EMERGENCY, f"13:00 {TOMORROW}", f"{hh} {TOMORROW}")}})
        for hh, expect in [("17:00", 1), ("17:20", 0), ("19:30", 1)]:
            await dtek_site.record_incidents(at(hh))
            await notifier.send_incidents(self.bot)
            sent = self.sent()
            self.assertEqual(len(sent), expect, hh)
        self.assertIn("продовжено", sent[0][1])


async def _verdict(grp):
    with patch.object(status, "get_schedule", AsyncMock(return_value={"today": {grp: []}})):
        return await status.verdict(grp, "dtek")


class DtekPageBlockedTests(DbCase):
    async def test_blocked_dtek_page_falls_back_quietly(self):
        with patch.multiple(providers, SOURCE="dtek", _site_cache={}, _site_locks={}, _site_last_good={}),                 patch.object(providers, "fetch_yasno", AsyncMock(return_value={})),                 patch.object(providers, "fetch_outage_data_ua", AsyncMock(return_value={})),                 patch.object(providers, "fetch_dtek", AsyncMock(side_effect=dtek_http.Blocked("антибот"))),                 patch.object(providers, "channel_schedule", AsyncMock(return_value=DaySchedule())) as channel,                 self.assertLogs("providers", "WARNING"):
            days = await providers.get_schedule_days("dtek", today=date(2026, 11, 20))
        self.assertEqual(set(days), {"today", "tomorrow"})
        channel.assert_awaited()


class YasnoChangeTests(DbCase):
    async def test_detects_changes_per_queue_and_pushes_subscribers(self):
        today = datetime.now(TZ).date()

        def days(slots_141, slots_181):
            return {"today": DaySchedule({"14.1": slots_141, "18.1": slots_181, "1.1": [Slot(8, 10)]}, "yasno",
                                         "20.11.2026 08:14"), "tomorrow": DaySchedule(source="yasno")}

        first = await providers.track_yasno("dtek", today, days([Slot(10, 12)], [Slot(16, 18)]))
        self.assertEqual(first, [])  # перший знімок — без пушів
        self.assertEqual(await providers.track_yasno("dtek", today, days([Slot(10, 12)], [Slot(16, 18)])), [])
        providers.drain_schedule_changes()
        changes = await providers.track_yasno("dtek", today, days([Slot(10, 13), Slot(20, 21)], [Slot(16, 18)]))
        self.assertEqual([c.grp for c in changes], ["14.1"])  # 18.1 і 1.1 не змінились
        c = changes[0]
        self.assertEqual(c.added, [Slot(12, 13), Slot(20, 21)])
        self.assertEqual(c.removed, [])
        shrink = providers.ScheduleChange("dtek", "today", today, "14.1", [Slot(10, 13)], [Slot(11, 13)])
        self.assertEqual(shrink.removed, [Slot(10, 11)])

        await db.set_user(1, grp="14.1", provider="dtek")
        await db.set_user(2, grp="18.1", provider="dtek")
        await db.set_user(3, grp="14.1", provider="cek")
        await db.kv_set("sched_init", "1")

        async def fetch(provider, *, today):
            return days([Slot(10, 13), Slot(20, 21)], [Slot(16, 18)]) if provider == "dtek" else {
                "today": DaySchedule(), "tomorrow": DaySchedule()}

        bot = AsyncMock()
        with patch.object(providers, "get_schedule_days", fetch), patch.object(notifier, "now_h", return_value=3.0):
            await notifier.tick(bot)
            await notifier.tick(bot)
        msgs = [(call.args[0], call.args[1]) for call in bot.send_message.await_args_list]
        self.assertEqual([uid for uid, _ in msgs], [1])  # лише підписник ДТЕК 14.1, один раз
        text = msgs[0][1]
        self.assertIn("графік на сьогодні змінився</b> · група <b>14.1", text)
        self.assertIn("Було: 10:00–12:00", text)
        self.assertIn("➕ Додали: 12:00–13:00, 20:00–21:00", text)
        self.assertIn("YASNO", text)
        self.assertEqual(json.loads((await db.get_snapshots("dtek", today.isoformat()))["14.1"]["slots"]),
                         [[10, 13, "off"], [20, 21, "off"]])


def _load_seed():
    path = Path(__file__).resolve().parent.parent / "scripts" / "seed_streets.py"
    spec = importlib.util.spec_from_file_location("seed_streets", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SeedTests(DbCase):
    async def test_seed_collects_streets_and_resumes(self):
        seed = _load_seed()
        streets = ["вул. Березинська", "вул. Космічна", "вул. Робоча"]
        houses = {"вул. Березинська": {"43": dtek_house("14.1"), "45": dtek_house(None)},
                  "вул. Космічна": {"7": dtek_house("18.1")}, "вул. Робоча": {"5": dtek_house("15.1")}}
        asked = []

        def respond(method, url, form):
            if method == "GET":
                return 200, page(streets[:1])
            if form["method"] == "getStreets":
                return 200, {"result": True, "streets": {"м. Дніпро": streets, "смт Дніпровське": ["вул. Степова"]}}
            asked.append(form["data[1][value]"])
            return 200, {"result": True, "data": houses[form["data[1][value]"]]}

        path = str(Path(self.tmp.name) / "seed.db")
        args = type("A", (), {"db_path": path, "concurrency": 2, "refresh": False, "by_letters": False})()
        with patch.object(seed, "PAUSE", (0, 0)), patch.object(db, "DB_PATH", path):
            await db.init()
            await db.upsert_streets(["вул. Березинська"])
            await db.save_houses("вул. Березинська", [("43", ["14.1"], "")])  # опитана до Ctrl+C
            with fake_dtek(respond):
                await seed.seed(args)
            self.assertCountEqual(asked, ["вул. Космічна", "вул. Робоча"])  # докачка: лише неопитані
            st = await db.directory_stats()
            self.assertEqual((st["streets"], st["polled"]), (3, 3))
            self.assertEqual((await db.find_house("вул. Космічна", "7"))["group_name"], "18.1")
            self.assertNotIn("вул. Степова", await db.street_names())
            # перенесення на сервер: у робочу БД з користувачами
            main = str(Path(self.tmp.name) / "main.db")
            with patch.object(db, "DB_PATH", main):
                await db.init()
                await db.set_user(77, grp="1.1")
            seed.import_from(path, main)
            with patch.object(db, "DB_PATH", main):
                self.assertEqual((await db.get_user(77))["grp"], "1.1")
                self.assertEqual((await db.find_house("вул. Робоча", "5"))["group_name"], "15.1")
                self.assertEqual(await db.unpolled_streets(), [])


if __name__ == "__main__":
    unittest.main()


class PollIntervalTests(DbCase):
    async def test_fast_while_outage_calm_otherwise(self):
        await db.set_user(1, grp="14.1", provider="dtek", street="вул. Березинська", house="43")
        users = await db.users_with_address()
        self.assertEqual(await notifier.poll_interval(users), 600)
        await db.kv_set("house:1", json.dumps({"start": "", "end": "", "miss": 0}))  # у підписника нема світла
        self.assertEqual(await notifier.poll_interval(users), 300)
        await db.kv_set("house:1", "")
        self.assertEqual(await notifier.poll_interval(users), 600)
        end = int(time.time()) + 3600
        await db.upsert_incident("dtek", "14.1", "вул. Космічна", "аварійні ремонтні роботи", None, end)
        self.assertEqual(await notifier.poll_interval(users), 300)  # аварія по його черзі
        await db.expire_incidents(end + 1)
        await db.set_emergency("dtek", {"since": 1, "text": ""})
        self.assertEqual(await notifier.poll_interval(users), 300)  # екстрені в місті

    async def test_site_check_schedules_next_poll(self):
        await db.set_user(1, grp="14.1", provider="dtek", street="вул. Березинська", house="43")
        off = dtek_house("14.1", EMERGENCY, f"13:00 {TOMORROW}", f"18:00 {TOMORROW}")
        probe = dtek_site.Probe(dtek_site.parse_houses("вул. Березинська", {"data": {"43": off}}), page_fetched=False)
        with patch.object(dtek_site, "probe", AsyncMock(return_value=probe)), \
                patch.multiple(notifier, _site_next=0.0, SOURCE="dtek", CITY="dnipro"), \
                patch.object(notifier.providers, "set_notice") as notice:
            await notifier.site_check(AsyncMock())
            left = notifier._site_next - time.monotonic()
        self.assertTrue(250 < left <= 300, left)  # у будинку підписника зараз аварія -> раз на 5 хв
        notice.assert_not_called()  # сторінку не тягнули — вікно «Шановні клієнти!» не затираємо
