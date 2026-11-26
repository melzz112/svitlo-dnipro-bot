import time
from datetime import datetime
from html import escape

import json
import logging

import httpx
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove

import db
import dtek_site
import keyboards as kb
import stats
from address import norm_house, parse_text_address, reverse_geocode
from parse import clean_chat_text
from providers import get_schedule_days
from config import ADMIN_IDS, TZ
from render import day_text
from status import emergency_today, house_block, verdict

log = logging.getLogger(__name__)

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


def _is_admin(user) -> bool:
    return bool(user) and user.id in ADMIN_IDS


def _stats_kb():
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    b = InlineKeyboardBuilder()
    b.button(text="🔄 Оновити", callback_data="adm:stats")
    return b.as_markup()


@router.message(Command("stats"), F.from_user.id.in_(ADMIN_IDS))
async def stats_cmd(m: Message):
    """Лише для адміна. Усім іншим — тиша: команда ніби не існує (див. фільтр вище і handler нижче)."""
    await m.answer(await stats.build(), reply_markup=_stats_kb())


@router.callback_query(F.data == "adm:stats", F.from_user.id.in_(ADMIN_IDS))
async def stats_refresh(cb: CallbackQuery):
    await edit(cb, await stats.build(), _stats_kb())


@router.message(Command("stats"))
async def stats_denied(m: Message):
    log.warning("/stats від чужого користувача %s", m.from_user.id if m.from_user else None)  # відповіді немає


@router.message(CommandStart())
@router.message(Command("menu"))
async def start(m: Message, state: FSMContext):
    await state.clear()
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
async def menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
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
    title = "Сьогодні" if day == "today" else "Завтра"
    em = await emergency_today(u["grp"], _prov(u)) if day == "today" else None
    schedule = (await get_schedule_days(_prov(u)))[day]
    slots = schedule.groups.get(u["grp"])
    text = f"🏢 {_pname(u)}\n" + day_text(u["grp"], slots, title)
    if em:  # графік показуємо завжди, але з попередженням, що під час екстрених він може не діяти
        text = f"{em}\n\n" + text
    if slots and await db.get_emergency(_prov(u)):
        text += "\n\n⚠️ Зараз діють екстрені — графік на завтра діятиме, якщо їх скасують."
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
    text = f"⚡ <b>{_pname(u)} · група {u['grp']}</b>  ·  {clock}\n\n"
    if house := await house_block(u):  # найточніше — що сайт ДТЕК каже саме про твій будинок
        text += f"{house}\n\n<b>По групі:</b>\n"
    text += await verdict(u["grp"], _prov(u))
    await edit(cb, text, kb.now_kb())


@router.callback_query(kb.Nav.filter(F.to == "chats"))
async def chats(cb: CallbackQuery):
    if not (u := await _need_group(cb)):
        return
    p, g = _prov(u), u["grp"]
    head = f"💬 <b>Що пишуть про {_pname(u)} {g}</b> <i>(не перевірено)</i>\n\n"
    c = (await db.chat_stats(provider=p)).get(g)
    if c:
        head += f"За 30 хв пишуть (людей): 💡 «є» — {c.get('on', 0)}, 🕯 «нема» — {c.get('off', 0)}\n\n"
    msgs = await db.recent_chat_msgs(g, p)
    if not msgs:
        text = head + f"За останні 2 год про групу {g} ніхто не писав."
    else:
        lines = []
        for r in msgs:
            place = " · ".join(x for x in (r["district"] or "", f"вул. {r['street']}" if r.get("street") else "") if x)
            lines.append(
                f"{ICON.get(r['event'], '❔')} {_ago(r['ts'])}{f' · <b>{escape(place)}</b>' if place else ''}\n"
                f"<i>{escape(clean_chat_text(r['text'])[:120])}</i>"
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


# ---------- адреса: геопозиція або текст -> будинок на сайті ДТЕК -> група і точні пуші ----------


class Addr(StatesGroup):
    waiting = State()  # чекаємо геопозицію або адресу текстом
    house = State()  # вулицю знаємо, чекаємо номер будинку


ADDR_TEXT = (
    "📍 <b>Твоя адреса</b>\n"
    "Я знайду твій будинок на сайті ДТЕК, сам визначу групу і стежитиму саме за ним: "
    "щойно там аварія чи екстрене — пуш, до котрої години, і коли світло повернеться.\n\n"
    "Натисни <b>«📍 Надіслати геопозицію»</b> (з телефону, коли ти вдома) або напиши адресу, "
    "напр. <i>Яворницького 3</i> чи <i>вул. Шевченка, 10А</i>.\n\n"
    "🔒 Координати використовуються лише щоб знайти адресу (OpenStreetMap) і не зберігаються. "
    "Я зберігаю тільки вулицю й номер будинку — видалити можна в налаштуваннях."
)


@router.callback_query(kb.Nav.filter(F.to == "addr"))
async def addr_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Addr.waiting)
    await cb.message.answer(ADDR_TEXT, reply_markup=kb.location_kb())
    await cb.answer()


