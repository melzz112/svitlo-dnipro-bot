"""Сайт ЦЕК, стрічка «Відключення»: графік ГПВ, «додатково відключаються черги», екстрені (ГАВ).
Розмітка — точна копія сторінки за 20.11.2026. Запуск: PYTHONPATH=. python -m unittest discover -s tests -p "test_cek_site.py"
"""
import unittest
from datetime import date

import cek_site

TODAY = date(2026, 11, 20)

HEAD = '<p style="text-align: center;">&nbsp;<strong>Шановні споживачі!</strong></p>\n'
EXTRA = (
    '<p>📢 Повідомляємо про зміни в ГПВ на 20 ЛИСТОПАДА, за розпорядженням диспетчерського центру НЕК "Укренерго" '
    'з 13:45 додатково</p>\n<p>відключаються черги: 5.1, 5.2;</p>\n<p>&nbsp;</p>\n'
)
SCHEDULE = (
    '<p>📢&nbsp; Повідомляємо про зміни у ГПВ&nbsp; 20 ЛИСТОПАДА за розпорядженням диспетчерського центру НЕК '
    '"Укренерго" з 14:00 до 24:00 діятимуть наступні черги:</p>\n<p>&nbsp;</p>\n'
    '<p>🔹 Черга 1.1<br />16:30-23:30</p>\n<p>🔹 Черга 1.2<br />16:30-23:30</p>\n'
    '<p>🔹 Черга 2.1<br />20:00-23:30</p>\n<p>🔹 Черга 2.2<br />20:00-23:30</p>\n'
    '<p>🔹 Черга 3.1<br />13:00-16:30<br />23:30-24:00</p>\n<p>🔹 Черга 3.2<br />13:00-16:30<br />23:30-24:00</p>\n'
    '<p>🔹 Черга 4.1<br />13:00-16:30<br />20:00-24:00</p>\n<p>🔹 Черга 4.2<br />13:00-16:30<br />20:00-24:00</p>\n'
    '<p>🔹 Черга 5.1<br />14:30-20:00</p>\n<p>🔹 Черга 5.2<br />14:30-20:00</p>\n'
    '<p>🔹 Черга 6.1<br />16:30-20:00<br />23:30-24:00</p>\n<p>🔹 Черга 6.2<br />16:30-20:00<br />23:30-24:00</p>\n'
)
TAIL = '<p>Попереджаємо, що протягом доби, за розпорядженням НЕК "Укренерго", графік може змінюватися‼️ </p>\n'
PAGE = HEAD + EXTRA + SCHEDULE + TAIL  # точно як на сайті

EMERGENCY_ON = '<p>📢 З 15:10 у м. ДНІПРО застосовуються ЕКСТРЕНІ ВІДКЛЮЧЕННЯ. Графіки погодинних відключень не діють.</p>\n'
EMERGENCY_OFF = '<p>📢 З 18:00 у м. ДНІПРО скасовуються ЕКСТРЕНІ ВІДКЛЮЧЕННЯ, застосовуються ГПВ.</p>\n'
TOMORROW = (
    '<p>📢 Графік на 21 ЛИСТОПАДА: з 00:00 до 24:00 застосовуватимуться відключення наступних черг:</p>\n'
    '<p>🔹 Черга 1.1<br />09:30-13:00</p>\n<p>🔹 Черга 1.2<br />20:00-23:30</p>\n<p>🔹 Черга 2.1<br />00:00-02:30</p>\n'
)


def h(hm: str) -> float:
    a, b = hm.split(":")
    return int(a) + int(b) / 60


class ParagraphTests(unittest.TestCase):
    def test_markup(self):
        ps = cek_site.paragraphs(PAGE)
        self.assertEqual(ps[0], "Шановні споживачі!")  # <strong>, &nbsp; і порожні <p>&nbsp;</p> прибрані
        self.assertIn("з 13:45 додатково", ps[1])
        self.assertEqual(ps[2], "відключаються черги: 5.1, 5.2;")
        self.assertIn("🔹 Черга 3.1\n13:00-16:30\n23:30-24:00", ps)  # <br /> -> новий рядок


class ScheduleTests(unittest.TestCase):
    """1) Звичайний графік ГПВ — найвищий блок на дату."""

    def test_plain_schedule(self):
        st = cek_site.parse_page(HEAD + SCHEDULE + TAIL, TODAY)
        self.assertFalse(st.emergency)
        day = st.days[TODAY]
        self.assertEqual(len(day.groups), 12)
        self.assertEqual(day.groups["1.1"], [(h("16:30"), h("23:30"), "off")])
        self.assertEqual(day.groups["3.1"], [(13.0, 16.5, "off"), (23.5, 24.0, "off")])
        self.assertEqual(day.groups["4.2"], [(13.0, 16.5, "off"), (20.0, 24.0, "off")])
        self.assertEqual(day.groups["5.1"], [(14.5, 20.0, "off")])
        self.assertFalse(day.extras)
        self.assertIn("діятимуть наступні черги", day.header)

    def test_topmost_block_wins(self):
        older = SCHEDULE.replace("16:30-23:30", "10:00-12:00")  # старіша версія нижче
        st = cek_site.parse_page(HEAD + SCHEDULE + older + TAIL, TODAY)
        self.assertEqual(st.days[TODAY].groups["1.1"], [(16.5, 23.5, "off")])

    def test_tomorrow_block(self):
        st = cek_site.parse_page(HEAD + TOMORROW + SCHEDULE + TAIL, TODAY)
        tomorrow = date(2026, 11, 21)
        self.assertEqual(st.days[tomorrow].groups["1.1"], [(9.5, 13.0, "off")])
        self.assertEqual(st.days[TODAY].groups["1.1"], [(16.5, 23.5, "off")])

    def test_windows_1251_page(self):
        raw = ('<html><head><meta charset="windows-1251"></head><body>' + HEAD + SCHEDULE + "</body></html>")
        raw_bytes = raw.replace("🔹 ", "").replace("📢", "").encode("cp1251")
        st = cek_site.parse_page(cek_site.decode(raw_bytes), TODAY)
        self.assertEqual(st.days[TODAY].groups["2.1"], [(20.0, 23.5, "off")])


