import asyncio
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

import db
import notifier
import providers
from providers import DaySchedule, Slot, get_schedule_days, parse_yasno_schedule

TODAY = date(2026, 11, 20)
TOMORROW = date(2026, 11, 21)


def day_data(day, status, *slots):
    return {"date": f"{day}T00:00:00+02:00", "status": status, "slots": slots}


class YasnoScheduleTests(unittest.TestCase):
    def test_parses_minutes_and_distinguishes_unpublished_from_no_outages(self):
        data = {
            "1.1": {
                "today": day_data(
                    TODAY, "ScheduleApplies",
                    {"start": 0, "end": 570, "type": "NotPlanned"},
                    {"start": 570, "end": 780, "type": "Definite"},
                    {"start": 780, "end": 1440, "type": "Possible"},
                ),
                "tomorrow": day_data(TOMORROW, "WaitingForSchedule"),
                "updatedOn": "2026-11-20T06:14:11+00:00",
            },
            "1.2": {"today": day_data(TODAY, "NoOutages")},
            "2.1": {"today": day_data(date(2026, 11, 19), "NoOutages")},
        }
        result = parse_yasno_schedule(data, TODAY)
        self.assertEqual(result["today"].groups, {"1.1": [Slot(9.5, 13), Slot(13, 24, "maybe")], "1.2": []})
        self.assertIn("20.11.2026 08:14", result["today"].label())
        self.assertNotIn("tomorrow", result)

    def test_emergency_is_not_reported_as_no_outages(self):
        result = parse_yasno_schedule({"1.1": {"today": day_data(TODAY, "EmergencyShutdowns")}}, TODAY)
        self.assertTrue(result["today"].emergency)
        self.assertFalse(result["today"].groups)


class ScheduleSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_separate_yasno_schedules_and_shares_inflight_requests(self):
        requests = []

        def respond(request):
            requests.append(request.url.path)
            start = 450 if "/301/" in request.url.path else 570
            return httpx.Response(200, json={"1.1": {"today": day_data(
                TODAY, "ScheduleApplies", {"start": start, "end": 780, "type": "Definite"},
            )}})

        async_client = httpx.AsyncClient
        with (
            patch.multiple(providers, SOURCE="yasno", CITY="dnipro", _site_cache={}, _site_locks={}),
            patch.object(providers.httpx, "AsyncClient", side_effect=lambda **kwargs: async_client(
                transport=httpx.MockTransport(respond), **kwargs,
            )),
            patch.object(providers, "channel_schedule", AsyncMock(return_value=DaySchedule())),
        ):
            dtek, cek, cek_again = await asyncio.gather(
                get_schedule_days("dtek", today=TODAY),
                get_schedule_days("cek", today=TODAY),
                get_schedule_days("cek", today=TODAY),
            )
        self.assertEqual(dtek["today"].groups["1.1"], [Slot(7.5, 13)])
        self.assertEqual(cek["today"].groups["1.1"], [Slot(9.5, 13)])
        self.assertEqual(cek, cek_again)
        self.assertCountEqual(requests, [
            "/api/blackout-service/public/shutdowns/regions/3/dsos/301/planned-outages",
            "/api/blackout-service/public/shutdowns/regions/3/dsos/303/planned-outages",
        ])

    async def test_dtek_block_page_uses_yasno_without_reading_telegram(self):
        def respond(request):
            if request.url.host == "www.dtek-dnem.com.ua":
                return httpx.Response(200, text='<iframe src="/_Incapsula_Resource"></iframe>')
            return httpx.Response(200, json={"1.1": {
                "today": day_data(TODAY, "NoOutages"),
                "tomorrow": day_data(TOMORROW, "NoOutages"),
            }})

        async_client = httpx.AsyncClient
        with (
            patch.multiple(providers, SOURCE="dtek", CITY="dnipro", _site_cache={}, _site_locks={}),
            patch.object(providers.httpx, "AsyncClient", side_effect=lambda **kwargs: async_client(
                transport=httpx.MockTransport(respond), **kwargs,
            )),
            patch.object(providers, "channel_schedule", AsyncMock()) as channel,
            self.assertLogs("providers", "WARNING"),
        ):
            result = await get_schedule_days("dtek", today=TODAY)
        self.assertEqual(result["today"].source, "yasno")
        self.assertEqual(result["today"].groups, {"1.1": []})
        channel.assert_not_called()

    async def test_falls_back_per_day_and_labels_the_actual_source(self):
        direct = DaySchedule({"1.1": [Slot(8, 10)]}, "dtek")
        tomorrow = DaySchedule({"1.1": [Slot(9, 11)]}, "yasno")
        channel = AsyncMock(return_value=DaySchedule({"1.1": [Slot(12, 13)]}))
        with (
            patch.multiple(providers, SOURCE="dtek", _site_cache={}, _site_locks={}),
            patch.object(providers, "fetch_dtek", AsyncMock(return_value={"today": direct})),
            patch.object(providers, "fetch_yasno", AsyncMock(return_value={"tomorrow": tomorrow})) as yasno,
            patch.object(providers, "channel_schedule", channel),
        ):
            result = await get_schedule_days("dtek", today=TODAY)
            self.assertIn("сайт ДТЕК", result["today"].label())
            self.assertIn("сайт YASNO", result["tomorrow"].label())
            channel.assert_not_called()

            yasno.side_effect = httpx.ConnectError("unavailable")
            with self.assertLogs("providers", "WARNING"):
                result = await get_schedule_days("cek", today=TODAY)
            channel.assert_any_await("cek", TODAY)
            self.assertIn("Telegram", result["today"].label())

    async def test_pushes_schedule_to_matching_operator_without_duplicates(self):
        async def fetch(provider, *, today):
            start = 10 if provider == "dtek" else 12
            return {
                "today": DaySchedule({"1.1": [Slot(start, start + 2)]}, "yasno"),
                "tomorrow": DaySchedule(),
            }

        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(db, "DB_PATH", str(Path(temp) / "test.db")),
            patch.object(providers, "get_schedule_days", fetch),
            patch.object(notifier, "now_h", return_value=9.75),
        ):
            await db.init()
            await db.set_user(1, grp="1.1", provider="dtek")
            await db.set_user(2, grp="1.1", provider="cek")
            await db.kv_set("sched_init", "1")
            bot = AsyncMock()
            await notifier.tick(bot)
            await notifier.tick(bot)

        messages = [(call.args[0], call.args[1]) for call in bot.send_message.await_args_list]
        self.assertEqual(len(messages), 3)
        self.assertTrue(any(uid == 1 and "ДТЕК" in text and "YASNO" in text for uid, text in messages))
        self.assertTrue(any(uid == 2 and "ЦЕК" in text and "YASNO" in text for uid, text in messages))
        self.assertTrue(any(uid == 1 and "Через 15 хв" in text for uid, text in messages))