@router.callback_query(kb.Nav.filter(F.to == "addr_del"))
async def addr_delete(cb: CallbackQuery):
    await db.set_user(cb.from_user.id, street="", house="")
    await db.kv_set(f"house:{cb.from_user.id}", "")
    await db.kv_set(f"house_seen:{cb.from_user.id}", "")
    await edit(cb, SETTINGS_TEXT + "\n\n🗑 Адресу видалено.", kb.settings_kb(await db.get_user(cb.from_user.id)))


@router.message(Addr.waiting, F.text == kb.CANCEL_BTN)
@router.message(Addr.house, F.text == kb.CANCEL_BTN)
async def addr_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Скасовано.", reply_markup=ReplyKeyboardRemove())
    await m.answer(_menu_text(await db.get_user(m.from_user.id)), reply_markup=kb.main_menu())


@router.message(Addr.waiting, F.location)
async def addr_location(m: Message, state: FSMContext):
    try:
        road, house, city = await reverse_geocode(m.location.latitude, m.location.longitude)
    except (httpx.HTTPError, ValueError) as e:
        log.warning("reverse geocode failed: %r", e)
        await m.answer("Не вдалось визначити адресу за геопозицією 😕 Напиши її текстом, напр. <i>Яворницького 3</i>.")
        return
    if city and "дніпр" not in city.lower():
        await m.answer(f"Схоже, це не Дніпро ({city}) — бот поки лише для Дніпра. Напиши адресу текстом.")
        return
    if not road:
        await m.answer("Не бачу вулиці в цій точці. Напиши адресу текстом, напр. <i>Яворницького 3</i>.")
        return
    if not house:
        await state.update_data(street=road)
        await state.set_state(Addr.house)
        await m.answer(f"Вулиця: <b>{escape(road)}</b>.\nНапиши номер будинку (напр. <i>14А</i>).")
        return
    await _addr_resolve(m, state, road, house)


@router.message(Addr.waiting, F.text)
async def addr_text(m: Message, state: FSMContext):
    street, house = parse_text_address(m.text)
    if not street:
        await m.answer("Напиши вулицю і номер будинку, напр. <i>Яворницького 3</i>.")
        return
    if not house:
        await state.update_data(street=street)
        await state.set_state(Addr.house)
        await m.answer(f"Вулиця: <b>{escape(street)}</b>.\nНапиши номер будинку (напр. <i>14А</i>).")
        return
    await _addr_resolve(m, state, street, house)


@router.message(Addr.house, F.text)
async def addr_house(m: Message, state: FSMContext):
    house = norm_house(m.text)
    if not house or not house[0].isdigit():
        await m.answer("Номер будинку — це цифри (і літера, якщо є), напр. <i>14А</i> чи <i>5/2</i>.")
        return
    await _addr_resolve(m, state, (await state.get_data()).get("street", ""), house)


