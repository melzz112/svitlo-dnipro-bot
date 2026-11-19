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


def _crowd_line(stats: dict, chat: dict | None = None) -> str:
    chat = chat or {}
    off, on = stats.get("off", 0) + chat.get("off", 0), stats.get("on", 0) + chat.get("on", 0)
    if not off and not on:
        return "👥 Звітів від людей за 30 хв немає"
    return f"👥 Люди за 30 хв (бот + чати): 🕯 нема — {off}, 💡 є — {on}"


def _ago(ts: int) -> str:
    m = (int(time.time()) - ts) // 60
    return f"{m} хв тому" if m < 60 else f"{m // 60} год {m % 60} хв тому"


@router.callback_query(kb.Nav.filter(F.to == "now"))
async def now(cb: CallbackQuery):
    if not (u := await _need_group(cb)):
        return
    s = await get_schedule()
    chat = (await db.chat_stats()).get(u["grp"])
    text = (
        f"⚡ <b>Зараз</b> · група <b>{u['grp']}</b>\n\n"
        f"📅 {now_status(s.get('today', {}).get(u['grp']))}\n"
        f"{_crowd_line(await db.report_stats(u['grp']), chat)}"
    )
    if a := await db.latest_alert(u["grp"]):
        text += f"\n\n📣 <b>З каналів</b> ({_ago(a['ts'])}):\n{a['text']}"
    await edit(cb, text, kb.simple_kb("now"))


@router.callback_query(kb.Nav.filter(F.to == "chats"))
async def chats(cb: CallbackQuery):
    u = await db.get_user(cb.from_user.id)
    stats = await db.chat_stats()
    head = "💬 <b>Що пишуть люди</b> <i>(не перевірено)</i>\n\n"
    if stats:
        head += "За 30 хв по групах:\n" + "\n".join(
            f"{'👉 ' if g == u['grp'] else ''}<b>{g}</b>: 🕯 {c.get('off', 0)}  💡 {c.get('on', 0)}"
            for g, c in sorted(stats.items())
        ) + "\n\n"
    msgs = await db.recent_chat_msgs()
    if not msgs:
        text = head + "За останні 2 год повідомлень немає."
    else:
        lines = []
        for r in msgs:
            street = f"вул. {r['street']}" if r.get("street") else ""
            tags = " · ".join(x for x in (r.get("grp") or "", r["district"] or "", street) if x)
            lines.append(
                f"{ICON.get(r['event'], '❔')} {f'<b>{escape(tags)}</b> · ' if tags else ''}{_ago(r['ts'])}\n"
                f"<i>{escape(r['text'][:120])}</i>"
            )
        text = head + "\n".join(lines)
    await edit(cb, text[:4000], kb.simple_kb("chats"))


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
