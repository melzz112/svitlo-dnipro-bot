"""Спам у чатах (емодзі-флуд, повтори) і адреса, коли сайт ДТЕК закрився антиботом.
Запуск: PYTHONPATH=. python -m unittest discover -s tests -p "test_chat_addr.py"
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import db
import dtek_site
import notifier
from dtek_site import House, Probe
from parse import chat_key, clean_chat_text

SPAM = "🛑🛑🛑🛑🛑🛑🛑🛑🛑🛑🛑🛑🛑🛑🛑🛑1.2 Анд Брылив шлях, пошли 4е сутки без света, не включали и на час нас 🛑🛑🛑🛑"


class CleanTests(unittest.TestCase):
    def test_emoji_flood(self):
        self.assertEqual(clean_chat_text(SPAM), "🛑 1.2 Анд Брылив шлях, пошли 4е сутки без света, не включали и на час нас 🛑")
        self.assertEqual(clean_chat_text("‼️‼️‼️‼️ група 1.2, світла немає‼️‼️‼️‼️"), "‼️ група 1.2, світла немає‼️")
        self.assertEqual(clean_chat_text("1.2 нема!!!!!!!"), "1.2 нема!!!")
        self.assertEqual(clean_chat_text("🔴 1.2 нема 🙏"), "🔴 1.2 нема 🙏")  # поодинокі емодзі лишаються
        self.assertEqual(clean_chat_text("1.2 дали) ура)))"), "1.2 дали) ура)))")

    def test_links_removed(self):
        from parse import parse_channel
        post = ("📢 Екстрені відключення на окремих адресах районів продовжили. Орієнтовні строки завершення:\n"
                "• Амур-Нижньодніпровський — для різних адрес до 15:53 або 17:20\n"
                "• Індустріальний, Чечелівський і Шевченківський — до 17:20\n"
                "Графік відключень - https://t.me/+9fuAjv24ktgxM2My\nПідписатись: @vklyuchatel_dnepr_svet")
        [a] = parse_channel(post)
        self.assertTrue(a.text.startswith("📢 Екстрені"))
        self.assertNotIn("📢 📢", a.text)
        for junk in ("http", "t.me", "@", "Графік відключень", "Підписатись"):
            self.assertNotIn(junk, a.text)
        self.assertIn("завершення:", a.text)
        self.assertEqual(clean_chat_text("1.2 дали https://t.me/x"), "1.2 дали")

    def test_key_ignores_emoji(self):
        self.assertEqual(chat_key(SPAM), chat_key(clean_chat_text(SPAM)))
        self.assertNotEqual(chat_key("1.2 нема"), chat_key("1.2 дали"))


class DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", str(Path(self.tmp.name) / "t.db"))
        self.db_patch.start()
        await db.init()

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()


class ChatTests(DbCase):
    async def test_repeat_not_stored(self):
        self.assertTrue(await db.add_chat_msg("chat", SPAM, None, "off", ["1.2"], who="a"))
        for _ in range(7):  # той самий текст ще 7 разів
            self.assertFalse(await db.add_chat_msg("chat", SPAM, None, "off", ["1.2"], who="a"))
        self.assertTrue(await db.add_chat_msg("chat", SPAM, None, "off", ["1.2"], who="b"))  # інша людина — рахуємо
        self.assertTrue(await db.add_chat_msg("chat", "1.2 досі нема", None, "off", ["1.2"], who="a"))  # новий текст
        rows = await db._all("SELECT text FROM chat_msgs")
        self.assertEqual(len(rows), 3)
        self.assertNotIn("🛑🛑", rows[0]["text"])

    async def test_old_duplicates_shown_once_and_counted_as_people(self):
        # те, що вже лежить у БД з повторами (до цього фіксу)
        now = int(time.time())
        for i in range(8):
            await db._exec("INSERT INTO chat_msgs(chat,text,district,event,ts,grp,street,who,provider) VALUES(?,?,?,?,?,?,?,?,?)",
                           ("c", SPAM, "АНД", "off", now - 60 - i * 60, "1.2", "", "a", ""))
        await db.add_chat_msg("chat", "1.2 дали", None, "on", ["1.2"], who="b")
        msgs = await db.recent_chat_msgs("1.2")
        self.assertEqual([m["who"] for m in msgs], ["b", "a"])
        self.assertEqual(await db.chat_stats(), {"1.2": {"off": 1, "on": 1}})


STREETS = ["вул. Березинська", "просп. Дмитра Яворницького"]


class AddressTests(DbCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await db.kv_set("dtek_streets", json.dumps(STREETS, ensure_ascii=False))

    async def test_cache_filled_by_site_and_used_when_blocked(self):
        self.assertIsNone(await dtek_site.lookup_cached("березинська", "45"))
        await dtek_site._cache_houses("вул. Березинська", [House("вул. Березинська", "45", ["2.1"], None),
                                                           House("вул. Березинська", "47", [], None, cek="1")])
        res = await dtek_site.lookup_cached("березинська", "45")
        self.assertEqual((res.street, res.house.num, res.house.groups, res.house.kind), ("вул. Березинська", "45", ["2.1"], None))
        self.assertIsNone((await dtek_site.lookup_cached("Березинська", "99")).house)  # вулиця є, будинку нема
        self.assertTrue((await dtek_site.lookup_cached("Березинська", "47")).house.cek)

    async def test_house_without_group(self):
        """«вул. Березинська, 45» є на сайті ДТЕК, але без черги: адресу ставимо без групи, групу не вигадуємо."""
        await db.set_user(8, grp="1.2", addr_q="березинська|45")
        probe = Probe(houses=[House("вул. Березинська", "45", [], None), House("вул. Березинська", "43", ["2.1"], None)])
        [(u, street, h)] = dtek_site.resolve_pending(probe, await db.users_addr_pending(), {8: ["вул. Березинська"]})
        text = await notifier._finish_address(u, street, h)
        self.assertIn("черги немає", text)
        self.assertNotIn("2.1", text)
        u = await db.get_user(8)
        self.assertEqual((u["street"], u["house"], u["grp"], u["addr_q"]), ("вул. Березинська", "45", "1.2", ""))  # група людини не чіпається

    async def test_pending_resolved_on_next_probe(self):
        await db.set_user(5, addr_q="березинська|45")
        await db.set_user(6, addr_q="березинська|999")
        await db.set_user(7, addr_q="Яворницького|3")  # цієї вулиці сайт цього разу не дав — чекає далі
        probe = Probe(houses=[House("вул. Березинська", "45", ["2.1"], None), House("вул. Березинська", "43", ["2.1"], None)])
        bot = AsyncMock()
        with patch.object(notifier, "DTEK_PROBE_SEC", 180), patch.object(notifier, "SOURCE", "dtek"), \
                patch.object(notifier, "CITY", "dnipro"), patch.object(notifier, "_site_next", 0), \
                patch.object(dtek_site, "probe", AsyncMock(return_value=probe)) as pr, \
                patch.object(dtek_site, "apply", AsyncMock(return_value=[])), \
                patch.object(notifier, "_send", AsyncMock()) as send:
            await notifier.site_check(bot)
        self.assertEqual(pr.call_args.args[0][:2], ["вул. Березинська", "просп. Дмитра Яворницького"])  # pending — першими
        texts = {c.args[1]: c.args[2] for c in send.call_args_list}
        self.assertIn("група <b>2.1</b>", texts[5])
        self.assertIn("будинку <b>999</b>", texts[6])
        self.assertNotIn(7, texts)
        u5, u6, u7 = [await db.get_user(i) for i in (5, 6, 7)]
        self.assertEqual((u5["street"], u5["house"], u5["grp"], u5["addr_q"]), ("вул. Березинська", "45", "2.1", ""))
        self.assertEqual(u6["addr_q"], "")
        self.assertEqual(u7["addr_q"], "Яворницького|3")


if __name__ == "__main__":
    unittest.main()
