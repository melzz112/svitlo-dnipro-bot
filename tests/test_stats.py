"""/stats: лише для адміна (ADMIN_IDS), решті — тиша. Лічильники, активність, зміст звіту.
Запуск: PYTHONPATH=. python -m unittest discover -s tests -p "test_stats.py" """
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, Message, User

import db
import handlers
import notifier
import stats
from providers import DaySchedule

ADMIN = 779043969
STRANGER = 111222333


def message(uid: int, text: str) -> Message:
    return Message(message_id=1, date=datetime.now(), chat=Chat(id=uid, type="private"),
                   from_user=User(id=uid, is_bot=False, first_name="x"), text=text)


def callback(uid: int, data: str) -> CallbackQuery:
    return CallbackQuery(id="1", from_user=User(id=uid, is_bot=False, first_name="x"), chat_instance="1", data=data)


async def runs(router_observer, handler_name: str, event, bot) -> bool:
    """Чи спрацює саме цей хендлер на подію (з усіма фільтрами)."""
    h = next(h for h in router_observer.handlers if h.callback.__name__ == handler_name)
    ok, _ = await h.check(event, bot=bot)
    return ok


class AccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_admin_passes_the_filter(self):
        bot = Bot("123:abc")
        # реальний хендлер збирається з ADMIN_IDS при імпорті; без ADMIN_IDS у середовищі список порожній ->
        # /stats не дозволено нікому (безпечне значення за замовчуванням)
        self.assertEqual(await runs(handlers.router.message, "stats_cmd", message(STRANGER, "/stats"), bot), False)
        await bot.session.close()

    async def test_filter_with_admin_configured(self):
        """Збираємо той самий фільтр з ADMIN_IDS=[ADMIN] і перевіряємо адміна та чужого."""
        from aiogram import F, Router
        from aiogram.filters import Command

        r = Router()

        @r.message(Command("stats"), F.from_user.id.in_([ADMIN]))
        async def stats_cmd(m): ...

        @r.callback_query(F.data == "adm:stats", F.from_user.id.in_([ADMIN]))
        async def stats_refresh(cb): ...

        bot = Bot("123:abc")
        self.assertTrue(await runs(r.message, "stats_cmd", message(ADMIN, "/stats"), bot))
        self.assertFalse(await runs(r.message, "stats_cmd", message(STRANGER, "/stats"), bot))
        self.assertFalse(await runs(r.message, "stats_cmd", message(ADMIN, "/menu"), bot))
        self.assertTrue(await runs(r.callback_query, "stats_refresh", callback(ADMIN, "adm:stats"), bot))
        self.assertFalse(await runs(r.callback_query, "stats_refresh", callback(STRANGER, "adm:stats"), bot))
        await bot.session.close()

    async def test_stranger_gets_no_reply(self):
        m = message(STRANGER, "/stats")
        with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
            await handlers.stats_denied(m)
        answer.assert_not_awaited()

    def test_admin_ids_parse(self):
        import config

        self.assertEqual(config.ADMIN_IDS.__class__, list)


class DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", str(Path(self.tmp.name) / "t.db"))
        self.db_patch.start()
        stats_db = patch.object(stats, "DB_PATH", str(Path(self.tmp.name) / "t.db"))
        stats_db.start()
        self.addCleanup(stats_db.stop)
        await db.init()
        db._seen_cache.clear()

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()


class TrackingTests(DbCase):
    async def test_created_and_seen(self):
        await db.get_user(1)
        u = await db.get_user(1)
        self.assertAlmostEqual(u["created"], time.time(), delta=5)
        await db._exec("UPDATE users SET seen=0 WHERE id=1")
        await db.touch_user(1)
        self.assertAlmostEqual((await db.get_user(1))["seen"], time.time(), delta=5)

    async def test_touch_is_throttled(self):
        await db.touch_user(2)
        await db._exec("UPDATE users SET seen=5 WHERE id=2")
        await db.touch_user(2)  # у межах хвилини — БД не чіпаємо
        self.assertEqual((await db.get_user(2))["seen"], 5)

    async def test_push_counters(self):
        bot = AsyncMock()
        await notifier._send(bot, 1, "hi")
        await notifier._send(bot, 1, "hi")
        bot.send_message.side_effect = RuntimeError("boom")
        await notifier._send(bot, 1, "hi")
        self.assertEqual((await stats._counter("push"), await stats._counter("push_fail")), (2, 1))


class ReportTests(DbCase):
    async def test_report_contents(self):
        await db.set_user(1, grp="1.2", street="вул. А", house="1")
        await db.set_user(2, grp="3.1", provider="cek", notify_on=0)
        await db.set_user(3)  # без групи
        await db._exec("UPDATE users SET created=?, seen=? WHERE id=3", (int(time.time()) - 3 * 86400, int(time.time()) - 3 * 86400))
        await db.add_chat_msg("Днепр свет/вода", "1.2 дали", None, "on", ["1.2"], who="a")
        await db.add_alert("k1", "off", "1.2", "18:05 20.11", "x", "dtek", "dteksite")
        await db.bump("push", 7)
        await db.kv_set("site_probe", '{"ok": true, "ts": %d, "houses": 1720}' % int(time.time()))
        sch = {d: DaySchedule({"1.2": []}, "yasno", "20.11.2026 07:59") for d in ("today", "tomorrow")}
        with patch("providers.get_schedule_days", AsyncMock(return_value=sch)):
            text = await stats.build()
        for part in ("Усього: <b>3</b>", "з групою: 2", "зі сповіщеннями: 1", "з адресою: 1", "ДТЕК 2 · ЦЕК 1",
                     "1.2 — 1", "Нових: за добу 2, за тиждень 3", "Сьогодні: 7", "Днепр свет/вода: 1 повід. від 1 людей",
                     "сайт ДТЕК dtek off — 1", "1720 будинків", "Графік ДТЕК: YASNO", "Графік ЦЕК: YASNO", "Диск:"):
            self.assertIn(part, text)

    async def test_works_on_empty_db(self):
        with patch("providers.get_schedule_days", AsyncMock(side_effect=ValueError("down"))):
            text = await stats.build()
        self.assertIn("Усього: <b>0</b>", text)
        self.assertIn("Повідомлень з чатів немає", text)
        self.assertIn("не вдалось дізнатись", text)


if __name__ == "__main__":
    unittest.main()
