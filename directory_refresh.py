"""Оновлення довідника вулиць і будинків ДТЕК (dtek_streets / dtek_houses) прямо на сервері — з телефона.

Адмін у боті: /db -> «🔄 Оновити довідник». Далі notifier кожні 15 с викликає step(): одна вулиця за раз
(getHomeNum з кешованим csrf, без завантаження сторінки), між вулицями — пауза темпу (повільно ~12 год,
швидше ~4 год на весь Дніпро). Сайт відмовив — спільна пауза DTEK_BACKOFF_SEC, потім продовжуємо з тієї ж
вулиці. Стан — у kv, тож перезапуск бота не скидає прогрес. Наприкінці адміну — підсумок: як змінились черги.
"""
import json
import logging
import time

import db
import dtek_http
import dtek_site
import groups as G
from address import norm_house
from config import ADMIN_IDS

log = logging.getLogger(__name__)

KEY = "dir_refresh"
PACES = {"slow": 20, "fast": 6}  # секунд між вулицями: ~12 год і ~4 год на 2232 вулиці
PACE_NAME = {"slow": "повільно", "fast": "швидше"}


async def state() -> dict | None:
    return json.loads(await db.kv_get(KEY) or "null")


async def _save(st: dict):
    await db.kv_set(KEY, json.dumps(st, ensure_ascii=False))


async def group_counts() -> dict[str, int]:
    rows = await db._all("SELECT COALESCE(group_name, '—') g, COUNT(*) n FROM dtek_houses WHERE operator='dtek' GROUP BY 1")
    return {r["g"]: r["n"] for r in rows}


async def start(pace: str = "slow") -> dict:
    """Нове оновлення (або зміна темпу поточного — прогрес не губимо)."""
    st = await state()
    if st and not st.get("finished") and st.get("started"):  # іде або зупинене — продовжуємо з того ж місця
        st.update(running=True, pace=pace)
        st.pop("stopped", None)
    else:
        total = (await db.directory_stats())["streets"]
        st = {"running": True, "pace": pace, "started": int(time.time()), "total": total, "done": 0, "failed": 0,
              "last": 0, "before": await group_counts(), "street": "", "cursor": 0}
    await _save(st)
    log.info("довідник: оновлення %s (%s)", "запущено", PACE_NAME.get(pace, pace))
    return st


async def stop() -> dict | None:
    st = await state()
    if st and st.get("running"):
        st["running"] = False
        st["stopped"] = int(time.time())
        await _save(st)
    return st


async def _next_street(cursor: int) -> tuple[int, str] | None:
    """Вулиці йдуть по id: курсор — остання опитана (або пропущена) вулиця, тож «Оновити» після зупинки
    продовжує з того ж місця, і кожна вулиця — рівно раз."""
    rows = await db._all("SELECT id, raw_name FROM dtek_streets WHERE id > ? ORDER BY id LIMIT 1", (cursor,))
    return (rows[0]["id"], rows[0]["raw_name"]) if rows else None


async def step(bot) -> bool:
    """Одна вулиця, якщо оновлення йде і настав її час. True — вулицю опитали."""
    st = await state()
    if not st or not st.get("running") or dtek_http.backoff_left():
        return False
    if time.time() - st.get("last", 0) < PACES.get(st.get("pace"), PACES["slow"]):
        return False
    if not st.get("streets_synced"):  # спершу — повний перелік вулиць: нові вулиці теж потраплять в обхід
        return await _sync_streets(st)
    nxt = await _next_street(st.get("cursor", 0))
    if nxt is None:
        await _finish(bot, st)
        return False
    sid, street = nxt
    st["last"], st["street"] = int(time.time()), street
    await _save(st)
    try:
        before = await _house_norms(street)
        async with dtek_site._Session() as s:
            houses = await s.houses(street)  # пише будинки (нові — додає) й last_polled_at
        now = {norm_house(h.num) for h in houses}
        gone = before - now if now else set()  # порожня відповідь там, де були будинки, — збій, не знесення
        if gone:
            await _delete_houses(street, gone)
        st["added"] = st.get("added", 0) + len(now - before)
        st["removed"] = st.get("removed", 0) + len(gone)
        st["done"] += 1
        st["cursor"] = sid
    except dtek_http.DtekError as e:  # антибот / мережа: пауза вже стоїть, цю ж вулицю — після неї
        log.warning("довідник: %s — %r", street, e)
        st["retries"] = st.get("retries", 0) + 1
        if st["retries"] >= 3:  # тричі поспіль не вийшло — пропускаємо, щоб не застрягти
            st.update(cursor=sid, retries=0, failed=st["failed"] + 1)
    except ValueError as e:  # сайт відповів, але не тим — вулицю пропускаємо
        st["failed"] += 1
        st["cursor"] = sid
        log.warning("довідник: %s — %r", street, e)
    else:
        st["retries"] = 0
    fresh = await state() or st  # поки йшов запит, адмін міг зупинити чи змінити темп
    await _save({**fresh, **{k: st[k] for k in ("last", "street", "done", "failed", "cursor", "added", "removed")
                             if k in st}, "retries": st.get("retries", 0)})
    return True


