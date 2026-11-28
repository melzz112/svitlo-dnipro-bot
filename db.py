import json
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
-- довідник сайту ДТЕК: усі вулиці м. Дніпро і всі їх будинки з чергами (збирає scripts/seed_streets.py,
-- доповнює кожна успішна відповідь сайту). Будинок без черги — group_name NULL, is_no_queue = 1.
CREATE TABLE IF NOT EXISTS dtek_streets(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_name TEXT NOT NULL UNIQUE,
    norm_name TEXT NOT NULL,
    last_polled_at INTEGER
);
CREATE INDEX IF NOT EXISTS dtek_streets_norm ON dtek_streets(norm_name);
CREATE TABLE IF NOT EXISTS dtek_houses(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    street_id INTEGER NOT NULL REFERENCES dtek_streets(id),
    house_raw TEXT NOT NULL,
    house_norm TEXT NOT NULL,
    operator TEXT NOT NULL DEFAULT 'dtek',
    group_name TEXT,
    is_no_queue INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS dtek_houses_street_house ON dtek_houses(street_id, house_norm);
CREATE INDEX IF NOT EXISTS dtek_houses_group ON dtek_houses(group_name);
-- аварія з часом ремонту по черзі: одне зведене сповіщення групі на (черга, причина, кінець)
CREATE TABLE IF NOT EXISTS group_incidents(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operator TEXT NOT NULL,
    group_name TEXT NOT NULL,
    street_name TEXT,
    reason TEXT NOT NULL,
    start_time INTEGER,
    end_time INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    notified_group INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS group_incidents_key ON group_incidents(operator, group_name, reason, end_time);
-- останній графік YASNO по кожній черзі (ДТЕК і ЦЕК): з ним порівнюємо свіжий, щоб пушити зміни
CREATE TABLE IF NOT EXISTS schedule_snapshots(
    operator TEXT NOT NULL,
    day TEXT NOT NULL,
    group_name TEXT NOT NULL,
    slots_hash TEXT NOT NULL,
    slots TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(operator, day, group_name)
);
"""

# (таблиця, колонка, default) — додаються до існуючої БД
MIGRATIONS = [
    ("chat_msgs", "grp", "''"),
    ("chat_msgs", "street", "''"),
    ("chat_msgs", "who", "''"),
    ("chat_msgs", "provider", "''"),  # '' — не сказано (рахуємо як ДТЕК), 'cek', 'dtek'
    ("users", "provider", "'dtek'"),
    ("reports", "provider", "'dtek'"),
    ("alerts", "provider", "'dtek'"),  # 'dtek' | 'cek' | '*'
    ("alerts", "src", "'channel'"),  # 'channel' — канали-агрегатори, 'dteksite' — перевірка адрес на сайті ДТЕК
    ("users", "created", "0"),  # коли людина вперше написала боту (0 — до появи статистики)
    ("users", "seen", "0"),  # остання активність (натиснула кнопку/написала)
    ("users", "street", "''"),  # адреса людини як на сайті ДТЕК («просп. Дмитра Яворницького») — для точних пушів
    ("users", "house", "''"),  # номер будинку як на сайті («14А»). Координати не зберігаємо.
    ("users", "addr_q", "''"),
    ("users", "quiet", "0"),  # тихі години вночі: лише пуші про аварію в самому будинку
    ("users", "digest", "0"),  # ранкове зведення одним повідомленням  # «вулиця|будинок», поки сайт ДТЕК не відповідав — знайдемо при першій успішній перевірці
]

PROVIDERS = ("dtek", "cek")
PROVIDER_NAME = {"dtek": "ДТЕК", "cek": "ЦЕК"}


async def init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        old_houses = await _old_houses(db)
        await db.executescript(SCHEMA)
        for table, col, default in MIGRATIONS:
            try:
                kind = "INTEGER" if default.isdigit() else "TEXT"
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind} DEFAULT {default}")
            except aiosqlite.OperationalError:
                pass
        await db.commit()
        await _migrate_houses(db, old_houses)
    await load_known_groups()


async def _old_houses(db) -> list[tuple] | None:
    """Стара таблиця dtek_houses(street, num, groups, cek, ts) -> її рядки; саму таблицю прибираємо."""
    async with db.execute("PRAGMA table_info(dtek_houses)") as cur:
        cols = {r[1] for r in await cur.fetchall()}
    if "street" not in cols:
        return None
    async with db.execute("SELECT street, num, groups, cek, ts FROM dtek_houses") as cur:
        rows = list(await cur.fetchall())
    await db.execute("DROP TABLE dtek_houses")
    await db.commit()
    return rows


async def _migrate_houses(db, rows: list[tuple] | None):
    """Перелік вулиць з kv і кеш будинків старого формату -> довідник dtek_streets / dtek_houses."""
    async with db.execute("SELECT v FROM kv WHERE k='dtek_streets'") as cur:
        kv = await cur.fetchone()
    if kv and kv[0]:
        await _upsert_streets(db, json.loads(kv[0]))
    by_street: dict[str, list] = {}
    for street, num, grps, cek, ts in rows or []:
        by_street.setdefault(street, []).append((num, [g for g in (grps or "").split(",") if g], cek or "", ts or 0))
    for street, houses in by_street.items():
        ts = max(h[3] for h in houses) or None
        # старий кеш (черги лише 1.1–6.2) — з його ж часом, щоб свіжий довідник сідера його перезаписав
        await _save_houses(db, street, [h[:3] for h in houses], ts, updated_at=ts or 1)
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


def _chat_provider_sql(provider):
    """Повідомлення без згадки постачальника рахуємо як ДТЕК (більшість міста)."""
    return "provider IN ('dtek','')" if provider == "dtek" else "provider='cek'"


# --- users ---
async def get_user(uid):
    rows = await _all("SELECT * FROM users WHERE id=?", (uid,))
    if rows:
        return rows[0]
    now = int(time.time())
    await _exec("INSERT INTO users(id, created, seen) VALUES(?,?,?)", (uid, now, now))
    return {"id": uid, "grp": None, "notify_on": 1, "notify_min": 15, "provider": "dtek", "street": "", "house": "",
            "created": now, "seen": now}


_seen_cache: dict[int, float] = {}


async def touch_user(uid: int):
    """Остання активність людини; пишемо в БД не частіше раза на хвилину."""
    now = time.time()
    if now - _seen_cache.get(uid, 0) < 60:
        return
    _seen_cache[uid] = now
    await get_user(uid)
    await _exec("UPDATE users SET seen=? WHERE id=?", (int(now), uid))


async def bump(name: str, n: int = 1):
    """Лічильник за добу (для /stats): ключ stat:<name>:YYYY-MM-DD за київським часом."""
    from datetime import datetime

    from config import TZ

    key = f"stat:{name}:{datetime.now(TZ):%Y-%m-%d}"
    await kv_set(key, str(int(await kv_get(key) or 0) + n))


async def set_user(uid, **fields):
    await get_user(uid)
    for k, v in fields.items():
        await _exec(f"UPDATE users SET {k}=? WHERE id=?", (v, uid))


async def users_with_group():
    return await _all("SELECT * FROM users WHERE grp IS NOT NULL")


async def users_with_address():
    """Абоненти ДТЕК, які вказали адресу, — за їх будинками стежимо на сайті ДТЕК."""
    return await _all("SELECT * FROM users WHERE street!='' AND house!='' AND provider='dtek'")


async def users_addr_pending():
    """Адреса чекає на сайт ДТЕК: [{"id", "addr_q": "березинська|45", ...}]."""
    return await _all("SELECT * FROM users WHERE addr_q!=''")


# --- довідник вулиць і будинків сайту ДТЕК (адреса знаходиться за 0 запитів до сайту) ---
def _house_row(num: str, grps: list[str], cek: str) -> tuple[str, str, str, str | None, int]:
    """Будинок з відповіді сайту -> (house_raw, house_norm, operator, group_name, is_no_queue)."""
    import groups as G
    from address import norm_house

    if cek:
        grp, operator = G.from_cek(cek), "cek"
    else:
        grp, operator = next(iter(G.sort(g for g in grps if G.is_group(g))), None), "dtek"
    return str(num), norm_house(num), operator, grp, int(grp is None)


async def _upsert_streets(db, names) -> int:
    from address import street_core

    rows = [(n.strip(), street_core(n)) for n in names if isinstance(n, str) and n.strip()]
    before = db.total_changes
    await db.executemany("INSERT OR IGNORE INTO dtek_streets(raw_name, norm_name) VALUES(?,?)", rows)
    return db.total_changes - before


async def _street_id(db, name: str) -> int:
    from address import street_core

    await db.execute("INSERT OR IGNORE INTO dtek_streets(raw_name, norm_name) VALUES(?,?)", (name, street_core(name)))
    async with db.execute("SELECT id FROM dtek_streets WHERE raw_name=?", (name,)) as cur:
        return (await cur.fetchone())[0]


async def _save_houses(db, street: str, houses, polled_at: int | None, updated_at: int | None = None):
    sid = await _street_id(db, street)
    now = int(updated_at or time.time())
    await db.executemany(
        "INSERT INTO dtek_houses(street_id, house_raw, house_norm, operator, group_name, is_no_queue, updated_at) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(street_id, house_norm) DO UPDATE SET house_raw=excluded.house_raw, "
        "operator=excluded.operator, group_name=excluded.group_name, is_no_queue=excluded.is_no_queue, "
        "updated_at=excluded.updated_at",
        [(sid, *_house_row(num, grps, cek), now) for num, grps, cek in houses],
    )
    if polled_at:
        await db.execute("UPDATE dtek_streets SET last_polled_at=? WHERE id=?", (polled_at, sid))


async def upsert_streets(names) -> int:
    """Перелік вулиць з сайту ДТЕК -> dtek_streets. Повертає, скільки нових."""
    async with aiosqlite.connect(DB_PATH) as db:
        n = await _upsert_streets(db, names)
        await db.commit()
        return n


async def street_names() -> list[str]:
    rows = await _all("SELECT raw_name FROM dtek_streets ORDER BY raw_name")
    if rows:
        return [r["raw_name"] for r in rows]
    return json.loads(await kv_get("dtek_streets") or "[]")


async def save_houses(street: str, houses: list[tuple[str, list[str], str]]):
    """Відповідь сайту по вулиці [(номер, [черги], cek)] -> dtek_houses; вулицю позначаємо опитаною.
    Порожня відповідь теж рахується: вулицю опитали, будинків ДТЕК на ній немає."""
    import groups as G

    async with aiosqlite.connect(DB_PATH) as db:
        await _save_houses(db, street, houses, int(time.time()))
        await db.commit()
    G.register(g for _, grps, cek in houses if not cek for g in grps)


async def unpolled_streets() -> list[str]:
    """Вулиці, яких ще не опитали (докачка сідера після Ctrl+C)."""
    rows = await _all("SELECT raw_name FROM dtek_streets WHERE last_polled_at IS NULL ORDER BY id")
    return [r["raw_name"] for r in rows]


def _cached(r: dict) -> dict:
    cek = r["operator"] == "cek"
    return {"num": r["house_raw"], "groups": [r["group_name"]] if r["group_name"] and not cek else [],
            "cek": (f"ЦЕК {r['group_name'] or ''}".strip() if cek else ""), "operator": r["operator"],
            "group_name": r["group_name"], "is_no_queue": r["is_no_queue"], "ts": r["updated_at"]}


_HOUSE_SQL = "SELECT h.* FROM dtek_houses h JOIN dtek_streets s ON s.id=h.street_id WHERE s.raw_name=?"


async def cached_houses(street: str) -> list[dict]:
    """Будинки вулиці з довідника: [{"num", "groups", "cek", "operator", "group_name", "is_no_queue", "ts"}]."""
    return [_cached(r) for r in await _all(_HOUSE_SQL, (street,))]


async def find_house(street: str, house: str) -> dict | None:
    """Будинок з довідника за нормалізованим номером — без жодного запиту до сайту."""
    from address import norm_house

    rows = await _all(_HOUSE_SQL + " AND h.house_norm=?", (street, norm_house(house)))
    return _cached(rows[0]) if rows else None


async def street_polled(street: str) -> bool:
    rows = await _all("SELECT last_polled_at FROM dtek_streets WHERE raw_name=?", (street,))
    return bool(rows and rows[0]["last_polled_at"])


async def directory_stats() -> dict:
    return (await _all(
        "SELECT (SELECT COUNT(*) FROM dtek_streets) streets, "
        "(SELECT COUNT(*) FROM dtek_streets WHERE last_polled_at IS NOT NULL) polled, "
        "(SELECT COUNT(*) FROM dtek_houses) houses, "
        "(SELECT COUNT(*) FROM dtek_houses WHERE is_no_queue=1 AND operator='dtek') no_queue"
    ))[0]


async def load_known_groups() -> list[str]:
    """Черги з довідника будинків і знімків YASNO -> реєстр groups (кнопки, розбір текстів з чатів)."""
    import groups as G

    rows = await _all(
        "SELECT DISTINCT group_name g FROM dtek_houses WHERE group_name IS NOT NULL AND operator='dtek' "
        "UNION SELECT DISTINCT group_name FROM schedule_snapshots"
    )
    G.register(r["g"] for r in rows)
    return G.known()


ADDR_FAIL_KEEP = 100


async def log_addr_fail(street: str, house: str, why: str, picked: str = ""):
    """Адреса, яку бот не знайшов (без імені людини) — щоб бачити в /db, чого бракує в пошуку."""
    items = json.loads(await kv_get("addr_fail") or "[]")
    items.append({"ts": int(time.time()), "q": street[:60], "house": house[:12], "why": why, "picked": picked[:60]})
    await kv_set("addr_fail", json.dumps(items[-ADDR_FAIL_KEEP:], ensure_ascii=False))
    await bump(f"addr_{why}")


async def addr_fails(limit: int = 10) -> list[dict]:
    return json.loads(await kv_get("addr_fail") or "[]")[-limit:]


async def users_in_group(operator: str, grp: str) -> list[dict]:
    return await _all("SELECT * FROM users WHERE grp=? AND COALESCE(provider,'dtek')=? AND notify_on=1",
                      (grp, operator))


# --- аварії по чергах: одне зведене сповіщення групі на (черга, причина, кінець) ---
async def upsert_incident(operator: str, grp: str, street: str | None, reason: str,
                          start_ts: int | None, end_ts: int) -> tuple[int, bool]:
    """-> (id, новий?). Той самий (черга, причина, кінець) з сусідньої вулиці чи з каналу — той самий інцидент."""
    now = int(time.time())
    key = (operator, grp, reason, int(end_ts))
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT OR IGNORE INTO group_incidents(operator, group_name, street_name, reason, start_time, end_time, "
            "status, notified_group, updated_at) VALUES(?,?,?,?,?,?,'active',0,?)",
            (operator, grp, street, reason, start_ts, int(end_ts), now),
        )
        new = cur.rowcount > 0
        if not new:
            await db.execute(
                "UPDATE group_incidents SET updated_at=?, street_name=COALESCE(street_name, ?), "
                "start_time=COALESCE(MIN(start_time, ?), start_time, ?) "
                "WHERE operator=? AND group_name=? AND reason=? AND end_time=?",
                (now, street, start_ts, start_ts, *key),
            )
        async with db.execute(
            "SELECT id FROM group_incidents WHERE operator=? AND group_name=? AND reason=? AND end_time=?", key
        ) as c:
            iid = (await c.fetchone())[0]
        await db.commit()
    return iid, new


async def expire_incidents(now: int | None = None):
    await _exec("UPDATE group_incidents SET status='ended' WHERE status='active' AND end_time<=?",
                (int(now or time.time()),))


async def end_incidents_except(operator: str, streets: set[str], keep: set[int]):
    """Інциденти сайту ДТЕК з цих вулиць, яких у свіжій відповіді вже немає, -> ended."""
    if not streets:
        return
    marks = ",".join("?" * len(streets))
    rows = await _all(f"SELECT id FROM group_incidents WHERE operator=? AND status='active' AND street_name IN ({marks})",
                      (operator, *streets))
    for r in rows:
        if r["id"] not in keep:
            await _exec("UPDATE group_incidents SET status='ended', updated_at=? WHERE id=?", (int(time.time()), r["id"]))


async def pending_incidents() -> list[dict]:
    """Ще не розіслані групі й ще не скінчені."""
    await expire_incidents()
    return await _all("SELECT * FROM group_incidents WHERE notified_group=0 AND status='active' ORDER BY id")


async def mark_incident_notified(iid: int):
    await _exec("UPDATE group_incidents SET notified_group=1 WHERE id=?", (iid,))


async def notified_incidents(operator: str, grp: str, reason: str, exclude: int, now: int | None = None) -> list[dict]:
    """Уже розіслані групі інциденти з тією ж причиною, чий час ремонту ще не минув (навіть якщо їх замінив
    новий з іншим кінцем) — щоб «+14 хв» не був новиною, а «+2 год» був «продовжено»."""
    return await _all(
        "SELECT * FROM group_incidents WHERE operator=? AND group_name=? AND reason=? AND notified_group=1 "
        "AND end_time>? AND id!=? ORDER BY updated_at DESC, id DESC",
        (operator, grp, reason, int(now or time.time()), exclude),
    )


async def extensions_today(operator: str, grp: str, reason: str) -> int:
    rows = await _all("SELECT COUNT(*) n FROM group_incidents WHERE operator=? AND group_name=? AND reason=? "
                      "AND notified_group=1 AND updated_at>?", (operator, grp, reason, int(time.time()) - 18 * 3600))
    return max(0, rows[0]["n"] - 1)


async def active_incidents(operator: str, grp: str, now: int | None = None) -> list[dict]:
    return await _all(
        "SELECT * FROM group_incidents WHERE operator=? AND group_name=? AND status='active' AND end_time>? "
        "ORDER BY end_time DESC", (operator, grp, int(now or time.time())),
    )


# --- знімки графіків YASNO по чергах ---
async def get_snapshots(operator: str, day: str) -> dict[str, dict]:
    rows = await _all("SELECT * FROM schedule_snapshots WHERE operator=? AND day=?", (operator, day))
    return {r["group_name"]: r for r in rows}


async def has_snapshots(operator: str) -> bool:
    return bool(await _all("SELECT 1 FROM schedule_snapshots WHERE operator=? LIMIT 1", (operator,)))


async def save_snapshots(operator: str, day: str, rows: list[tuple[str, str, str]]):
    """rows: [(черга, хеш, слоти JSON)]. Знімки старші за 3 дні прибираємо."""
    from datetime import datetime, timedelta

    from config import TZ

    now = int(time.time())
    old = (datetime.now(TZ).date() - timedelta(days=3)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            "INSERT OR REPLACE INTO schedule_snapshots(operator, day, group_name, slots_hash, slots, updated_at) "
            "VALUES(?,?,?,?,?,?)", [(operator, day, g, h, sl, now) for g, h, sl in rows],
        )
        await db.execute("DELETE FROM schedule_snapshots WHERE operator=? AND day<?", (operator, old))
        await db.commit()


# --- звіти з кнопок бота ---
async def add_report(uid, grp, status, provider="dtek"):
    await _exec("DELETE FROM reports WHERE user_id=?", (uid,))  # один актуальний звіт на людину
    await _exec(
        "INSERT INTO reports(user_id, grp, status, ts, provider) VALUES(?,?,?,?,?)",
        (uid, grp, status, int(time.time()), provider),
    )


async def report_stats(grp, minutes=30, provider="dtek"):
    since = int(time.time()) - minutes * 60
    rows = await _all(
        "SELECT status, COUNT(*) c FROM reports WHERE grp=? AND provider=? AND ts>=? GROUP BY status",
        (grp, provider, since),
    )
    return {r["status"]: r["c"] for r in rows}


# --- повідомлення з чатів ---
REPEAT_SEC = 12 * 3600  # той самий текст від тієї ж людини за 12 год — спам, не новий звіт


async def add_chat_msg(chat, text, district, event, groups=(), street=None, who="", provider="") -> bool:
    """who — хеш автора (не сам ID): щоб рахувати різних людей, а не повідомлення.
    Емодзі-флуд стискаємо; повтор того самого тексту від тієї ж людини не зберігаємо -> False."""
    from parse import chat_key, clean_chat_text

    text = clean_chat_text(text)
    if who:
        key = chat_key(text)
        for r in await _all("SELECT text FROM chat_msgs WHERE who=? AND ts>=?", (who, int(time.time()) - REPEAT_SEC)):
            if chat_key(r["text"]) == key:
                return False
    await _exec(
        "INSERT INTO chat_msgs(chat, text, district, event, ts, grp, street, who, provider) VALUES(?,?,?,?,?,?,?,?,?)",
        (chat, text, district, event, int(time.time()), ",".join(groups), street or "", who, provider),
    )
    return True


async def crowd(minutes=15, provider="dtek"):
    """По групах: різні люди «є»/«нема» з чатів і з кнопок бота + згадані райони/вулиці.
    {"1.2": {"on": {who,...}, "off": {...}, "places": Counter}}"""
    from collections import Counter

    since = int(time.time()) - minutes * 60
    out: dict = {}

    def slot(g):
        return out.setdefault(g, {"on": set(), "off": set(), "places": Counter()})

    latest: dict[tuple, str] = {}  # (група, людина) -> її ОСТАННІЙ стан: «дали», потім «знову нема» = нема
    rows = await _all(
        f"SELECT * FROM chat_msgs WHERE ts>=? AND grp!='' AND {_chat_provider_sql(provider)} ORDER BY ts", (since,)
    )
    for r in rows:
        who = r["who"] or f"m{r['ts']}{r['text'][:20]}"
        for g in r["grp"].split(","):
            latest[(g, who)] = r["event"]
            for p in (r["district"], r["street"]):
                if p and r["event"] == "on":
                    slot(g)["places"][p] += 1
    for (g, who), ev in latest.items():
        slot(g)[ev].add(who)
    for r in await _all("SELECT * FROM reports WHERE ts>=? AND provider=?", (since, provider)):
        slot(r["grp"])[r["status"]].add(f"bot{r['user_id']}")
    return out


async def crowd_people(grp: str, provider: str, since: float) -> dict[str, tuple[str, int]]:
    """Хто що писав про групу з моменту since: {людина: (останній стан, коли)}. Для екрана «Зараз»."""
    out: dict[str, tuple[str, int]] = {}
    rows = await _all(
        f"SELECT who, event, ts, text FROM chat_msgs WHERE ts>=? AND (','||grp||',') LIKE ? AND "
        f"{_chat_provider_sql(provider)} ORDER BY ts",
        (int(since), f"%,{grp},%"),
    )
    for r in rows:
        out[r["who"] or f"m{r['ts']}{r['text'][:20]}"] = (r["event"], r["ts"])
    for r in await _all(
        "SELECT user_id, status, ts FROM reports WHERE grp=? AND provider=? AND ts>=? ORDER BY ts",
        (grp, provider, int(since)),
    ):
        out[f"bot{r['user_id']}"] = (r["status"], r["ts"])
    return out


def _no_channel_sql() -> str:
    """Аварії з Telegram-каналів вимкнені (CHANNEL_ALERTS=false) — лише сайт ДТЕК."""
    import config

    return "" if config.CHANNEL_ALERTS else " AND COALESCE(src,'channel')!='channel'"


async def off_alerts(grp: str, provider: str, hours=12):
    since = int(time.time()) - hours * 3600
    return await _all(
        "SELECT * FROM alerts WHERE kind='off' AND grp=? AND provider IN (?, '*') AND ts>=?" + _no_channel_sql()
        + " ORDER BY ts", (grp, provider, since),
    )


async def chat_stats(minutes=30, provider="dtek"):
    """{"1.1": {"off": 5, "on": 1}, ...} — скільки різних людей з чатів пишуть «є»/«нема» (від кожної — останнє)."""
    since = int(time.time()) - minutes * 60
    latest: dict[tuple[str, str], str] = {}
    for r in await _all(
        f"SELECT grp, event, who, ts, text FROM chat_msgs WHERE ts>=? AND grp!='' AND {_chat_provider_sql(provider)} "
        "ORDER BY ts", (since,)
    ):
        who = r["who"] or f"m{r['ts']}{r['text'][:20]}"
        for g in r["grp"].split(","):
            latest[(g, who)] = r["event"]
    out: dict[str, dict[str, int]] = {}
    for (g, _), ev in latest.items():
        out.setdefault(g, {}).setdefault(ev, 0)
        out[g][ev] += 1
    return out


async def recent_chat_msgs(grp, provider="dtek", hours=2, limit=10):
    """Лише повідомлення, де згадано саме цю групу, і лише свого постачальника. Той самий текст від тієї ж
    людини — один раз (найсвіжіший), навіть якщо вона вставила його 8 разів."""
    from parse import chat_key

    since = int(time.time()) - hours * 3600
    rows = await _all(
        f"SELECT * FROM chat_msgs WHERE ts>=? AND (','||grp||',') LIKE ? AND {_chat_provider_sql(provider)} "
        "ORDER BY ts DESC LIMIT ?",
        (since, f"%,{grp},%", limit * 10),
    )
    out, seen = [], set()
    for r in rows:
        k = (r["who"], chat_key(r["text"])) if r["who"] else (r["ts"], r["text"])
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
        if len(out) >= limit:
            break
    return out


# --- alerts з каналів ---
async def add_alert(key, kind, grp, until, text, provider="dtek", src="channel", sent=0) -> bool:
    """sent=1 — лише для стану («Зараз», «люди пишуть») без пуша: групі вже пише інцидент (group_incidents)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT OR IGNORE INTO alerts(key, kind, grp, until, text, ts, provider, src, sent) VALUES(?,?,?,?,?,?,?,?,?)",
            (key, kind, grp, until, text, int(time.time()), provider, src, sent),
        )
        await db.commit()
        return cur.rowcount > 0


async def pending_alerts(max_age_min=30):
    since = int(time.time()) - max_age_min * 60
    await _exec("UPDATE alerts SET sent=1 WHERE sent=0 AND ts<?", (since,))  # старе не розсилаємо
    return await _all("SELECT * FROM alerts WHERE sent=0 ORDER BY ts")


async def mark_alert_sent(key):
    await _exec("UPDATE alerts SET sent=1 WHERE key=?", (key,))


async def last_alert(kind, grp, minutes, provider="dtek", src=None):
    since = int(time.time()) - int(minutes * 60)
    rows = await _all(
        "SELECT * FROM alerts WHERE kind=? AND grp=? AND provider IN (?, '*') AND ts>=?"
        + (" AND src=?" if src else _no_channel_sql()) + " ORDER BY ts DESC LIMIT 1",
        (kind, grp, provider, since) + ((src,) if src else ()),
    )
    return rows[0] if rows else None


async def latest_alert(grp, hours=6, provider="dtek"):
    since = int(time.time()) - hours * 3600
    rows = await _all(
        "SELECT * FROM alerts WHERE (grp=? OR grp='*') AND provider IN (?, '*') AND ts>=?" + _no_channel_sql()
        + " ORDER BY ts DESC LIMIT 1", (grp, provider, since),
    )
    return rows[0] if rows else None


# --- резервні графіки з постів у Telegram-каналах ---
async def save_channel_schedule(provider: str, day: str, groups: dict, updated: str, post_ts: float | None = None):
    """day 'YYYY-MM-DD'; groups {"1.1": [[9.5, 13.0], ...]}. Злиття: коригування може мати не всі групи.
    post_ts — час поста: групу оновлюємо лише новішим постом (вчорашній «графік на завтра» з іншого
    каналу не затре сьогоднішнє «Оновлений графік»)."""
    post_ts = time.time() if post_ts is None else post_ts
    cur = json.loads(await kv_get(f"gpv:{provider}:{day}") or "{}")
    stamps = json.loads(await kv_get(f"gpvts:{provider}:{day}") or "{}")
    fresh = {g: v for g, v in groups.items() if post_ts >= stamps.get(g, 0)}
    if not fresh:
        return False
    cur.update({g: [list(x) for x in v] for g, v in fresh.items()})
    stamps.update({g: post_ts for g in fresh})
    await kv_set(f"gpv:{provider}:{day}", json.dumps(cur))
    await kv_set(f"gpvts:{provider}:{day}", json.dumps(stamps))
    if post_ts >= max(stamps.values()):
        await kv_set(f"gpvupd:{provider}:{day}", updated)
    return True


async def get_channel_schedule(provider: str, day: str) -> tuple[dict, str | None]:
    return json.loads(await kv_get(f"gpv:{provider}:{day}") or "{}"), await kv_get(f"gpvupd:{provider}:{day}")


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


# --- режим екстрених відключень (стан веде notifier, читає екран «Зараз») ---
async def get_emergency(provider: str) -> dict | None:
    """{"since": ts, "text": офіційний текст, "source": ...} поки діють екстрені, інакше None."""
    v = await kv_get(f"emerg:{provider}")
    return json.loads(v) if v else None


async def set_emergency(provider: str, state: dict | None):
    await kv_set(f"emerg:{provider}", json.dumps(state, ensure_ascii=False) if state else "")
