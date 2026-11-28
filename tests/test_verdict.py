"""«⚡ Зараз»: офіційний час головний, люди уточнюють. Запуск: PYTHONPATH=. python -m unittest discover -s tests -p "test_verdict.py"
- поки офіційно відключення — «світло є» не пишемо: 1–4 «дали» -> «🔴 … дехто пише, що дали», 5+ -> «🟡 частково»;
- після офіційного кінця — «🟢 світло є»; «нема» після кінця -> «⚠️ не всі зі світлом», 3+ і їх більше -> «🔴 локальна аварія»;
- пуш «люди пишуть: дали» під час офіційного відключення — лише від 5 людей, як «почали вмикати, поки не всім».
"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import db
import notifier
import status
from providers import Slot
from render import now_h


class VerdictCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if now_h() < 3:
            self.skipTest("тести рахують слоти відносно поточного часу — після 03:00")
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", str(Path(self.tmp.name) / "t.db"))
        self.db_patch.start()
        await db.init()
        await db.set_user(1, grp="1.2", provider="dtek")
        self.t = now_h()

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    async def say(self, event, n, prefix, ago_min=0):
        for i in range(n):
            await db.add_chat_msg("chat", f"1.2 {event}", None, event, ["1.2"], who=f"{prefix}{i}")
        if ago_min:
            await db._exec("UPDATE chat_msgs SET ts=ts-? WHERE who LIKE ?", (ago_min * 60, f"{prefix}%"))

    async def verdict(self, slots):
        with patch.object(status, "get_schedule", AsyncMock(return_value={"today": {"1.2": slots}})):
            return await status.verdict("1.2", "dtek")


class DuringOutageTests(VerdictCase):
    """Слот графіка йде зараз (почався годину тому, кінець за 30 хв)."""

    def slot(self):
        return [Slot(self.t - 1, self.t + 0.5)]

    async def test_screenshot_1634_then_1642(self):
        await self.say("off", 4, "off", ago_min=40)  # 16:34: нема — 4
        await self.say("on", 1, "on1")
        text = await self.verdict(self.slot())
        self.assertIn("🔴 <b>Світла немає</b>", text)
        self.assertIn("💡 1 · 🕯 4", text)
        await self.say("on", 2, "on2")  # 16:42: ще двоє «дали» — разом 3
        text = await self.verdict(self.slot())
        self.assertNotIn("🟢", text)  # раніше тут було «Світло є»
        self.assertIn("🕯 4", text)  # ранні «нема» не викидаємо
        self.assertIn("🔴 <b>Світла немає</b>", text)

    async def test_few_on_is_still_no_power(self):
        await self.say("on", 3, "on")
        text = await self.verdict(self.slot())
        self.assertIn("🔴 <b>Світла немає</b>", text)
        self.assertIn("дехто пише, що дали — можливо, почали вмикати", text)

    async def test_many_on_is_partial_not_green(self):
        await self.say("on", 6, "on", ago_min=10)  # за 30 хв, але не за 5 — «частково»
        await self.say("off", 2, "off", ago_min=10)
        text = await self.verdict(self.slot())
        self.assertIn("🟡 <b>Частково є світло</b>", text)
        self.assertNotIn("🟢", text)
        self.assertIn("📅 Графік: відключення до", text)

    async def test_fresh_wave_of_on_beats_old_off(self):
        """Скрін 11:24: «нема» з ночі + свіжа хвиля «дали» -> не «Світла немає», а «частково»."""
        slot = [Slot(self.t - 3, self.t + 2)]
        await self.say("off", 12, "night", ago_min=150)  # написали на початку відключення і мовчать
        await self.say("off", 4, "off", ago_min=10)
        await self.say("on", 12, "on", ago_min=7)
        text = await self.verdict(slot)
        self.assertIn("🟡 <b>Частково є світло</b>", text)
        self.assertIn("(30 хв: 💡 12 · 🕯 4)", text)
        self.assertNotIn("🟢", text)


class FastSwitchTests(VerdictCase):
    """≥5 людей за 5 хв пишуть одне й те саме — вердикт перемикається, хоч би що казав офіційний час."""

    async def test_five_on_in_five_minutes_is_green_during_outage(self):
        slot = [Slot(self.t - 1, self.t + 2)]
        await self.say("off", 3, "off", ago_min=30)
        await self.say("on", 4, "on", ago_min=2)
        self.assertNotIn("не в усіх", await self.verdict(slot))  # 4 — ще ні
        await self.say("on", 1, "late")
        text = await self.verdict(slot)
        self.assertIn("🟡 <b>Є світло, але не в усіх</b>", text)
        self.assertIn("за 5 хв 5 людей пишуть «дали»", text)
        self.assertIn("офіційно — відключення до", text)

    async def test_old_on_does_not_count(self):
        await self.say("on", 6, "on", ago_min=8)  # 8 хв тому — поза вікном 5 хв
        self.assertNotIn("не в усіх", await self.verdict([Slot(self.t - 1, self.t + 2)]))

    async def test_five_off_in_five_minutes_is_red_without_outage(self):
        await self.say("off", 5, "off", ago_min=1)
        await self.say("on", 2, "on", ago_min=1)
        text = await self.verdict([])
        self.assertIn("🔴 <b>Світла немає</b>", text)
        self.assertIn("🕯 5", text)


class AfterOutageTests(VerdictCase):
    """Слот скінчився 30 хв тому."""

    def slot(self):
        return [Slot(self.t - 3, self.t - 0.5)]

    async def test_green_after_official_end(self):
        await self.say("off", 4, "off", ago_min=60)  # писали під час відключення — вже не рахуються
        text = await self.verdict(self.slot())
        self.assertIn("🟢 <b>Світло є</b>", text)
        self.assertIn("відключення скінчилось о", text)
        self.assertNotIn("Не всі", text)

    async def test_someone_still_without_power(self):
        await self.say("on", 2, "on")
        await self.say("off", 1, "off", ago_min=5)  # після кінця
        text = await self.verdict(self.slot())
        self.assertIn("🟢 <b>Світло є</b>", text)
        self.assertIn("🕯 1", text)

    async def test_many_still_without_power_is_local_outage(self):
        await self.say("off", 3, "off", ago_min=5)
        text = await self.verdict(self.slot())
        self.assertIn("🔴 <b>Світла немає</b>", text)
        self.assertIn("локальна аварія", text)


class CrowdPushTests(VerdictCase):
    async def push(self, slots):
        bot = AsyncMock()
        with patch.object(notifier, "get_schedule", AsyncMock(return_value={"today": {"1.2": slots}})), \
                patch.object(status, "get_schedule", AsyncMock(return_value={"today": {"1.2": slots}})):
            await notifier.crowd_check(bot)
        return [c.args[1] for c in bot.send_message.await_args_list]

    async def test_three_on_during_outage_no_push_five_push_partial(self):
        slots = [Slot(self.t - 1, self.t + 0.5)]
        await self.say("on", 3, "a")
        self.assertEqual(await self.push(slots), [])
        await self.say("on", 2, "b")
        sent = await self.push(slots)
        self.assertEqual(len(sent), 1)
        self.assertIn("почали вмикати світло", sent[0])
        self.assertIn("Поки не всім — офіційно відключення до", sent[0])


if __name__ == "__main__":
    unittest.main()
