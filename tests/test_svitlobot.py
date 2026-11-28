"""Світлобот: парсинг рядків, групи, гістерезис, перший прохід без пушів.
Запуск: PYTHONPATH=. python -m unittest discover -s tests -p "test_svitlobot.py"
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import db
import notifier
import status
import svitlobot as svb
from svitlobot import SEP, GroupStat


def row(i, code, addr="Дніпро -> Калинова 82", extra="Група 1.2 - Дніпро - ДТЕК"):
    return SEP.join([str(i), str(code), "2026-11-26T10:00:00Z", addr, f"svitlobot_x{i}", "12", extra])


class ParseTests(unittest.TestCase):
    def test_token_formats(self):
        self.assertEqual(svb.parse_token(' "abc123" \n'), "abc123")
        self.assertEqual(svb.parse_token('{"token": "xyz"}'), "xyz")
        self.assertEqual(svb.parse_token('{"t": "q1"}'), "q1")

    def test_row(self):
        p = svb.parse_point(row(7, 1))
        self.assertEqual((p.id, p.status, p.slug, p.subscribers), ("7", "on", "svitlobot_x7", 12))
        self.assertEqual((p.provider, p.grp), ("dtek", "1.2"))
        self.assertEqual(p.address, "Дніпро -> Калинова 82")

    def test_operator_and_group(self):
        self.assertEqual(svb.extract_group("Група 3.1 - Дніпро - ЦЕК"), ("cek", "3.1"))
        self.assertEqual(svb.extract_group("черга 6.2 ДТЭК"), ("dtek", "6.2"))
        self.assertEqual(svb.extract_group("Дніпро 4.1"), ("dtek", "4.1"))  # оператора нема — ДТЕК
        self.assertIsNone(svb.extract_group("Група 9.9"))  # такої групи нема
        self.assertIsNone(svb.extract_group("v 2.15.3"))
        # групи нема в додаткових полях — шукаємо в адресі
        p = svb.parse_point(row(1, 2, addr="Дніпро -> Робоча 5 (гр. 2.1)", extra=""))
        self.assertEqual((p.provider, p.grp, p.status), ("dtek", "2.1", "off"))

    def test_city_filter(self):
        text = "\n".join([row(1, 1), row(2, 1, addr="Київ -> Хрещатик 1"), "", "битий рядок", row(3, 2, addr="ДНІПРО -> Поля 5")])
        self.assertEqual([p.id for p in svb.parse_channels(text, "Дніпро")], ["1", "3"])

    def test_city_not_in_street(self):
        text = "\n".join([
            row(1, 1, addr="Київ -> Дніпровська набережна 3"), row(2, 1, addr="Павлоград -> Дніпровська 18"),
            row(3, 1, addr="Проців -> СТ Дніпро-86"), row(4, 1, addr="М. Дніпро -> Робоча 5"),
            row(5, 1, addr="Дніпро, жм Покровський, 3п. -> 1"), row(6, 1, addr="Дніпропетровськ -> Миру 1"),
        ])
        self.assertEqual([p.id for p in svb.parse_channels(text, "Дніпро")], ["4", "5"])

    def test_groups_map_fills_missing(self):
        text = "\n".join([row(1, 1, extra="-"), row(2, 2, extra="-"), row(3, 1)])
        pts = svb.parse_channels(text, "Дніпро", mapping={"svitlobot_x1": ("cek", "4.1"), "svitlobot_x3": ("cek", "6.2")})
        # без групи (x2) — відкинуто; підпис у даних важливіший за файл (x3)
        self.assertEqual([(p.slug, p.provider, p.grp) for p in pts], [("svitlobot_x1", "cek", "4.1"), ("svitlobot_x3", "dtek", "1.2")])

    def test_region_label(self):
        real = svb.parse_point(row(1, 2, extra="Дніпро (ЦЕК) — група 1.1"))
        self.assertEqual((real.provider, real.grp), ("cek", "1.1"))
        latin_i = svb.parse_point(row(2, 1, extra="Днiпро (ДТЕК) — група 5.1"))
        self.assertEqual((latin_i.provider, latin_i.grp), ("dtek", "5.1"))
        # ті самі номери черг в інших областях — не наші, навіть якщо точка в Дніпрі
        for other in ("Група 1.1", "Київська обл. — група 2.1", "Одеса та обл. — група 3.1"):
            self.assertIsNone(svb.parse_point(row(3, 1, extra=other)).grp, other)
        self.assertEqual(svb.parse_channels(row(4, 1, extra="Група 1.1"), "Дніпро"), [])

    def test_groups_map_file(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "m.json"
            path.write_text(json.dumps({"a": {"provider": "dtek", "grp": "2.1"}, "b": {"provider": None, "grp": None},
                                        "c": {"provider": "dtek", "grp": "9.9"}}), encoding="utf-8")
            with patch.object(svb, "MAP_PATH", path), patch.object(svb, "_map_cache", (0.0, {})):
                self.assertEqual(svb.groups_map(), {"a": ("dtek", "2.1")})

    def test_aggregate_skips_maintenance(self):
        pts = svb.parse_channels("\n".join([row(1, 1), row(2, 1), row(3, 2), row(4, 3)]), "Дніпро")
        st = svb.aggregate(pts)[("dtek", "1.2")]
        self.assertEqual((st.on, st.off, st.maint, st.alive), (2, 1, 1, 3))


class DecideTests(unittest.TestCase):
    def d(self, on, off, prev):
        return svb.decide(GroupStat("dtek", "1.2", on, off), prev, 2, 0.65, 0.35)

    def test_thresholds(self):
        self.assertEqual(self.d(14, 2, None), "on")
        self.assertEqual(self.d(2, 14, "on"), "off")
        self.assertEqual(self.d(8, 8, "off"), "off")  # гістерезис
        self.assertEqual(self.d(8, 8, "on"), "on")
        self.assertIsNone(self.d(8, 8, None))
        self.assertEqual(self.d(1, 0, "off"), "off")  # замало точок


class ApplyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", str(Path(self.tmp.name) / "t.db"))
        self.db_patch.start()
        await db.init()
        svb._initialized = False

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    def pts(self, on, off, extra="Група 1.2 - Дніпро - ДТЕК"):
        rows = [row(i, 1, extra=extra) for i in range(on)] + [row(100 + i, 2, extra=extra) for i in range(off)]
        return svb.parse_channels("\n".join(rows), "Дніпро")

    async def test_baseline_then_change(self):
        self.assertEqual(await svb.apply(self.pts(14, 2)), [])  # перший прохід — без пушів
        self.assertEqual(await svb.apply(self.pts(9, 7)), [])  # між порогами — лишається «on»
        self.assertEqual(json.loads(await db.kv_get("svb:dtek:1.2"))["state"], "on")
        ch = await svb.apply(self.pts(2, 14))
        self.assertEqual([(c.provider, c.grp, c.state) for c in ch], [("dtek", "1.2", "off")])
        self.assertIn("світло зникло", svb.notify_text(ch[0], "ДТЕК"))
        ch = await svb.apply(self.pts(14, 2))
        self.assertEqual(svb.notify_text(ch[0], "ДТЕК").splitlines(),
                         ["💡 <b>ДТЕК · група 1.2: світло з'явилось</b>", "📡 За датчиками: 14 з 16 точок зі світлом"])

    async def test_cek_separate(self):
        await svb.apply(self.pts(10, 0) + self.pts(0, 10, "Дніпро (ЦЕК) — група 1.2"))
        self.assertEqual(svb.status_line(await svb.group_state("dtek", "1.2")), "📡 Датчики: 🟢 10 з 10 зі світлом")
        self.assertEqual(svb.status_line(await svb.group_state("cek", "1.2")), "📡 Датчики: 🔴 0 з 10 зі світлом")

    async def test_notifier_push_and_failure(self):
        await db.set_user(1, grp="1.2", provider="dtek")
        await db.set_user(2, grp="1.2", provider="cek")
        await db.set_user(3, grp="2.1", provider="dtek")
        bot = AsyncMock()
        with patch.object(notifier, "SVITLOBOT_ENABLED", True), patch.object(notifier, "_send", AsyncMock()) as send:
            for pts in (self.pts(14, 2), self.pts(1, 15)):
                notifier._svb_next = 0
                with patch.object(svb, "fetch_points", AsyncMock(return_value=pts)):
                    await notifier.svitlobot_check(bot)
            self.assertEqual([c.args[1] for c in send.call_args_list], [1])
            self.assertIn("світло зникло", send.call_args.args[2])
            notifier._svb_next = 0
            with patch.object(svb, "fetch_points", AsyncMock(side_effect=svb.SourceError("429", 300))):
                await notifier.svitlobot_check(bot)  # не падає, лише відкладає
            self.assertGreater(notifier._svb_next, 0)

    async def test_verdict_shows_sensors(self):
        await svb.apply(self.pts(14, 2))
        with patch.object(status, "get_schedule", AsyncMock(return_value={"today": {"1.2": []}})):
            text = await status.verdict("1.2", "dtek")
        self.assertIn("📡 Датчики: 🟢 14 з 16 зі світлом", text)


class FetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_errors_are_soft(self):
        from aiohttp import web
        from aiohttp.test_utils import TestServer

        async def token(_):
            return web.Response(text='"tok"')

        async def channels(req):
            if req.query.get("t") != "tok":
                return web.Response(status=403)
            return web.Response(text=row(1, 1) + "\n" + row(2, 2))

        async def limited(_):
            return web.Response(status=429, headers={"Retry-After": "90"})

        app = web.Application()
        app.add_routes([web.get("/token", token), web.get("/ch", channels), web.get("/lim", limited)])
        async with TestServer(app) as srv:
            base = str(srv.make_url("/"))
            with patch.object(svb, "SVITLOBOT_TOKEN_URL", base + "token"), patch.object(svb, "SVITLOBOT_CHANNELS_URL", base + "ch"):
                pts = await svb.fetch_points()
            self.assertEqual([p.status for p in pts], ["on", "off"])
            with patch.object(svb, "SVITLOBOT_TOKEN_URL", base + "lim"), patch.object(svb, "SVITLOBOT_CHANNELS_URL", base + "ch"):
                with self.assertRaises(svb.SourceError) as e:
                    await svb.fetch_points()
            self.assertEqual(e.exception.retry_after, 90)
            with patch.object(svb, "SVITLOBOT_TOKEN_URL", "http://127.0.0.1:1/x"), patch.object(svb, "SVITLOBOT_CHANNELS_URL", base + "ch"):
                with self.assertRaises(svb.SourceError):
                    await svb.fetch_points()


if __name__ == "__main__":
    unittest.main()
