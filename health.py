"""Стан джерел даних (YASNO, сайт ДТЕК, Світлобот) і пуш адміну, коли джерело лежить довше FAIL_ALERT_SEC.

Кожне звернення до джерела відмічається mark(source, ok). notifier раз на 15 с викликає check(bot):
джерело падає з якогось моменту і досі не ожило понад 30 хв — адміну «⚠️ … недоступне з 11:02 (помилка)»;
ожило після такого пуша — «✅ … знову працює (не було 1 год 10 хв)». Стан — у kv, переживає перезапуск.
"""
import json
import logging
import time
from datetime import datetime

import db
from config import ADMIN_IDS, TZ

log = logging.getLogger(__name__)

FAIL_ALERT_SEC = 30 * 60
NAMES = {"yasno": "YASNO (графіки)", "dtek": "сайт ДТЕК (перевірка адрес)", "svitlobot": "Світлобот (датчики)"}


def _key(source: str) -> str:
    return f"health:{source}"


async def get(source: str) -> dict:
    return json.loads(await db.kv_get(_key(source)) or "null") or {}


async def mark(source: str, ok: bool, err: str = ""):
    """Результат звернення до джерела. Перша помилка після успіху — початок збою; успіх — кінець."""
    st = await get(source)
    now = int(time.time())
    if ok:
        if st.get("down_since") or st.get("ok") is not True:
            await db.kv_set(_key(source), json.dumps({**st, "ok": True, "down_since": None, "err": "",
                                                      "up_ts": now}, ensure_ascii=False))
        return
    if st.get("down_since"):
        if err and err != st.get("err"):
            await db.kv_set(_key(source), json.dumps({**st, "err": err[:120]}, ensure_ascii=False))
        return
    await db.kv_set(_key(source), json.dumps({**st, "ok": False, "down_since": now, "err": err[:120],
                                              "alerted": False}, ensure_ascii=False))


def _hm(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600} год {sec % 3600 // 60} хв" if sec >= 3600 else f"{max(sec // 60, 1)} хв"


async def check(bot):
    """Пуші адміну: «лежить понад 30 хв» і «знову працює»."""
    now = time.time()
    for source, name in NAMES.items():
        st = await get(source)
        text = None
        if st.get("down_since") and not st.get("alerted") and now - st["down_since"] >= FAIL_ALERT_SEC:
            since = datetime.fromtimestamp(st["down_since"], TZ).strftime("%H:%M")
            text = f"⚠️ <b>{name}</b> недоступне з {since} ({_hm(now - st['down_since'])})"
            if st.get("err"):
                text += f"\n<i>{st['err']}</i>"
            st["alerted"] = True
        elif st.get("ok") and st.get("alerted"):
            down = (st.get("up_ts") or now) - (st.get("alerted_since") or st.get("up_ts") or now)
            text = f"✅ <b>{name}</b> знову працює" + (f" (не було {_hm(down)})" if down > 0 else "")
            st["alerted"] = False
        else:
            continue
        if st.get("alerted"):
            st["alerted_since"] = st["down_since"]
        await db.kv_set(_key(source), json.dumps(st, ensure_ascii=False))
        log.warning("health: %s", text.replace("<b>", "").replace("</b>", ""))
        for uid in ADMIN_IDS:
            try:
                await bot.send_message(uid, text)
            except Exception as e:  # pragma: no cover — адміну не дійшло, не привід зламати цикл
                log.warning("health: адміну %s не надіслано: %r", uid, e)