async def _addr_resolve(m: Message, state: FSMContext, street_q: str, house: str):
    await m.answer("🔎 Шукаю будинок на сайті ДТЕК…")
    cached = False
    try:
        res = await dtek_site.lookup(street_q, house)
    except (httpx.HTTPError, ValueError) as e:
        log.warning("ДТЕК lookup %r %r failed: %r", street_q, house, e)
        # сайт закрився антиботом — беремо будинок з кешу (його наповнює кожна успішна перевірка сайту)
        res = await dtek_site.lookup_cached(street_q, house)
        cached = res is not None
        if res is None:
            cands = await dtek_site.street_candidates(street_q)
            if cands:
                await state.clear()
                num = norm_house(house)
                await db.set_user(m.from_user.id, addr_q=f"{street_q}|{num}")
                await m.answer(
                    f"⏳ Сайт ДТЕК зараз не пускає бота. Адресу <b>{escape(cands[0])}, {escape(num)}</b> я запам'ятав: "
                    "щойно сайт відповість (зазвичай за кілька хвилин), сам знайду будинок, поставлю групу "
                    "і напишу тобі.\n\nПоки що можеш обрати групу вручну 👇", reply_markup=ReplyKeyboardRemove())
                u = await db.get_user(m.from_user.id)
                await m.answer(PICK_TEXT.format(prov=_pname(u)), reply_markup=kb.groups_kb(u["grp"], _prov(u)))
                return
            res = dtek_site.Lookup()  # вулиці немає в переліку ДТЕК — нижче попросимо перевірити назву
    if not res.street:
        await m.answer(f"Не знайшов вулицю «{escape(street_q)}» у переліку ДТЕК. Перевір назву й напиши ще раз "
                       "(напр. <i>просп. Яворницького 3</i>). Якщо ти абонент ЦЕК — натисни «❌ Скасувати» "
                       "і обери групу вручну.")
        await state.set_state(Addr.waiting)
        return
    await state.clear()
    await db.set_user(m.from_user.id, addr_q="")  # адресу знайшли — більше не чекаємо сайт
    if not res.house:
        await m.answer(
            f"Вулицю знайшов (<b>{escape(res.street)}</b>), але будинку <b>{escape(house)}</b> немає в даних ДТЕК.\n"
            "Якщо номер правильний — схоже, твій будинок обслуговує <b>ЦЕК</b>. Обери постачальника й групу вручну "
            "(вони є в рахунку за світло).", reply_markup=ReplyKeyboardRemove(),
        )
        u = await db.get_user(m.from_user.id)
        await m.answer(PICK_TEXT.format(prov="ЦЕК"), reply_markup=kb.groups_kb(u["grp"], "cek"))
        return
    h = res.house
    if h.cek:  # сайт ДТЕК позначив будинок як ЦЕК
        cek_grp = dtek_site.cek_group(h)
        if cek_grp:
            await db.set_user(m.from_user.id, provider="cek", grp=cek_grp, street="", house="")
            await m.answer(
                f"🏠 {escape(res.street)}, {escape(h.num)}\nЗа даними сайту ДТЕК твій будинок обслуговує <b>ЦЕК</b> · "
                f"група <b>{cek_grp}</b>. Поставив ЦЕК {cek_grp}.\n\nℹ️ Сповіщень саме по будинку для ЦЕК поки немає — "
                "лише графік, аварії з каналів і те, що пишуть люди.", reply_markup=ReplyKeyboardRemove())
            await m.answer(_menu_text(await db.get_user(m.from_user.id)), reply_markup=kb.main_menu())
        else:
            await m.answer(
                f"🏠 {escape(res.street)}, {escape(h.num)}\nЗа даними сайту ДТЕК твій будинок обслуговує <b>ЦЕК</b>. "
                "Обери групу ЦЕК вручну (є в рахунку за світло).", reply_markup=ReplyKeyboardRemove())
            u = await db.get_user(m.from_user.id)
            await m.answer(PICK_TEXT.format(prov="ЦЕК"), reply_markup=kb.groups_kb(u["grp"], "cek"))
        return
    grp = h.groups[0] if h.groups else None
    fields = {"street": res.street, "house": h.num, "provider": "dtek"}
    if grp:
        fields["grp"] = grp
    await db.set_user(m.from_user.id, **fields)
    if cached:  # з кешу — черга відома, а чи є зараз світло, покаже перша ж перевірка сайту (і напише, якщо нема)
        await db.kv_set(f"house:{m.from_user.id}", "")
        await db.kv_set(f"house_seen:{m.from_user.id}", "")
        grp_line = f"🏢 ДТЕК · група <b>{grp}</b>" if grp else "🏢 ДТЕК · у цього будинку на сайті ДТЕК черги немає — стежу за самим будинком"
        await m.answer(
            f"✅ <b>Знайшов!</b>\n🏠 {escape(res.street)}, {escape(h.num)}\n{grp_line}\n"
            "⏳ Сайт ДТЕК зараз не пускає бота — чи є світло в будинку, перевірю, щойно він відповість.\n\n"
            "Далі я стежу за твоїм будинком на сайті ДТЕК і напишу, щойно буде аварія, продовження чи світло повернеться.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await m.answer(_menu_text(await db.get_user(m.from_user.id)), reply_markup=kb.main_menu())
        return
    # одразу запам'ятовуємо поточний стан, щоб перша ж перевірка не дублювала те, що людина бачить зараз
    await db.kv_set(f"house_seen:{m.from_user.id}", json.dumps(
        {"kind": h.kind, "reason": dtek_site.reason(h) if h.kind else "", "start": h.start, "end": h.end,
         "ts": int(time.time()), "groups": h.groups, "street": res.street, "num": h.num}, ensure_ascii=False))
    off = bool(h.kind) and h.kind != "schedule"
    await db.kv_set(f"house:{m.from_user.id}", json.dumps({"start": h.start, "end": h.end, "miss": 0}) if off else "")
    if off:
        now_line = f"🔴 Зараз: {dtek_site.reason(h)}" + (f" до <b>{dtek_site.short_dt(h.end)}</b>" if h.end else "")
    elif h.kind == "schedule":
        now_line = "🔴 Зараз: відключення за графіком"
    else:
        now_line = "✅ Зараз відключень немає"
    grp_line = f"🏢 ДТЕК · група <b>{grp}</b>" if grp else "🏢 ДТЕК · у цього будинку на сайті ДТЕК черги немає — стежу за самим будинком"
    await m.answer(
        f"✅ <b>Знайшов!</b>\n🏠 {escape(res.street)}, {escape(h.num)}\n{grp_line}\n{now_line}\n\n"
        "Тепер я перевіряю твій будинок на сайті ДТЕК кожні кілька хвилин і напишу, щойно буде аварія, "
        "продовження чи світло повернеться.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await m.answer(_menu_text(await db.get_user(m.from_user.id)), reply_markup=kb.main_menu())
