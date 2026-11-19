import time

import aiosqlite

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY,
    grp TEXT,
    notify_on INTEGER DEFAULT 1,
    notify_min INTEGER DEFAULT 15
);
CREATE TABLE IF NOT EXISTS reports(
    user_id INTEGER, grp TEXT, status TEXT, ts INTEGER
);
CREATE TABLE IF NOT EXISTS chat_msgs(
    chat TEXT, text TEXT, district TEXT, event TEXT, ts INTEGER
);
CREATE TABLE IF NOT EXISTS alerts(
    key TEXT PRIMARY KEY, kind TEXT, grp TEXT, until TEXT, text TEXT, ts INTEGER, sent INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sent(key TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""


async def init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.executescript(SCHEMA)
        for col in ("grp", "street", "who"):  # міграції chat_msgs
            try:
                await db.execute(f"ALTER TABLE chat_msgs ADD COLUMN {col} TEXT DEFAULT ''")
            except aiosqlite.OperationalError:
                pass
        await db.commit()


async def _exec(sql, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(sql, args)
        await db.commit()


async def _all(sql, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, args) as cur:
            return [dict(r) for r in await cur.fetchall()]


# --- users ---
async def get_user(uid):
    rows = await _all("SELECT * FROM users WHERE id=?", (uid,))
    if rows:
        return rows[0]
    await _exec("INSERT INTO users(id) VALUES(?)", (uid,))
    return {"id": uid, "grp": None, "notify_on": 1, "notify_min": 15}


async def set_user(uid, **fields):
    await get_user(uid)
    for k, v in fields.items():
        await _exec(f"UPDATE users SET {k}=? WHERE id=?", (v, uid))


async def users_with_group():
    return await _all("SELECT * FROM users WHERE grp IS NOT NULL")


# --- crowd reports ---
async def add_report(uid, grp, status):
    await _exec("DELETE FROM reports WHERE user_id=?", (uid,))  # один актуальний звіт на людину
    await _exec("INSERT INTO reports VALUES(?,?,?,?)", (uid, grp, status, int(time.time())))


async def report_stats(grp, minutes=30):
    since = int(time.time()) - minutes * 60
    rows = await _all(
        "SELECT status, COUNT(*) c FROM reports WHERE grp=? AND ts>=? GROUP BY status", (grp, since)
    )
    return {r["status"]: r["c"] for r in rows}


# --- chat messages ---
async def add_chat_msg(chat, text, district, event, groups=(), street=None, who=""):
    """who — хеш автора (не сам ID): щоб рахувати різних людей, а не повідомлення."""
    await _exec(
        "INSERT INTO chat_msgs(chat, text, district, event, ts, grp, street, who) VALUES(?,?,?,?,?,?,?,?)",
        (chat, text, district, event, int(time.time()), ",".join(groups), street or "", who),
    )


async def crowd(minutes=15):
    """По групах: різні люди «є»/«нема» з чатів і з кнопок бота + згадані райони/вулиці.
    {"1.2": {"on": {who,...}, "off": {...}, "places": Counter}}"""
    from collections import Counter

    since = int(time.time()) - minutes * 60
    out: dict = {}

    def slot(g):
        return out.setdefault(g, {"on": set(), "off": set(), "places": Counter()})

    for r in await _all("SELECT * FROM chat_msgs WHERE ts>=? AND grp!=''", (since,)):
        for g in r["grp"].split(","):
            s = slot(g)
            s[r["event"]].add(r["who"] or f"m{r['ts']}{r['text'][:20]}")
            for p in (r["district"], r["street"]):
                if p and r["event"] == "on":
                    s["places"][p] += 1
    for r in await _all("SELECT * FROM reports WHERE ts>=?", (since,)):
        slot(r["grp"])[r["status"]].add(f"bot{r['user_id']}")
    return out


async def chat_stats(minutes=30):
    """{"1.1": {"off": 5, "on": 1}, ...} — звіти людей з чатів по групах."""
    since = int(time.time()) - minutes * 60
    out: dict[str, dict[str, int]] = {}
    for r in await _all("SELECT grp, event FROM chat_msgs WHERE ts>=? AND grp!=''", (since,)):
        for g in r["grp"].split(","):
            out.setdefault(g, {}).setdefault(r["event"], 0)
            out[g][r["event"]] += 1
    return out


# --- alerts з каналів ---
async def add_alert(key, kind, grp, until, text) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT OR IGNORE INTO alerts(key, kind, grp, until, text, ts) VALUES(?,?,?,?,?,?)",
            (key, kind, grp, until, text, int(time.time())),
        )
        await db.commit()
        return cur.rowcount > 0


async def pending_alerts(max_age_min=30):
    since = int(time.time()) - max_age_min * 60
    await _exec("UPDATE alerts SET sent=1 WHERE sent=0 AND ts<?", (since,))  # старе не розсилаємо
    return await _all("SELECT * FROM alerts WHERE sent=0 ORDER BY ts")


async def mark_alert_sent(key):
    await _exec("UPDATE alerts SET sent=1 WHERE key=?", (key,))


async def last_alert(kind, grp, minutes):
    since = int(time.time()) - int(minutes * 60)
    rows = await _all(
        "SELECT * FROM alerts WHERE kind=? AND grp=? AND ts>=? ORDER BY ts DESC LIMIT 1", (kind, grp, since)
    )
    return rows[0] if rows else None


async def latest_alert(grp, hours=6):
    since = int(time.time()) - hours * 3600
    rows = await _all(
        "SELECT * FROM alerts WHERE (grp=? OR grp='*') AND ts>=? ORDER BY ts DESC LIMIT 1", (grp, since)
    )
    return rows[0] if rows else None


async def recent_chat_msgs(hours=2, limit=12):
    since = int(time.time()) - hours * 3600
    return await _all(
        "SELECT * FROM chat_msgs WHERE ts>=? ORDER BY ts DESC LIMIT ?", (since, limit)
    )


# --- dedup / kv ---
async def mark_sent(key) -> bool:
    """True якщо ключ новий (ще не відправляли)."""
    try:
        await _exec("INSERT INTO sent VALUES(?)", (key,))
        return True
    except aiosqlite.IntegrityError:
        return False


async def kv_get(k):
    rows = await _all("SELECT v FROM kv WHERE k=?", (k,))
    return rows[0]["v"] if rows else None


async def kv_set(k, v):
    await _exec("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, v))
