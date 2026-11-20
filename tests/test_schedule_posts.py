"""Графіки з постів у каналах (резерв, коли сайти недоступні). Запуск: python tests/test_schedule_posts.py"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.stdout.reconfigure(encoding="utf-8")

from parse import parse_schedule_posts  # noqa: E402

D = date(2026, 11, 20)
FOOTER = "\n\n🌐 Наш сайт\n📱 Наш застосунок\n\nПідписатися на канал"
GROUPS = [f"{q}.{s}" for q in range(1, 7) for s in (1, 2)]

# 20.11, 05:38: ДТЕК спершу написав «не прогнозується» для всіх
NONE_POST = "⚠️ ДТЕК: графік відключень на 20.11.2026\n\n" + "\n\n".join(
    f"Група {g}\nВідключення не прогнозується" for g in GROUPS
) + FOOTER
# 07:38: «Оновлений графік»
UPDATED_POST = (
    "⚠️ ДТЕК: графік відключень на 20.11.2026\n\n"
    "Група 1.1\nОновлений графік:\n• 07:30 — 10:00\n\nГрупа 1.2\nОновлений графік:\n• 07:30 — 10:00\n\n"
    "Група 3.1\nОновлений графік:\n• 09:30 — 13:30\n\nГрупа 4.1\nОновлений графік:\n• 20:00 — 24:00" + FOOTER
)
# вчора: «на завтра» з датою
TOMORROW_POST = (
    "⚠️ ДТЕК: графік відключень світла на завтра, 20.11.2026\n\nГрупа 1.1\nГрафік на завтра:\n• 06:00 — 10:00\n"
    "• 16:30 — 20:30\n\nГрупа 1.2\nГрафік на завтра:\n• 06:00 — 10:00\n\nГрупа 2.1\nГрафік на завтра:\n• 16:30 — 20:30"
)
# комбо: ДТЕК (без дати = день поста) + ЦЕК
COMBO_POST = (
    "Екстрені відключення СКАСОВАНО\nГрафік ДТЕК\nГрупа 1.1\n• 01:30 — 05:30\nГрупа 1.2\n• 01:30 — 05:30\n"
    "Група 2.1\n• 12:00 — 16:00\n\nГрафік для ЦЕК\n🔹 Черга 1.1\n05:00-08:30\n🔹 Черга 1.2\n15:30-19:00\n🔹 Черга 2.1\n12:00-19:00"
)
# ЦЕК словами + репост «в один рядок»
CEK_WORDS = "Графік для ЦЕК\n21 ЛИСТОПАДА\n🔹 Черга 1.1\n09:30-13:00\n20:00-22:00\n🔹 Черга 1.2\n20:00-23:30\n🔹 Черга 2.1\n00:00-02:30"
FLAT = "Графік ДТЕК 20.11.2026  Група 1.1 Графік на завтра: • 01:30 — 05:30 • 12:00 — 16:00  Група 1.2 • 22:30 — 24:00  Група 2.1 • 06:00 — 09:00"
# не графіки
WARNING = "⚠️ УВАГА ЦЕК, група 1.1, 1.2 — готуємось до відключення світла з 12:30 до 16:00"
SUMMARY = "ОПЕРАТИВНЕ ЗВЕДЕННЯ (Група 3.1)\nЗсув графіку (до 11:10 19.11.2026):\nм. Дніпро"


def main():
    fails = []

    def check(name, got, exp):
        if got != exp:
            fails.append(f"{name}: {got} != {exp}")

    r = parse_schedule_posts(NONE_POST, D)
    check("не прогнозується", [(p, d, len(g), sum(map(len, g.values()))) for p, d, g in r], [("dtek", D, 12, 0)])

    r = parse_schedule_posts(UPDATED_POST, D)
    check("оновлений графік", r and (r[0][0], r[0][2]["1.2"], r[0][2]["4.1"]), ("dtek", [(7.5, 10.0)], [(20.0, 24.0)]))

    r = parse_schedule_posts(TOMORROW_POST, date(2026, 11, 19))
    check("на завтра з датою", r and (r[0][1], r[0][2]["1.1"]), (D, [(6.0, 10.0), (16.5, 20.5)]))

    r = parse_schedule_posts(COMBO_POST, D)
    check("комбо", [(p, d, g["1.2"]) for p, d, g in r], [("dtek", D, [(1.5, 5.5)]), ("cek", D, [(15.5, 19.0)])])

    r = parse_schedule_posts(CEK_WORDS, D)
    check("ЦЕК словами", r and (r[0][0], r[0][1], r[0][2]["1.1"]), ("cek", date(2026, 11, 21), [(9.5, 13.0), (20.0, 22.0)]))

    r = parse_schedule_posts(FLAT, D)
    check("в один рядок", r and (r[0][2]["1.1"], r[0][2]["1.2"]), ([(1.5, 5.5), (12.0, 16.0)], [(22.5, 24.0)]))

    check("попередження — не графік", parse_schedule_posts(WARNING, D), [])
    check("зведення — не графік", parse_schedule_posts(SUMMARY, D), [])

    for f in fails:
        print("FAIL", f)
    print(f"{8 - len(fails)}/8 ok")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
