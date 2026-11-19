from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import GROUPS


class Nav(CallbackData, prefix="nav"):
    to: str  # menu | now | sched | chats | report | settings | groups
    arg: str = ""


class SetGroup(CallbackData, prefix="grp"):
    code: str


class Report(CallbackData, prefix="rep"):
    status: str  # on | off


class Notify(CallbackData, prefix="ntf"):
    value: int  # 0 = вимк/увімк, інакше хвилини


def _back(b: InlineKeyboardBuilder):
    b.button(text="⬅️ Меню", callback_data=Nav(to="menu"))


def main_menu():
    b = InlineKeyboardBuilder()
    b.button(text="⚡ Зараз", callback_data=Nav(to="now"))
    b.button(text="📅 Графік", callback_data=Nav(to="sched", arg="today"))
    b.button(text="💬 Що пишуть у чатах", callback_data=Nav(to="chats"))
    b.button(text="🙋 У мене є / нема світла", callback_data=Nav(to="report"))
    b.button(text="⚙️ Налаштування", callback_data=Nav(to="settings"))
    b.adjust(2, 1, 1, 1)
    return b.as_markup()


def groups_kb(current: str | None):
    b = InlineKeyboardBuilder()
    for g in GROUPS:
        b.button(text=f"✅ {g}" if g == current else g, callback_data=SetGroup(code=g))
    if current:
        _back(b)
    b.adjust(4)
    return b.as_markup()


def sched_kb(day: str):
    b = InlineKeyboardBuilder()
    b.button(text="• Сьогодні •" if day == "today" else "Сьогодні", callback_data=Nav(to="sched", arg="today"))
    b.button(text="• Завтра •" if day == "tomorrow" else "Завтра", callback_data=Nav(to="sched", arg="tomorrow"))
    b.button(text="🔄 Оновити", callback_data=Nav(to="sched", arg=day + "-r"))
    _back(b)
    b.adjust(2, 2)
    return b.as_markup()


def simple_kb(refresh_to: str):
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Оновити", callback_data=Nav(to=refresh_to))
    _back(b)
    return b.as_markup()


def report_kb():
    b = InlineKeyboardBuilder()
    b.button(text="💡 Є світло", callback_data=Report(status="on"))
    b.button(text="🕯 Нема світла", callback_data=Report(status="off"))
    _back(b)
    b.adjust(2, 1)
    return b.as_markup()


def settings_kb(user: dict):
    b = InlineKeyboardBuilder()
    b.button(text=f"🔢 Група: {user['grp'] or '—'}", callback_data=Nav(to="groups"))
    b.button(
        text="🔔 Сповіщення: увімк" if user["notify_on"] else "🔕 Сповіщення: вимк",
        callback_data=Notify(value=0),
    )
    for m in (5, 15, 30, 60):
        mark = "✅ " if user["notify_min"] == m else ""
        b.button(text=f"{mark}{m} хв", callback_data=Notify(value=m))
    _back(b)
    b.adjust(1, 1, 4, 1)
    return b.as_markup()
