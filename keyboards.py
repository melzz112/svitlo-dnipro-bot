from aiogram.filters.callback_data import CallbackData
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import GROUPS


class Nav(CallbackData, prefix="nav"):
    to: str  # menu | now | sched | chats | report | settings | groups | addr | addr_del
    arg: str = ""


class SetGroup(CallbackData, prefix="grp"):
    code: str


class SetProvider(CallbackData, prefix="prv"):
    code: str  # dtek | cek


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


def groups_kb(current: str | None, provider: str = "dtek"):
    b = InlineKeyboardBuilder()
    for code, name in (("dtek", "ДТЕК"), ("cek", "ЦЕК")):
        b.button(text=f"✅ {name}" if code == provider else name, callback_data=SetProvider(code=code))
    for g in GROUPS:
        b.button(text=f"✅ {g}" if g == current else g, callback_data=SetGroup(code=g))
    b.button(text="📍 Не знаю групу — визначити за адресою", callback_data=Nav(to="addr"))
    if current:
        _back(b)
    b.adjust(2, 4, 4, 4, 1, 1)
    return b.as_markup()


def location_kb():
    """Звичайна (не inline) клавіатура: лише вона вміє попросити геопозицію."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=LOCATION_BTN, request_location=True)], [KeyboardButton(text=CANCEL_BTN)]],
        resize_keyboard=True, one_time_keyboard=True, input_field_placeholder="або напиши: Яворницького 3",
    )


LOCATION_BTN = "📍 Надіслати геопозицію"
CANCEL_BTN = "❌ Скасувати"


def sched_kb(day: str):
    b = InlineKeyboardBuilder()
    b.button(text="• Сьогодні •" if day == "today" else "Сьогодні", callback_data=Nav(to="sched", arg="today"))
    b.button(text="• Завтра •" if day == "tomorrow" else "Завтра", callback_data=Nav(to="sched", arg="tomorrow"))
    b.button(text="🔄 Оновити", callback_data=Nav(to="sched", arg=day + "-r"))
    _back(b)
    b.adjust(2, 2)
    return b.as_markup()


def now_kb():
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Оновити", callback_data=Nav(to="now"))
    b.button(text="📅 Графік", callback_data=Nav(to="sched", arg="today"))
    _back(b)
    b.adjust(2, 1)
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
    prov = "ЦЕК" if user.get("provider") == "cek" else "ДТЕК"
    b.button(text=f"🔢 {prov} · група {user['grp'] or '—'}", callback_data=Nav(to="groups"))
    b.button(
        text="🔔 Сповіщення: увімк" if user["notify_on"] else "🔕 Сповіщення: вимк",
        callback_data=Notify(value=0),
    )
    for m in (5, 15, 30, 60):
        mark = "✅ " if user["notify_min"] == m else ""
        b.button(text=f"{mark}{m} хв", callback_data=Notify(value=m))
    rows = [1, 1, 4]
    if user.get("street") and user.get("house"):
        b.button(text=f"📍 {user['street']}, {user['house']}"[:60], callback_data=Nav(to="addr"))
        b.button(text="🗑 Видалити адресу", callback_data=Nav(to="addr_del"))
        rows += [1, 1]
    else:
        b.button(text="📍 Вказати адресу — сповіщення по будинку", callback_data=Nav(to="addr"))
        rows += [1]
    _back(b)
    b.adjust(*rows, 1)
    return b.as_markup()
