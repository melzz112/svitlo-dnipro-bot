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


class PollSoonTests(DbCase):
    async def test_new_address_triggers_check_once_a_minute(self):
        await db.set_user(1, grp="14.1", provider="dtek", street="вул. Березинська", house="43")
        houses = dtek_site.parse_houses("вул. Березинська", {"data": {"43": dtek_house("14.1")}})
        probe = AsyncMock(return_value=dtek_site.Probe(houses))
        with patch.object(dtek_site, "probe", probe), \
                patch.multiple(notifier, _site_next=time.monotonic() + 500, SOURCE="dtek", CITY="dnipro"), \
                patch.object(notifier.providers, "set_notice"):
            await notifier.site_check(AsyncMock())
            self.assertEqual(probe.await_count, 0)  # планова перевірка ще не скоро
            dtek_site.poll_soon()
            await notifier.site_check(AsyncMock())
            self.assertEqual(probe.await_count, 1)  # людина вказала адресу — перевірили одразу
            dtek_site.poll_soon()
            await notifier.site_check(AsyncMock())
            self.assertEqual(probe.await_count, 1)  # але не частіше раза на хвилину
            planned = int(await db.kv_get("site_next_ts"))
        self.assertTrue(550 <= planned - time.time() <= 600)  # наступна — за розкладом (10 хв)
        self.assertIsNotNone(await db.kv_get("house_seen:1"))

    async def test_now_screen_says_real_next_check(self):
        await db.set_user(1, grp="14.1", provider="dtek", street="вул. Березинська", house="43")
        u = await db.get_user(1)
        await db.kv_set("site_probe", json.dumps({"ok": True, "ts": int(time.time()) - 60}))
        await db.kv_set("site_next_ts", str(int(time.time()) + 240))
        nxt = datetime.fromtimestamp(int(time.time()) + 240, TZ).strftime("%H:%M")
        self.assertIn(f"наступна перевірка ≈ {nxt}", await status.house_block(u))
        await db.kv_set("site_next_ts", str(int(time.time()) + 30))
        self.assertIn("перевіряю просто зараз", await status.house_block(u))

    async def test_binding_from_directory_asks_for_check(self):
        await db.upsert_streets(["вул. Березинська"])
        await db.save_houses("вул. Березинська", [("43", ["14.1"], "")])
        await db.set_user(1)
        state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
        await state.set_state(handlers.Addr.waiting)
        m = _Msg("Березинська 43")
        await handlers.addr_text(m, state)
        self.assertTrue(dtek_site.take_poll_soon())
        self.assertTrue(any("за хвилину буде" in t for t, _ in m.answers))


