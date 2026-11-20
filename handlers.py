import time
from datetime import datetime
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

import db
import keyboards as kb
from providers import get_schedule_days
from config import TZ
from render import day_text
from status import verdict

router = Router()

MENU_TEXT = "⚡ <b>Світло Дніпро</b>\n{prov} · група <b>{grp}</b>\n\nОбери, що показати:"
PICK_TEXT = (
    "🏢 Обери свого <b>постачальника</b> (ДТЕК чи ЦЕК — є в рахунку за світло),\n"
    "🔢 потім свою <b>групу</b> відключень.\n\n"
    "Зараз: <b>{prov}</b>"
)
CHANNEL_NOTE = "\n\nℹ️ Графіка поки немає і в Telegram-каналах."
ICON = {"on": "💡", "off": "🕯"}


def _prov(u: dict) -> str:
    return u.get("provider") or "dtek"


def _pname(u: dict) -> str:
    return db.PROVIDER_NAME[_prov(u)]


def _menu_text(u: dict) -> str:
    return MENU_TEXT.format(prov=_pname(u), grp=u["grp"] or "—")


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
        await m.answer(PICK_TEXT.format(prov=_pname(u)), reply_markup=kb.groups_kb(None, _prov(u)))
    else:
        await m.answer(_menu_text(u), reply_markup=kb.main_menu())


@router.callback_query(kb.SetProvider.filter())
async def set_provider(cb: CallbackQuery, callback_data: kb.SetProvider):
    await db.set_user(cb.from_user.id, provider=callback_data.code)
    u = await db.get_user(cb.from_user.id)
    await edit(cb, PICK_TEXT.format(prov=_pname(u)), kb.groups_kb(u["grp"], _prov(u)))


@router.callback_query(kb.SetGroup.filter())
async def set_group(cb: CallbackQuery, callback_data: kb.SetGroup):
    await db.set_user(cb.from_user.id, grp=callback_data.code)
    await edit(cb, _menu_text(await db.get_user(cb.from_user.id)), kb.main_menu())


@router.callback_query(kb.Nav.filter(F.to == "menu"))
async def menu(cb: CallbackQuery):
    await edit(cb, _menu_text(await db.get_user(cb.from_user.id)), kb.main_menu())


@router.callback_query(kb.Nav.filter(F.to == "groups"))
async def groups(cb: CallbackQuery):
    u = await db.get_user(cb.from_user.id)
    await edit(cb, PICK_TEXT.format(prov=_pname(u)), kb.groups_kb(u["grp"], _prov(u)))


async def _need_group(cb: CallbackQuery):
    u = await db.get_user(cb.from_user.id)
    if not u["grp"]:
        await edit(cb, PICK_TEXT.format(prov=_pname(u)), kb.groups_kb(None, _prov(u)))
        return None
    return u


@router.callback_query(kb.Nav.filter(F.to == "sched"))
async def sched(cb: CallbackQuery, callback_data: kb.Nav):
    if not (u := await _need_group(cb)):
        return
    day = "tomorrow" if callback_data.arg.partition("-")[0] == "tomorrow" else "today"
    schedule = (await get_schedule_days(_prov(u)))[day]
    title = "Сьогодні" if day == "today" else "Завтра"
    slots = schedule.groups.get(u["grp"])
    text = f"🏢 {_pname(u)}\n" + day_text(u["grp"], slots, title)
    if slots is not None and (label := schedule.label()):
        text += f"\n\n{label}"
    elif slots is None and schedule.emergency:
        text += "\n\n🚨 За даними YASNO діють екстрені відключення."
    elif slots is None and schedule.source == "channel":
        text += CHANNEL_NOTE
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
    clock = datetime.now(TZ).strftime("%H:%M")
    text = f"⚡ <b>{_pname(u)} · група {u['grp']}</b>  ·  {clock}\n\n{await verdict(u['grp'], _prov(u))}"
    await edit(cb, text, kb.now_kb())


@router.callback_query(kb.Nav.filter(F.to == "chats"))
async def chats(cb: CallbackQuery):
    if not (u := await _need_group(cb)):
        return
    p, g = _prov(u), u["grp"]
    head = f"💬 <b>Що пишуть про {_pname(u)} {g}</b> <i>(не перевірено)</i>\n\n"
    c = (await db.chat_stats(provider=p)).get(g)
    if c:
        head += f"За 30 хв: 💡 «є» — {c.get('on', 0)}, 🕯 «нема» — {c.get('off', 0)}\n\n"
    msgs = await db.recent_chat_msgs(g, p)
    if not msgs:
        text = head + f"За останні 2 год про групу {g} ніхто не писав."
    else:
        lines = []
        for r in msgs:
            place = " · ".join(x for x in (r["district"] or "", f"вул. {r['street']}" if r.get("street") else "") if x)
            lines.append(
                f"{ICON.get(r['event'], '❔')} {_ago(r['ts'])}{f' · <b>{escape(place)}</b>' if place else ''}\n"
                f"<i>{escape(r['text'][:120])}</i>"
            )
        text = head + "\n".join(lines)
    await edit(cb, text[:4000], kb.simple_kb("chats"))


@router.callback_query(kb.Nav.filter(F.to == "report"))
async def report_menu(cb: CallbackQuery):
    if not (u := await _need_group(cb)):
        return
    text = (
        f"🙋 <b>Як у тебе зараз?</b> · {_pname(u)} · група <b>{u['grp']}</b>\n\n"
        f"{_crowd_line(await db.report_stats(u['grp'], provider=_prov(u)))}\n\nТвій звіт допоможе сусідам."
    )
    await edit(cb, text, kb.report_kb())


@router.callback_query(kb.Report.filter())
async def report(cb: CallbackQuery, callback_data: kb.Report):
    if not (u := await _need_group(cb)):
        return
    await db.add_report(cb.from_user.id, u["grp"], callback_data.status, _prov(u))
    stats = await db.report_stats(u["grp"], provider=_prov(u))
    await edit(cb, f"✅ Дякую! Записав: {ICON[callback_data.status]}\n\n{_crowd_line(stats)}", kb.report_kb())


SETTINGS_TEXT = "⚙️ <b>Налаштування</b>\n\nЗа скільки хвилин попереджати про відключення:"


@router.callback_query(kb.Nav.filter(F.to == "settings"))
async def settings(cb: CallbackQuery):
    await edit(cb, SETTINGS_TEXT, kb.settings_kb(await db.get_user(cb.from_user.id)))


@router.callback_query(kb.Notify.filter())
async def notify(cb: CallbackQuery, callback_data: kb.Notify):
    u = await db.get_user(cb.from_user.id)
    if callback_data.value == 0:
        await db.set_user(cb.from_user.id, notify_on=0 if u["notify_on"] else 1)
    else:
        await db.set_user(cb.from_user.id, notify_min=callback_data.value, notify_on=1)
    await edit(cb, SETTINGS_TEXT, kb.settings_kb(await db.get_user(cb.from_user.id)))