async def _sync_streets(st: dict) -> bool:
    """getStreets (один запит) -> нові вулиці в dtek_streets; вони отримують більший id і потрапляють в обхід."""
    st["last"] = int(time.time())
    try:
        async with dtek_site._Session() as s:
            names = await s.street_list()
    except dtek_http.DtekError as e:  # пауза вже стоїть — спробуємо після неї
        log.warning("довідник: перелік вулиць — %r", e)
        await _save({**(await state() or st), "last": st["last"]})
        return True
    except ValueError as e:
        log.warning("довідник: перелік вулиць — %r", e)
        names = []
    new = await db.upsert_streets(names) if names else 0
    if new:
        log.info("довідник: нових вулиць %d", new)
    total = (await db.directory_stats())["streets"]
    await _save({**(await state() or st), "last": st["last"], "streets_synced": True, "new_streets": new,
                 "total": total})
    return True


async def _house_norms(street: str) -> set[str]:
    rows = await db._all("SELECT h.house_norm n FROM dtek_houses h JOIN dtek_streets s ON s.id=h.street_id "
                         "WHERE s.raw_name=?", (street,))
    return {r["n"] for r in rows}


async def _delete_houses(street: str, norms: set[str]):
    """Будинків уже немає на сайті ДТЕК — прибираємо з довідника."""
    marks = ",".join("?" * len(norms))
    await db._exec(f"DELETE FROM dtek_houses WHERE street_id=(SELECT id FROM dtek_streets WHERE raw_name=?) "
                   f"AND house_norm IN ({marks})", (street, *norms))


async def _finish(bot, st: dict):
    after = await group_counts()
    st.update(running=False, finished=int(time.time()), after=after)
    await _save(st)
    await db.load_known_groups()
    log.info("довідник: оновлено %d вулиць, помилок %d", st["done"], st["failed"])
    text = "✅ <b>Довідник адрес оновлено</b>\n" + summary(st)
    for uid in ADMIN_IDS:
        try:
            await bot.send_message(uid, text)
        except Exception as e:  # pragma: no cover
            log.warning("довідник: адміну %s не надіслано: %r", uid, e)


def changes(before: dict, after: dict) -> list[str]:
    out = []
    for g in G.sort(k for k in set(before) | set(after) if k != "—") + (["—"] if "—" in set(before) | set(after) else []):
        d = after.get(g, 0) - before.get(g, 0)
        if d:
            out.append(f"{'без черги' if g == '—' else g}: {before.get(g, 0)} → {after.get(g, 0)} ({d:+d})")
    return out


def summary(st: dict) -> str:
    took = (st.get("finished") or time.time()) - st["started"]
    lines = [f"Вулиць: {st['done']} · помилок: {st['failed']} · за {int(took // 3600)} год {int(took % 3600 // 60)} хв",
             f"Нових вулиць: {st.get('new_streets', 0)} · нових будинків: {st.get('added', 0)} · "
             f"прибрано будинків: {st.get('removed', 0)}"]
    diff = changes(st.get("before") or {}, st.get("after") or {})
    lines.append("Зміни черг (будинків):\n" + "\n".join(diff) if diff else "Черги будинків не змінились.")
    return "\n".join(lines)


def _hm(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 3600} год {sec % 3600 // 60} хв" if sec >= 3600 else f"{sec // 60} хв"


async def report() -> str:
    """Екран /db: довідник + стан оновлення."""
    d = await db.directory_stats()
    oldest = await db._all("SELECT MIN(last_polled_at) t FROM dtek_streets WHERE last_polled_at IS NOT NULL")
    counts = await group_counts()
    from datetime import datetime

    from config import TZ

    lines = ["🗂 <b>Довідник адрес ДТЕК</b>",
             f"Вулиць: {d['streets']} · будинків: {d['houses']} · без черги: {d['no_queue']}"]
    if oldest and oldest[0]["t"]:
        lines.append(f"Найстаріші дані: {datetime.fromtimestamp(oldest[0]['t'], TZ):%d.%m %H:%M}")
    top = sorted(((g, n) for g, n in counts.items() if g != "—"), key=lambda x: -x[1])
    if top:
        lines.append("Черги: " + ", ".join(f"{g} — {n}" for g, n in top))
    st = await state()
    lines.append("")
    if st and st.get("running"):
        left = max(st["total"] - st["done"] - st["failed"], 0)
        pause = dtek_http.backoff_left()
        lines.append(f"🔄 <b>Оновлення йде</b> ({PACE_NAME.get(st['pace'], st['pace'])}): {st['done']}/{st['total']}, "
                     f"помилок {st['failed']}")
        if st.get("street"):
            lines.append(f"Зараз: {st['street']}")
        lines.append(f"Залишилось ≈ {_hm(left * PACES.get(st['pace'], 20) + pause)}"
                     + (f" (сайт на паузі ще {_hm(pause)})" if pause else ""))
    elif st and st.get("finished"):
        lines.append(f"✅ Останнє оновлення: {datetime.fromtimestamp(st['finished'], TZ):%d.%m %H:%M}\n" + summary(st))
    elif st and st.get("stopped"):
        lines.append(f"⏹ Оновлення зупинено на {st['done']}/{st['total']} — «Оновити» продовжить з того ж місця")
    else:
        lines.append("Оновлення ще не запускали.")
    return "\n".join(lines)


async def run(bot):
    """Фонова задача бота: раз на секунду — чи не час наступної вулиці (темп задає PACES)."""
    import asyncio

    while True:
        try:
            await step(bot)
        except Exception:
            log.exception("довідник: крок оновлення впав")
        await asyncio.sleep(1)
