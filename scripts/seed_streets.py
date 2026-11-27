"""Повний довідник вулиць і будинків ДТЕК для м. Дніпро — запускати з ПК з українським IP.

    python scripts/seed_streets.py --db-path dtek_seed.db --concurrency 3

1) Перелік вулиць: method=getStreets (сайт віддає всі вулиці міста одним списком) + перелік зі сторінки;
   прогрес — по літерах українського алфавіту. Якщо сайт перелік не віддав — перебір алфавіту запитами.
2) --concurrency воркерів (пауза 0.4–1.0 с між запитами кожного) опитують getHomeNum для вулиць, яких ще не
   опитували (last_polled_at IS NULL), і пишуть усі будинки в dtek_houses: будь-яка черга X.Y (1.1 … 18.1+),
   будинки без черги — group_name NULL, is_no_queue = 1, будинки ЦЕК — operator = 'cek'.
3) Ctrl+C — зупинка; кожна вулиця зберігається одразу, тож повторний запуск докачує решту.

Перенести на сервер (у робочу БД бота, користувачів не чіпає):
    python scripts/seed_streets.py --import-from dtek_seed.db --db-path /data/svitlo.db
"""
import argparse
import asyncio
import random
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import dtek_http  # noqa: E402
import dtek_site  # noqa: E402
import groups as G  # noqa: E402
from address import street_core  # noqa: E402

ALPHABET = "абвгґдеєжзиіїйклмнопрстуфхцчшщьюя"
PAUSE = (0.4, 1.0)
RETRIES = 4


def log(msg: str):
    print(msg, flush=True)


class Site:
    """Одна сесія сайту на всіх воркерів; антибот — відкриваємо сторінку заново (нові кукі), один раз на всіх."""

    def __init__(self):
        self.s: dtek_site._Session | None = None
        self.gen = 0
        self.lock = asyncio.Lock()

    async def open(self, seen_gen: int | None = None, wait: float = 0):
        async with self.lock:
            if seen_gen is not None and seen_gen != self.gen:
                return  # інший воркер уже відкрив нову
            if wait:
                log(f"   … сайт відмовив, пауза {int(wait)} с")
                await asyncio.sleep(wait)
            await dtek_http.reset()
            self.s = await dtek_site._Session(use_backoff=False).__aenter__()
            self.gen += 1


def first_letter(name: str) -> str:
    core = street_core(name)
    return next((ch for ch in core if ch.isalpha()), "?")


async def collect_streets(site: Site, by_letters: bool) -> list[str]:
    streets = set(site.s.streets)
    if not by_letters:
        try:
            streets |= set(await site.s.street_list())
        except dtek_http.ERRORS as e:
            log(f"getStreets: {e!r} — беремо перелік зі сторінки")
    if by_letters or not streets:
        for ch in ALPHABET:
            form = dtek_http.ajax_form("getStreets", [("city", dtek_site.CITY), ("street", ch)])
            try:
                got = dtek_site.parse_streets_payload(await dtek_http.ajax(form, site.s.csrf, use_backoff=False))
            except dtek_http.ERRORS as e:
                log(f"  «{ch}»: {e!r}")
                got = []
            streets |= set(got)
            log(f"  «{ch}»: {len(got)} вулиць")
            await asyncio.sleep(random.uniform(*PAUSE))
    letters = Counter(first_letter(s) for s in streets)
    log("За літерами: " + ", ".join(f"{ch} {letters[ch]}" for ch in ALPHABET if letters[ch])
        + (f", інші {sum(n for ch, n in letters.items() if ch not in ALPHABET)}"
           if any(ch not in ALPHABET for ch in letters) else ""))
    return sorted(streets)


async def worker(site: Site, queue: asyncio.Queue, state: dict):
    while True:
        try:
            street = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        houses = None
        for attempt in range(1, RETRIES + 1):
            gen = site.gen
            try:
                houses = await site.s.houses(street)  # зберігає будинки і last_polled_at
                break
            except dtek_http.Blocked as e:
                log(f"   {street}: {e}")
                try:
                    await site.open(gen, wait=30 * attempt)
                except dtek_http.ERRORS as e2:
                    log(f"   сторінка не відкрилась: {e2!r}")
            except dtek_http.ERRORS as e:
                log(f"   {street}: {e!r} (спроба {attempt}/{RETRIES})")
                await asyncio.sleep(3 * attempt)
        state["done"] += 1
        n = f"[{state['done'] + state['skip']}/{state['total']}]"
        if houses is None:
            state["failed"] += 1
            log(f"{n} {street} — не вдалось, лишається на докачку")
        else:
            no_q = sum(1 for h in houses if not h.cek and not h.groups)
            grps = G.sort(g for h in houses if not h.cek for g in h.groups)
            state["houses"] += len(houses)
            extra = (f" · черги {', '.join(grps)}" if grps else "") + (f" · без черги {no_q}" if no_q else "")
            log(f"{n} {street} — {len(houses)} будинків{extra}")
        await asyncio.sleep(random.uniform(*PAUSE))