class ExtraTests(unittest.TestCase):
    """2) «з 13:45 додатково відключаються черги: 5.1, 5.2» вище графіка."""

    def test_exact_page(self):
        st = cek_site.parse_page(PAGE, TODAY)
        day = st.days[TODAY]
        # 5.1/5.2 за графіком 14:30–20:00 + «додатково з 13:45» -> 13:45–20:00
        self.assertEqual(day.groups["5.1"], [(h("13:45"), 20.0, "off")])
        self.assertEqual(day.groups["5.2"], [(h("13:45"), 20.0, "off")])
        self.assertEqual(day.groups["1.1"], [(16.5, 23.5, "off")])  # інші черги — без змін
        self.assertEqual([(e.grp, e.start) for e in day.extras], [("5.1", h("13:45")), ("5.2", h("13:45"))])
        self.assertFalse(st.emergency)

    def test_extra_below_schedule_is_old(self):
        st = cek_site.parse_page(HEAD + SCHEDULE + EXTRA + TAIL, TODAY)
        self.assertEqual(st.days[TODAY].groups["5.1"], [(14.5, 20.0, "off")])
        self.assertFalse(st.days[TODAY].extras)

    def test_extra_other_date_not_applied(self):
        other = EXTRA.replace("на 20 ЛИСТОПАДА", "на 21 ЛИСТОПАДА")
        st = cek_site.parse_page(HEAD + other + SCHEDULE + TAIL, TODAY)
        self.assertEqual(st.days[TODAY].groups["5.1"], [(14.5, 20.0, "off")])
        day21 = st.days[date(2026, 11, 21)]
        self.assertTrue(day21.partial)  # графіка на 21-ше ще немає — лише «додатково»
        self.assertEqual(sorted(day21.groups), ["5.1", "5.2"])

    def test_extra_with_explicit_end_and_inside_slot(self):
        explicit = ('<p>📢 Зміни в ГПВ на 20 ЛИСТОПАДА: з 10:00 до 12:00 додатково відключаються черги: 1.1, 3.1;</p>\n')
        st = cek_site.parse_page(HEAD + explicit + SCHEDULE, TODAY)
        self.assertEqual(st.days[TODAY].groups["1.1"], [(10.0, 12.0, "off"), (16.5, 23.5, "off")])
        inside = '<p>📢 Зміни в ГПВ на 20 ЛИСТОПАДА: з 14:00 додатково відключаються черги: 3.1;</p>\n'
        st = cek_site.parse_page(HEAD + inside + SCHEDULE, TODAY)
        self.assertEqual(st.days[TODAY].groups["3.1"], [(13.0, 16.5, "off"), (23.5, 24.0, "off")])  # і так вимкнено

    def test_extra_without_nearby_slot_is_maybe(self):
        far = '<p>📢 Зміни в ГПВ на 20 ЛИСТОПАДА: з 06:00 додатково відключаються черги: 1.1;</p>\n'
        st = cek_site.parse_page(HEAD + far + SCHEDULE, TODAY)
        self.assertEqual(st.days[TODAY].groups["1.1"], [(6.0, 10.0, "maybe"), (16.5, 23.5, "off")])


class EmergencyTests(unittest.TestCase):
    """3) ЕКСТРЕНІ ВІДКЛЮЧЕННЯ: введення / скасування / порядок відносно графіка."""

    def test_emergency_above_schedule(self):
        st = cek_site.parse_page(HEAD + EMERGENCY_ON + PAGE, TODAY)
        self.assertTrue(st.emergency)
        self.assertIn("ЕКСТРЕНІ ВІДКЛЮЧЕННЯ", st.emergency_text)
        self.assertEqual(st.days[TODAY].groups["1.1"], [(16.5, 23.5, "off")])  # графік лишається для довідки

    def test_cancelled_above(self):
        st = cek_site.parse_page(HEAD + EMERGENCY_OFF + EMERGENCY_ON + PAGE, TODAY)
        self.assertFalse(st.emergency)

    def test_cancel_then_reintroduced(self):
        st = cek_site.parse_page(HEAD + EMERGENCY_ON + EMERGENCY_OFF + EMERGENCY_ON + PAGE, TODAY)
        self.assertTrue(st.emergency)

    def test_emergency_below_current_schedule_is_old(self):
        st = cek_site.parse_page(HEAD + SCHEDULE + EMERGENCY_ON + TAIL, TODAY)
        self.assertFalse(st.emergency)

    def test_emergency_without_schedule_today(self):
        st = cek_site.parse_page(HEAD + EMERGENCY_ON, TODAY)
        self.assertTrue(st.emergency)

    def test_other_city_only(self):
        kam = "<p>📢 У м. Кам'янське застосовуються ЕКСТРЕНІ ВІДКЛЮЧЕННЯ.</p>\n"
        st = cek_site.parse_page(HEAD + kam + PAGE, TODAY)
        self.assertFalse(st.emergency)


if __name__ == "__main__":
    unittest.main()
