from datetime import datetime

from config import TZ
from providers import Slot


def hm(h: float) -> str:
    m = round(h * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


def now_h() -> float:
    n = datetime.now(TZ)
    return n.hour + n.minute / 60


def _cell(slots: list[Slot], h: int) -> str:
    kinds = {s.kind for s in slots if s.start < h + 1 and s.end > h}
    return "🟥" if "off" in kinds else "🟨" if "maybe" in kinds else "🟩"


def timeline(slots: list[Slot]) -> str:
    rows = []
    for a in (0, 12):
        rows.append(f"<code>{a:02d}</code> " + "".join(_cell(slots, h) for h in range(a, a + 12)))
    return "\n".join(rows)


def day_text(grp: str, slots: list[Slot] | None, title: str) -> str:
    head = f"📅 <b>{title}</b> · група <b>{grp}</b>\n\n"
    if slots is None:
        return head + "Графіка ще немає (або джерело недоступне)."
    if not slots:
        return head + "✅ Відключень не заплановано."
    lines = [
        f"{'🔴' if s.kind == 'off' else '🟡'} {hm(s.start)}–{hm(s.end)}"
        for s in sorted(slots, key=lambda s: s.start)
    ]
    off = sum(s.end - s.start for s in slots if s.kind == "off")
    return head + timeline(slots) + "\n\n" + "\n".join(lines) + f"\n\nБез світла: ~{off:g} год\n🟥 точно  🟨 можливо  🟩 є"


def now_status(slots: list[Slot] | None) -> str:
    if slots is None:
        return "❔ Графік невідомий"
    t = now_h()
    cur = next((s for s in slots if s.start <= t < s.end and s.kind == "off"), None)
    if cur:
        return f"🕯 За графіком світла <b>немає</b> до <b>{hm(cur.end)}</b>"
    nxt = min((s for s in slots if s.start > t and s.kind == "off"), key=lambda s: s.start, default=None)
    if nxt:
        left = nxt.start - t
        return f"💡 За графіком світло <b>є</b>. Вимкнуть о <b>{hm(nxt.start)}</b> (через {int(left)} год {round(left % 1 * 60)} хв)"
    return "💡 За графіком світло <b>є</b>, сьогодні більше відключень немає"
