import time
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

import db
import keyboards as kb
import providers
from providers import get_schedule
from render import day_text, now_status

router = Router()

MENU_TEXT = "⚡ <b>Світло Дніпро</b>\nГрупа: <b>{grp}</b>\n\nОбери, що показати:"
PICK_TEXT = "🔢 Обери свою групу відключень.\nДізнатись можна на сайті ДТЕК/YASNO або в рахунку."
ICON = {"on": "💡", "off": "🕯"}


async def edit(cb: CallbackQuery, text: str, markup):
    try:
        await cb.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise
    await cb.answer()


@router.message(CommandStart())
@router.message(Command("menu"))
async def start(m: Message):
    u = await db.get_user(m.from_user.id)
    if not u["grp"]:
        await m.answer(PICK_TEXT, reply_markup=kb.groups_kb(None))
    else:
        await m.answer(MENU_TEXT.format(grp=u["grp"]), reply_markup=kb.main_menu())


@router.callback_query(kb.SetGroup.filter())
async def set_group(cb: CallbackQuery, callback_data: kb.SetGroup):
    await db.set_user(cb.from_user.id, grp=callback_data.code)
    await edit(cb, MENU_TEXT.format(grp=callback_data.code), kb.main_menu())


@router.callback_query(kb.Nav.filter(F.to == "menu"))
async def menu(cb: CallbackQuery):
    u = await db.get_user(cb.from_user.id)
    await edit(cb, MENU_TEXT.format(grp=u["grp"] or "—"), kb.main_menu())


@router.callback_query(kb.Nav.filter(F.to == "groups"))
async def groups(cb: CallbackQuery):
    u = await db.get_user(cb.from_user.id)
    await edit(cb, PICK_TEXT, kb.groups_kb(u["grp"]))


async def _need_group(cb: CallbackQuery):
    u = await db.get_user(cb.from_user.id)
    if not u["grp"]:
        await edit(cb, PICK_TEXT, kb.groups_kb(None))
        return None
    return u


@router.callback_query(kb.Nav.filter(F.to == "sched"))
async def sched(cb: CallbackQuery, callback_data: kb.Nav):
    if not (u := await _need_group(cb)):
        return
    day, _, refresh = callback_data.arg.partition("-")
    s = await get_schedule(force=bool(refresh))
    title = "Сьогодні" if day == "today" else "Завтра"
    text = day_text(u["grp"], s.get(day, {}).get(u["grp"]), title)
    if providers.last_update:
        text += f"\n\n🕒 Оновлено ДТЕК: {providers.last_update}"
    await edit(cb, text, kb.sched_kb(day))


def _crowd_line(stats: dict) -> str:
    if not stats:
        return "👥 Звітів від людей за 30 хв немає"
    return f"👥 За 30 хв: 🕯 нема — {stats.get('off', 0)}, 💡 є — {stats.get('on', 0)}"


@router.callback_query(kb.Nav.filter(F.to == "now"))
async def now(cb: CallbackQuery):
    if not (u := await _need_group(cb)):
        return
    s = await get_schedule()
    msgs = await db.recent_chat_msgs(hours=1, limit=50)
    text = (
        f"⚡ <b>Зараз</b> · група <b>{u['grp']}</b>\n\n"
        f"{now_status(s.get('today', {}).get(u['grp']))}\n"
        f"{_crowd_line(await db.report_stats(u['grp']))}\n"
        f"💬 Згадок у чатах за годину: {len(msgs)}"
    )
    await edit(cb, text, kb.simple_kb("now"))


@router.callback_query(kb.Nav.filter(F.to == "chats"))
async def chats(cb: CallbackQuery):
    msgs = await db.recent_chat_msgs()
    if not msgs:
        text = "💬 <b>Що пишуть у чатах</b>\n\nЗа останні 2 год нічого релевантного."
    else:
        lines = []
        for r in msgs:
            ago = (int(time.time()) - r["ts"]) // 60
            where = f"<b>{escape(r['district'])}</b> · " if r["district"] else ""
            lines.append(f"{ICON.get(r['event'], '❔')} {where}{ago} хв тому\n<i>{escape(r['text'][:160])}</i>")
        text = "💬 <b>Що пишуть у чатах</b> <i>(не перевірено)</i>\n\n" + "\n\n".join(lines)
    await edit(cb, text, kb.simple_kb("chats"))


@router.callback_query(kb.Nav.filter(F.to == "report"))
async def report_menu(cb: CallbackQuery):
    if not (u := await _need_group(cb)):
        return
    text = (
        f"🙋 <b>Як у тебе зараз?</b> · група <b>{u['grp']}</b>\n\n"
        f"{_crowd_line(await db.report_stats(u['grp']))}\n\nТвій звіт допоможе сусідам."
    )
    await edit(cb, text, kb.report_kb())


@router.callback_query(kb.Report.filter())
async def report(cb: CallbackQuery, callback_data: kb.Report):
    if not (u := await _need_group(cb)):
        return
    await db.add_report(cb.from_user.id, u["grp"], callback_data.status)
    text = f"✅ Дякую! Записав: {ICON[callback_data.status]}\n\n{_crowd_line(await db.report_stats(u['grp']))}"
    await edit(cb, text, kb.report_kb())


@router.callback_query(kb.Nav.filter(F.to == "settings"))
async def settings(cb: CallbackQuery):
    u = await db.get_user(cb.from_user.id)
    await edit(cb, "⚙️ <b>Налаштування</b>\n\nЗа скільки хвилин попереджати про відключення:", kb.settings_kb(u))


@router.callback_query(kb.Notify.filter())
async def notify(cb: CallbackQuery, callback_data: kb.Notify):
    u = await db.get_user(cb.from_user.id)
    if callback_data.value == 0:
        await db.set_user(cb.from_user.id, notify_on=0 if u["notify_on"] else 1)
    else:
        await db.set_user(cb.from_user.id, notify_min=callback_data.value, notify_on=1)
    u = await db.get_user(cb.from_user.id)
    await edit(cb, "⚙️ <b>Налаштування</b>\n\nЗа скільки хвилин попереджати про відключення:", kb.settings_kb(u))