async def seed(args):
    db.DB_PATH = args.db_path
    await db.init()
    if args.refresh:
        await db._exec("UPDATE dtek_streets SET last_polled_at=NULL")
    site = Site()
    await site.open()
    log(f"Сторінка ДТЕК відкрита (вулиць на сторінці: {len(site.s.streets)})")
    streets = await collect_streets(site, args.by_letters)
    new = await db.upsert_streets(streets)
    log(f"Вулиць м. Дніпро: {len(streets)} (нових у БД: {new})")

    todo = await db.unpolled_streets()
    total = (await db.directory_stats())["streets"]
    state = {"total": total, "skip": total - len(todo), "done": 0, "failed": 0, "houses": 0}
    log(f"Опитати: {len(todo)} вулиць (уже готово: {state['skip']}), воркерів: {args.concurrency}")
    queue: asyncio.Queue = asyncio.Queue()
    for st in todo:
        queue.put_nowait(st)
    started = time.monotonic()
    await asyncio.gather(*(worker(site, queue, state) for _ in range(max(1, args.concurrency))))
    st = await db.directory_stats()
    grps = await db.load_known_groups()
    log(f"\nГотово за {int(time.monotonic() - started)} с: вулиць {st['polled']}/{st['streets']}, будинків {st['houses']}, "
        f"без черги {st['no_queue']}; не вдалось {state['failed']} (запусти ще раз — докачає)")
    log("Черги: " + ", ".join(grps))


def import_from(src: str, dst: str):
    """Довідник з src (зібраний на ПК) -> робоча БД бота dst: вулиці й будинки upsert, інше не чіпаємо.
    Схема dst має бути актуальна (main() спершу робить db.init)."""
    con = sqlite3.connect(dst)
    con.execute("ATTACH DATABASE ? AS seed", (src,))
    with con:
        con.execute(
            "INSERT INTO dtek_streets(raw_name, norm_name, last_polled_at) "
            "SELECT raw_name, norm_name, last_polled_at FROM seed.dtek_streets WHERE true "
            "ON CONFLICT(raw_name) DO UPDATE SET last_polled_at=MAX(COALESCE(last_polled_at, 0), "
            "COALESCE(excluded.last_polled_at, 0))"
        )
        con.execute(
            "INSERT INTO dtek_houses(street_id, house_raw, house_norm, operator, group_name, is_no_queue, updated_at) "
            "SELECT d.id, h.house_raw, h.house_norm, h.operator, h.group_name, h.is_no_queue, h.updated_at "
            "FROM seed.dtek_houses h JOIN seed.dtek_streets s ON s.id=h.street_id "
            "JOIN main.dtek_streets d ON d.raw_name=s.raw_name WHERE true "
            "ON CONFLICT(street_id, house_norm) DO UPDATE SET house_raw=excluded.house_raw, operator=excluded.operator, "
            "group_name=excluded.group_name, is_no_queue=excluded.is_no_queue, updated_at=excluded.updated_at "
            "WHERE excluded.updated_at >= dtek_houses.updated_at"
        )
    streets, houses = (con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("dtek_streets", "dtek_houses"))
    con.close()
    log(f"Імпортовано в {dst}: вулиць {streets}, будинків {houses}")


async def _init(path: str):
    db.DB_PATH = path
    await db.init()


def main():
    ap = argparse.ArgumentParser(description="Довідник вулиць і будинків ДТЕК (м. Дніпро)")
    ap.add_argument("--db-path", default="dtek_seed.db", help="куди писати (за замовчуванням dtek_seed.db)")
    ap.add_argument("--concurrency", type=int, default=3, help="паралельних воркерів (за замовчуванням 3)")
    ap.add_argument("--refresh", action="store_true", help="переопитати всі вулиці заново")
    ap.add_argument("--by-letters", action="store_true", help="перелік вулиць запитами по кожній літері")
    ap.add_argument("--import-from", metavar="SEED_DB", help="влити готовий довідник у --db-path (на сервері)")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.import_from:
        if not Path(args.import_from).exists():
            sys.exit(f"Немає файлу {args.import_from}")
        asyncio.run(_init(args.db_path))
        import_from(args.import_from, args.db_path)
        return
    if sys.platform == "win32":  # curl_cffi на Windows хоче selector-цикл
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(seed(args))
    except KeyboardInterrupt:
        log("\nЗупинено (Ctrl+C). Усе опитане збережено — запусти ту саму команду ще раз, щоб докачати решту.")
    except dtek_http.ERRORS as e:
        log(f"\nСайт ДТЕК не відкрився: {e!r}. Спробуй пізніше (потрібен український IP).")
        sys.exit(1)


if __name__ == "__main__":
    main()