class DirectoryRefreshTests(DbCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        import directory_refresh as dr
        self.dr = dr
        await db.upsert_streets(["вул. Березинська", "вул. Космічна", "вул. Робоча"])
        await db.save_houses("вул. Березинська", [("43", ["1.2"], ""), ("45", [], "")])  # старий стан
        await db.save_houses("вул. Космічна", [("7", ["14.1"], "")])
        await db.save_houses("вул. Робоча", [("5", ["15.1"], "")])
        self.site = {"вул. Березинська": {"43": dtek_house("14.1"), "45": dtek_house("14.1")},
                     "вул. Космічна": {"7": dtek_house("14.1")}, "вул. Робоча": {"5": dtek_house("15.1")}}

    def respond(self, method, url, form):
        if method == "GET":
            return 200, page(list(self.site))
        if form["method"] == "getStreets":
            return 200, {"result": True, "streets": {"м. Дніпро": list(self.site)}}
        self.asked.append(form["data[1][value]"])
        return 200, {"result": True, "data": self.site[form["data[1][value]"]]}

    async def test_full_refresh_with_pace_backoff_stop_and_summary(self):
        self.asked, bot = [], AsyncMock()
        with fake_dtek(self.respond), patch.object(self.dr, "ADMIN_IDS", [77]):
            await self.dr.start("fast")
            self.assertTrue(await self.dr.step(bot))  # спершу — перелік вулиць
            await db.kv_set(self.dr.KEY, json.dumps({**await self.dr.state(), "last": 0}))
            self.assertTrue(await self.dr.step(bot))
            self.assertFalse(await self.dr.step(bot))  # темп: наступна вулиця — через 6 с
            dtek_http.backoff("тест")
            await db.kv_set(self.dr.KEY, json.dumps({**await self.dr.state(), "last": 0}))
            self.assertFalse(await self.dr.step(bot))  # сайт на паузі — чекаємо
            dtek_http.clear_backoff()
            await self.dr.stop()
            self.assertFalse(await self.dr.step(bot))
            self.assertIn("зупинено на 1/3", await self.dr.report())
            await self.dr.start("slow")  # продовжує, а не з нуля
            for _ in range(3):
                await db.kv_set(self.dr.KEY, json.dumps({**await self.dr.state(), "last": 0}))
                await self.dr.step(bot)
        self.assertEqual(sorted(self.asked), ["вул. Березинська", "вул. Космічна", "вул. Робоча"])  # кожна — раз
        st = await self.dr.state()
        self.assertEqual((st["running"], st["done"], st["failed"]), (False, 3, 0))
        self.assertEqual((await db.find_house("вул. Березинська", "45"))["group_name"], "14.1")
        uid, text = bot.send_message.await_args.args
        self.assertEqual(uid, 77)
        self.assertIn("Довідник адрес оновлено", text)
        self.assertIn("1.2: 1 → 0 (-1)", text)
        self.assertIn("14.1: 1 → 3 (+2)", text)
        self.assertIn("без черги: 1 → 0 (-1)", text)
        self.assertIn("Останнє оновлення", await self.dr.report())

    async def test_admin_db_command(self):
        import directory_refresh as dr
        m = _Msg("/db")
        await handlers.db_cmd(m)
        text, markup = m.answers[0]
        self.assertIn("Довідник адрес ДТЕК", text)
        self.assertIn("Вулиць: 3", text)
        buttons = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("adm:db:start:slow", buttons)
        cb = AsyncMock()
        cb.data = "adm:db:start:fast"
        cb.message.edit_text = AsyncMock()
        await handlers.db_action(cb)
        self.assertTrue((await dr.state())["running"])
        self.assertIn("Оновлення йде", cb.message.edit_text.await_args.args[0])
        cb.data = "adm:db:stop"
        await handlers.db_action(cb)
        self.assertFalse((await dr.state())["running"])


class DirectoryNewAndGoneTests(DirectoryRefreshTests):
    async def test_new_street_new_house_and_demolished_house(self):
        self.site["вул. Нова"] = {"1": dtek_house("18.1"), "3": dtek_house(None)}  # нова вулиця на сайті
        self.site["вул. Космічна"] = {"7": dtek_house("14.1"), "9": dtek_house("14.1")}  # новий будинок 9
        self.site["вул. Робоча"] = {}  # порожня відповідь — збій, будинок 5 не чіпаємо
        self.site["вул. Березинська"] = {"43": dtek_house("14.1")}  # будинок 45 знесли
        self.asked, bot = [], AsyncMock()
        with fake_dtek(self.respond), patch.object(self.dr, "ADMIN_IDS", [77]):
            await self.dr.start("fast")
            for _ in range(7):
                await db.kv_set(self.dr.KEY, json.dumps({**await self.dr.state(), "last": 0}))
                await self.dr.step(bot)
        self.assertIn("вул. Нова", self.asked)
        self.assertEqual((await db.find_house("вул. Нова", "1"))["group_name"], "18.1")
        self.assertIsNotNone(await db.find_house("вул. Космічна", "9"))
        self.assertIsNone(await db.find_house("вул. Березинська", "45"))
        self.assertIsNotNone(await db.find_house("вул. Робоча", "5"))
        st = await self.dr.state()
        self.assertEqual((st["new_streets"], st["added"], st["removed"], st["total"]), (1, 3, 1, 4))
        text = bot.send_message.await_args.args[1]
        self.assertIn("Нових вулиць: 1 · нових будинків: 3 · прибрано будинків: 1", text)


class PartialOutageWordingTests(DbCase):
    async def _verdict_with(self, off: int, total: int) -> str:
        end = (datetime.now(TZ) + timedelta(hours=3)).strftime("%H:%M %d.%m.%Y")
        await db.kv_set("site_grp:14.1", json.dumps({"total": total, "off": off, "end": end, "ts": int(time.time()),
                                                     "streets": ["вул. Тополина"], "n_streets": 1}))
        await db.add_alert(f"site:off:14.1:{off}", "off", "14.1", dtek_site.short_dt(end),
                           "🚨 <b>Екстрене відключення</b> · група <b>14.1</b>", "dtek", "dteksite", sent=1)
        for who in "abcde":
            await db.add_chat_msg(f"chat{off}", f"14.1 дали {who}{off}", None, "on", ["14.1"], who=f"{who}{off}")
        return await _verdict("14.1")

    async def test_time_not_for_whole_group_when_only_part_of_addresses(self):
        G.register(NEW_QUEUES)
        text = await self._verdict_with(222, 575)
        self.assertIn("🟢 <b>Світло є, але не в усіх</b>", text)  # 39% адрес — не «у групи світла немає»
        self.assertIn("екстрене лише на частині адрес групи", text)
        self.assertNotIn("Світла немає", text)
        self.assertNotIn("офіційно — відключення до", text)
        self.assertIn("екстрене у 222 з 575 перевірених адрес групи, до", text)  # час — при адресах

    async def test_time_kept_when_whole_group_is_off(self):
        G.register(NEW_QUEUES)
        text = await self._verdict_with(575, 575)
        self.assertIn("офіційно — відключення до", text)


class OffLockTests(DbCase):
    """Перше «вимкнули» — пуш; далі «вимкнули» з чатів/датчиків для тієї ж групи — тиша, доки надійно не дали."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        G.register(NEW_QUEUES)
        await db.set_user(1, grp="14.1", provider="dtek")
        self.n = 0

    async def wave(self, event: str, people: int = 4):
        self.n += 1
        for i in range(people):
            await db.add_chat_msg("chat", f"14.1 {event} {self.n}-{i}", None, event, ["14.1"], who=f"w{self.n}-{i}")

    async def crowd(self) -> list[str]:
        await db.kv_set("crowd_ts:14.1", "0")  # без 20-хвилинного інтервалу між пушами
        bot = AsyncMock()
        with patch.object(notifier, "get_schedule", AsyncMock(return_value={"today": {"14.1": []}})), \
                patch.object(status, "get_schedule", AsyncMock(return_value={"today": {"14.1": []}})):
            await notifier.crowd_check(bot)
        return [c.args[1] for c in bot.send_message.await_args_list]

    async def clear_chat(self):
        await db._exec("DELETE FROM chat_msgs")

    async def test_second_off_from_chats_is_silent_until_reliable_on(self):
        await self.wave("off")
        sent = await self.crowd()
        self.assertEqual(len(sent), 1)
        self.assertIn("вимкнули світло", sent[0])
        await self.clear_chat()
        await self.wave("on", 8)
        self.assertIn("дали світло", (await self.crowd())[0])  # «дали» з чатів — пуш є, але замок лишається
        await self.clear_chat()
        await self.wave("off")
        self.assertEqual(await self.crowd(), [])  # знову «нема» в чатах — група вже знає
        await notifier.unlock_off("dtek", "14.1")  # датчики / канал / сайт ДТЕК: світло дали
        await self.clear_chat()
        await self.wave("on", 8)
        await self.crowd()
        await self.clear_chat()
        await self.wave("off")
        self.assertEqual(len(await self.crowd()), 1)  # нове відключення — знову пуш

    async def test_lock_expires(self):
        await notifier.lock_off("dtek", "14.1", "crowd")
        self.assertTrue(await notifier.off_locked("dtek", "14.1"))
        with patch.object(notifier.time, "time", return_value=time.time() + notifier.OFF_LOCK_SEC + 1):
            self.assertFalse(await notifier.off_locked("dtek", "14.1"))

    async def test_official_alerts_lock_and_unlock(self):
        await db.add_alert("ch-off", "off", "14.1", "18:00", "🚨 <b>Аварійне відключення</b> · група <b>14.1</b>")
        await notifier.send_alerts(AsyncMock())
        self.assertTrue(await notifier.off_locked("dtek", "14.1"))
        await db.add_alert("site-on", "on", "14.1", None, "✅ Група 14.1: екстрене закінчилось", "dtek", "dteksite")
        await notifier.send_alerts(AsyncMock())
        self.assertFalse(await notifier.off_locked("dtek", "14.1"))


class SvitlobotDirectoryTests(DbCase):
    async def test_point_group_comes_from_directory(self):
        import svitlobot as svb
        from test_svitlobot import row

        await db.upsert_streets(["вул. Калинова"])
        await db.save_houses("вул. Калинова", [("82", ["17.1"], ""), ("84", [], "")])
        raw = "\n".join([
            row(1, 1),  # підпис «Група 1.2», але за довідником будинок у 17.1
            row(2, 1, addr="Дніпро -> Невідома 5"),  # адреси в довіднику немає — лишається підпис 1.2
            row(3, 2, addr="Дніпро -> вул. Калинова 82 (п.1-3)", extra=""),  # без підпису — з довідника
            row(4, 1, addr="Дніпро -> Калинова 84"),  # будинок без черги — у групах не рахуємо
            row(5, 1, addr="Київ -> Калинова 82"),  # інше місто
        ])
        with patch.object(svb, "_dir_ts", 0.0):
            pts = await svb.resolve_points(raw, mapping={})
        self.assertEqual([(p.id, p.provider, p.grp) for p in pts],
                         [("1", "dtek", "17.1"), ("2", "dtek", "1.2"), ("3", "dtek", "17.1")])


class HealthTests(DbCase):
    async def test_admin_alert_after_30_min_and_recovery(self):
        import health
        bot = AsyncMock()
        with patch.object(health, "ADMIN_IDS", [77]):
            await health.mark("yasno", False, "ConnectTimeout")
            await health.check(bot)
            self.assertEqual(bot.send_message.await_count, 0)  # ще не 30 хв
            st = await health.get("yasno")
            await db.kv_set("health:yasno", json.dumps({**st, "down_since": int(time.time()) - 31 * 60}))
            await health.mark("yasno", False, "ConnectTimeout")  # повторна помилка — не новий збій
            await health.check(bot)
            await health.check(bot)
            self.assertEqual(bot.send_message.await_count, 1)
            self.assertIn("YASNO (графіки)</b> недоступне", bot.send_message.await_args.args[1])
            await health.mark("yasno", True)
            await health.check(bot)
            self.assertIn("знову працює (не було", bot.send_message.await_args.args[1])
            await health.check(bot)
            self.assertEqual(bot.send_message.await_count, 2)


class StreetOptionsTests(unittest.TestCase):
    def test_options(self):
        import address
        streets = ["вул. Шевченка", "пров. Шевченка", "вул. Березинська", "просп. Дмитра Яворницького",
                   "вул. Шевчука"]
        self.assertEqual(address.street_options("Шевченка", streets), ["вул. Шевченка", "пров. Шевченка"])
        self.assertEqual(address.street_options("шевч", streets), ["вул. Шевчука", "вул. Шевченка", "пров. Шевченка"])
        self.assertEqual(address.street_options("Березинськая", streets), ["вул. Березинська"])
        self.assertEqual(address.street_options("Хрещатик", streets), [])


class StreetPickDialogTests(DbCase):
    async def test_ambiguous_street_buttons(self):
        await db.upsert_streets(["вул. Шевченка", "пров. Шевченка", "вул. Шевчука"])
        await db.save_houses("пров. Шевченка", [("5", ["16.1"], "")])
        await db.set_user(1)
        state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
        await state.set_state(handlers.Addr.waiting)
        m = _Msg("шевч 5")
        with patch.object(dtek_site, "site_blocked", AsyncMock(return_value=True)):
            await handlers.addr_text(m, state)
        text, markup = m.answers[-1]
        self.assertIn("Яку вулицю маєш на увазі", text)
        names = [b.text for row in markup.inline_keyboard for b in row]
        self.assertIn("пров. Шевченка", names)
        cb = AsyncMock()
        cb.from_user.id = 1
        cb.message = _Msg("")
        await handlers.addr_pick(cb, kb.StreetPick(i=names.index("пров. Шевченка")), state)
        u = await db.get_user(1)
        self.assertEqual((u["street"], u["house"], u["grp"]), ("пров. Шевченка", "5", "16.1"))


class InlineTests(DbCase):
    async def test_inline_lookup_from_directory(self):
        await db.upsert_streets(["вул. Микитинська", "вул. Березинська"])
        await db.save_houses("вул. Микитинська", [("14", ["14.1"], ""), ("16", [], "")])
        q = AsyncMock()
        q.query = "Микитинська 14"
        with fake_dtek(lambda *a: (500, "")) as s:
            await handlers.inline_lookup(q)
            self.assertEqual(s.calls, [])  # лише довідник
        results = q.answer.await_args.args[0]
        self.assertEqual(results[0].title, "вул. Микитинська, 14 — ДТЕК · черга 14.1")
        self.assertIn("черга 14.1", results[0].input_message_content.message_text)
        q.query = "Микитинська 16"
        await handlers.inline_lookup(q)
        self.assertIn("без черги", q.answer.await_args.args[0][0].title)
        q.query = "Неіснуюча 1"
        await handlers.inline_lookup(q)
        self.assertEqual(q.answer.await_args.args[0][0].id, "hint")


class QuietDigestTests(DbCase):
    async def test_quiet_hours_only_urgent(self):
        await db.set_user(1, grp="14.1", provider="dtek", quiet=1)
        bot = AsyncMock()
        night = datetime(2026, 11, 27, 2, 0, tzinfo=TZ)
        with patch.object(notifier, "datetime", wraps=datetime) as dt:
            dt.now.return_value = night
            await notifier._send(bot, 1, "графік оновився")
            await notifier._send(bot, 1, "🏠 у твоєму будинку немає світла", urgent=True)
            dt.now.return_value = night.replace(hour=9)
            await notifier._send(bot, 1, "графік оновився")
        self.assertEqual([c.args[1] for c in bot.send_message.await_args_list],
                         ["🏠 у твоєму будинку немає світла", "графік оновився"])

    async def test_morning_digest_once(self):
        await db.set_user(1, grp="14.1", provider="dtek", digest=1, street="вул. Микитинська", house="14")
        await db.set_user(2, grp="14.1", provider="dtek")
        bot = AsyncMock()
        days = {"today": DaySchedule({"14.1": [Slot(10, 12)]}, "yasno"), "tomorrow": DaySchedule()}
        morning = datetime.now(TZ).replace(hour=notifier.DIGEST_HOUR, minute=3)
        with patch.object(notifier, "datetime", wraps=datetime) as dt, \
                patch.object(providers, "get_schedule_days", AsyncMock(return_value=days)):
            dt.now.return_value = morning
            await notifier.morning_digest(bot)
            await notifier.morning_digest(bot)
        self.assertEqual(bot.send_message.await_count, 1)
        uid, text = bot.send_message.await_args.args
        self.assertEqual(uid, 1)
        self.assertIn("Доброго ранку", text)
        self.assertIn("Твій будинок", text)
        self.assertIn("10:00", text)

    def test_settings_buttons(self):
        markup = kb.settings_kb({"grp": "14.1", "notify_on": 1, "notify_min": 15, "quiet": 1, "digest": 0})
        texts = [b.text for row in markup.inline_keyboard for b in row]
        self.assertTrue(any(t.startswith("🌙 Тихі години") and t.endswith("увімк") for t in texts))
        self.assertTrue(any(t.startswith("☀️ Зведення") and t.endswith("вимк") for t in texts))


class AckTests(unittest.IsolatedAsyncioTestCase):
    async def test_callback_answered_before_handler(self):
        order = []
        cb = AsyncMock()
        cb.answer.side_effect = lambda *a, **k: order.append("ack")

        async def handler(event, data):
            order.append("handler")

        await handlers.ack_early(handler, cb, {})
        self.assertEqual(order, ["ack", "handler"])
        cb.answer.side_effect = RuntimeError("query is too old")
        await handlers._ack(cb)  # помилка відповіді не ламає обробку


class ChatsVsSensorsTests(DbCase):
    """«Нема» від кількох людей у чатах не робить «у всієї групи світла немає», якщо датчики бачать світло."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        G.register(NEW_QUEUES)
        await db.set_user(1, grp="14.1", provider="dtek")
        for i in range(4):
            await db.add_chat_msg("chat", f"14.1 нема {i}", None, "off", ["14.1"], who=f"p{i}")

    async def sensors(self, state):
        await db.kv_set("svb:dtek:14.1", json.dumps({"state": state, "on": 12, "off": 1, "maint": 0,
                                                     "ts": int(time.time()), "since": int(time.time())}))

    async def test_sensors_on_means_part_of_group(self):
        await self.sensors("on")
        text = await _verdict("14.1")
        self.assertIn("🟠 <b>Частина групи без світла</b>", text)
        self.assertNotIn("🔴 <b>Світла немає</b>", text)
        bot = AsyncMock()
        with patch.object(notifier, "get_schedule", AsyncMock(return_value={"today": {"14.1": []}})),                 patch.object(status, "get_schedule", AsyncMock(return_value={"today": {"14.1": []}})):
            await notifier.crowd_check(bot)
        push = bot.send_message.await_args.args[1]
        self.assertIn("у частини людей вимкнули світло", push)
        self.assertIn("в більшості будинків групи світло є", push)

    async def test_sensors_off_keeps_no_power(self):
        await self.sensors("off")
        self.assertIn("🔴 <b>Світла немає</b>", await _verdict("14.1"))


class ChatStreetTests(DbCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        import chat_streets
        self.cs = chat_streets
        await db.upsert_streets(["вул. Калинова", "вул. Тополина", "вул. Березинська", "вул. Робоча",
                                 "просп. Дмитра Яворницького", "вул. Яворницького"])
        await db.save_houses("вул. Тополина", [(str(n), ["14.1"], "") for n in range(1, 40)])
        await db.save_houses("вул. Калинова", [(str(n), ["16.1"], "") for n in range(1, 40)])
        await db.save_houses("вул. Березинська", [("45", ["14.1"], "")])
        await db.save_houses("вул. Робоча", [("5", ["15.1"], ""), ("7", ["13.1"], "")])
        await db.save_houses("просп. Дмитра Яворницького", [("50", ["18.1"], "")])
        await db.save_houses("вул. Яворницького", [("2", ["17.1"], "")])
        await chat_streets.index.build()

    async def test_street_and_queue_from_text(self):
        m = self.cs.index.match
        self.assertEqual(await m("ул Тополиная 13 дали"), ("вул. Тополина", "14.1", "13"))
        self.assertEqual(await m("вул. Калинова нема"), ("вул. Калинова", "16.1", None))
        self.assertEqual(await m("Робоча 7 нема"), ("вул. Робоча", "13.1", "7"))
        self.assertEqual(await m("Дмитра Яворницького 50 нема"), ("просп. Дмитра Яворницького", "18.1", "50"))
        self.assertIsNone(await m("Яворницкого света нет"))  # і проспект, і вулиця — без будинку не вгадуємо
        self.assertIsNone(await m("на роботі нема світла"))  # звичайне слово
        self.assertIsNone(await m("на Березинке дали"))  # масив, а не вулиця
        self.assertIsNone(await m("Тополь 3 нет света"))

    async def test_old_group_replaced_by_street_queue(self):
        res = {"event": "on", "groups": ["1.2"], "street": None, "district": None, "provider": ""}
        out = await self.cs.apply(res, "1.2 березинская 45 дали")
        self.assertEqual((out["groups"], out["street"]), (["14.1"], "вул. Березинська"))
        kept = await self.cs.apply({**res, "groups": ["14.1"]}, "вул. Калинова 5 дали")
        self.assertEqual(kept["groups"], ["14.1"])  # жива черга від людини — не чіпаємо
        none = await self.cs.apply({**res, "groups": []}, "вул. Калинова дали")
        self.assertEqual(none["groups"], ["16.1"])


class RepeatOnTests(DbCase):
    async def test_second_restoring_post_is_silent_until_new_outage(self):
        G.register(NEW_QUEUES)
        await db.set_user(1, grp="14.1", provider="dtek")

        async def post(key, kind, text):
            await db.add_alert(key, kind, "14.1", None if kind == "on" else "18:00", text)
            await db._exec("UPDATE alerts SET ts=? WHERE key=?", (int(time.time()) - 600, key))  # без 5 хв очікування
            bot = AsyncMock()
            await notifier.send_alerts(bot)
            return bot.send_message.await_count

        self.assertEqual(await post("on1", "on", "✅ Група 14.1: триває відновлення"), 1)
        self.assertEqual(await post("on2", "on", "✅ Група 14.1: триває відновлення"), 0)  # повтор
        self.assertEqual(await post("off1", "off", "🚨 <b>Аварійне відключення</b> · група 14.1"), 1)
        self.assertEqual(await post("on3", "on", "✅ Група 14.1: триває відновлення"), 1)  # після нового — знову


class RateLimitTests(DbCase):
    async def test_six_per_hour_then_summary(self):
        await db.set_user(1, grp="14.1", provider="dtek")
        bot = AsyncMock()
        for i in range(9):
            await notifier._send(bot, 1, f"оновлення {i}")
        await notifier._send(bot, 1, "🏠 у будинку нема світла", urgent=True)  # про будинок — завжди
        texts = [c.args[1] for c in bot.send_message.await_args_list]
        self.assertEqual(len(texts), 7)
        self.assertEqual(texts[-1], "🏠 у будинку нема світла")
        notifier._rate[1] = [t - 3700 for t in notifier._rate[1]]  # минула година
        await notifier._send(bot, 1, "нове")
        self.assertIn("Ще 3 оновлення за останню годину не надсилав", bot.send_message.await_args.args[1])


class OutageHoursTests(DbCase):
    async def test_hours_today_and_week(self):
        await db.set_user(1, grp="14.1", provider="dtek", street="вул. Микитинська", house="14")
        now = int(time.time())
        midnight = int(datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        start = max(midnight, now - 2 * 3600)
        await db.kv_set("house_log:1", json.dumps([[now - 3 * 86400, now - 3 * 86400 + 5 * 3600], [start, None]]))
        await db.kv_set("house_seen:1", json.dumps({"kind": "emergency", "reason": "екстрене відключення",
                                                    "end": "", "ts": now}))
        block = await status.house_block(await db.get_user(1))
        self.assertIn("⏳ Без світла: сьогодні", block)
        self.assertIn("за 7 днів", block)
        await dtek_site.log_outage(1, False)  # світло дали — проміжок закрито
        log_ = json.loads(await db.kv_get("house_log:1"))
        self.assertIsNotNone(log_[-1][1])
        await dtek_site.log_outage(1, True, "")  # знову вимкнули — новий проміжок
        self.assertIsNone(json.loads(await db.kv_get("house_log:1"))[-1][1])


class AddressSearchTests(DbCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        streets = {"вул. Яворницького": [("2", "17.1")], "просп. Яворницького Дмитра": [("50", "18.1"), ("10", "17.1")],
                   "вул. Космічна": [("9", "17.1")], "просп. Героїв": [("20", "17.1")], "вул. Героїв Дніпра": [("20", "18.1")],
                   "просп. Мазепи Івана": [("20", "15.1")], "вул. Титова": [("5", "16.1")], "вул. Тилова": [("30", "16.1")],
                   "просп. Олександра Поля": [("50", "15.1")], "вул. Старокозацька": [("30", "15.1")],
                   "просп. Мануйлівський": [("60", "16.1")]}
        await db.upsert_streets(list(streets))
        for st, houses in streets.items():
            await db.save_houses(st, [(n, [g], "") for n, g in houses])

    async def found(self, street, house):
        r = await dtek_site.lookup_cached(street, house)
        return (r.street, r.house.num if r.house else None) if r else None

    async def test_russian_old_names_and_house_choice(self):
        self.assertEqual(await self.found("Космическая", "9"), ("вул. Космічна", "9"))
        self.assertEqual(await self.found("Мазепы", "20"), ("просп. Мазепи Івана", "20"))
        self.assertEqual(await self.found("Героев", "20"), ("просп. Героїв", "20"))  # не «Героїв Дніпра»
        self.assertEqual(await self.found("Карла Маркса", "10"), ("просп. Яворницького Дмитра", "10"))
        self.assertEqual(await self.found("Кірова", "50"), ("просп. Олександра Поля", "50"))
        self.assertEqual(await self.found("Комсомольская", "30"), ("вул. Старокозацька", "30"))
        self.assertEqual(await self.found("Калинина", "60"), ("просп. Мануйлівський", "60"))
        # «Яворницького 50» — і вулиця, і проспект; будинок 50 лише на проспекті
        self.assertEqual(await self.found("Яворницького", "50"), ("просп. Яворницького Дмитра", "50"))
        self.assertEqual(await self.found("просп. Яворницького", "50"), ("просп. Яворницького Дмитра", "50"))
        # є точна «вул. Титова» — схожу «вул. Тилова» (де будинок 30 є) не підставляємо
        self.assertEqual(await self.found("Титова", "30"), ("вул. Титова", None))

    async def test_failed_search_is_logged_for_db(self):
        import directory_refresh as dr
        await db.set_user(1)
        state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
        await state.set_state(handlers.Addr.waiting)
        with patch.object(dtek_site, "site_blocked", AsyncMock(return_value=True)):
            await handlers.addr_text(_Msg("Космическая 99"), state)
        fails = await db.addr_fails()
        self.assertEqual((fails[-1]["q"], fails[-1]["house"], fails[-1]["why"]), ("Космическая", "99", "house"))
        self.assertIn("«Космическая 99» — будинку немає на вул. Космічна", await dr.report())


class Group181ScenarioTests(DbCase):
    """Скрин 28.11: графік 04:00–10:30, о 09:24 канал «світла ще немає» (без часу), зараз 11:51;
    сайт: екстрене на 9 з 63 адрес до 12:30. Має бути «світло має бути» + рядок про частину адрес."""

    async def test_stale_channel_post_and_partial_site(self):
        G.register(NEW_QUEUES)
        now = datetime.now(TZ).replace(hour=11, minute=51, second=0, microsecond=0)
        posted = now.replace(hour=9, minute=24)
        await db.add_alert("ch-181", "off", "18.1", None,
                           "🚨 <b>Світла ще немає, відновлення очікується</b> · група <b>18.1</b>")
        await db._exec("UPDATE alerts SET ts=? WHERE key='ch-181'", (int(posted.timestamp()),))
        await db.kv_set("site_grp:18.1", json.dumps({"total": 63, "off": 9, "end": now.strftime("12:30 %d.%m.%Y"),
                                                     "ts": int(now.timestamp()), "streets": ["вул. 20-річчя Перемоги"],
                                                     "n_streets": 1}))
        sch = {"today": {"18.1": [Slot(0, 1.5), Slot(4, 10.5), Slot(13, 19.5), Slot(22, 24)]}}
        with patch.object(status, "get_schedule", AsyncMock(return_value=sch)), \
                patch.object(status, "now_h", return_value=11 + 51 / 60), \
                patch.object(status, "datetime", wraps=datetime) as dt:
            dt.now.return_value = now
            dt.fromtimestamp.side_effect = datetime.fromtimestamp
            text = await status.verdict("18.1", "dtek")
        self.assertIn("🟢 <b>Світло є</b>", text)
        self.assertIn("відключення скінчилось о 10:30", text)  # пост о 09:24 був про слот 04:00–10:30
        self.assertNotIn("Світла немає", text)
        self.assertIn("🚧 Частина адрес групи без світла (сайт ДТЕК): екстрене у 9 з 63", text)
        self.assertIn("до <b>12:30</b> · вул. 20-річчя Перемоги", text)
        self.assertIn("далі <b>13:00–19:30</b>", text)


class WeakSensorsTests(DbCase):
    async def test_one_sensor_shows_honest_state_without_push(self):
        import svitlobot as svb
        G.register(NEW_QUEUES)
        await db.kv_set("svb:dtek:18.1", json.dumps({"state": "off", "on": 0, "off": 1, "ts": 1, "since": 1}))
        pt = svb.Point(id="1", status="on", changed=None, address="Дніпро -> x 1", slug="s1", subscribers=1,
                       provider="dtek", grp="18.1")
        with patch.object(svb, "_initialized", True):
            changes = await svb.apply([pt])
        self.assertEqual(changes, [])  # одна точка — групі не пушимо
        st = await svb.group_state("dtek", "18.1")
        self.assertEqual((st["state"], st["weak"]), ("on", True))  # не застрягло на «нема»
        self.assertIn("🟢 є світло (1/1 точка — мало датчиків, лише орієнтир)", svb.status_line(st))
        self.assertFalse(await status.sensors_say_on("18.1", "dtek"))  # для рішень про групу — ні


class HouseScheduleUntilTests(DbCase):
    async def test_schedule_outage_shows_end_time(self):
        await db.set_user(1, grp="14.1", provider="dtek", street="вул. Микитинська", house="14")
        today = datetime.now(TZ).strftime("%d.%m.%Y")
        await db.kv_set("house_seen:1", json.dumps({"kind": "schedule", "reason": "відключення за графіком",
                                                    "start": f"08:40 {today}", "end": f"13:30 {today}",
                                                    "ts": int(time.time())}))
        self.assertIn("🔴 відключення за графіком до <b>13:30</b>", await status.house_block(await db.get_user(1)))
