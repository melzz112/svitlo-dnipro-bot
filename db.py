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
CREATE TABLE IF NOT EXISTS sent(key TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""


async def init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.executescript(SCHEMA)
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
async def add_chat_msg(chat, text, district, event):
    await _exec("INSERT INTO chat_msgs VALUES(?,?,?,?,?)", (chat, text, district, event, int(time.time())))


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
