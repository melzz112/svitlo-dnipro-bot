"""Групи для точок Світлобота без підпису групи — за адресою з «Перевірити адресу» на сайті ДТЕК.

Запускаємо вручну зрідка (нові точки / ДТЕК переніс будинки):  python svitlobot_map.py
Результат — svitlobot_groups.json {slug: {...}}; бот читає його і на сайт ДТЕК за цим не ходить.
Точки, які вже є у файлі, не перевіряємо знову (крім --all).
"""
import asyncio
import json
import logging
import random
import sys
from pathlib import Path

import httpx

import dtek_site
import svitlobot
from address import find_house, match_street, norm_house, parse_text_address
from config import SVITLOBOT_CITY_FILTER

MAP_PATH = Path(__file__).with_name("svitlobot_groups.json")
log = logging.getLogger(__name__)


def load() -> dict:
    try:
        return json.loads(MAP_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def split_address(address: str) -> tuple[str, str | None]:
    """«Дніпро -> пр Слобожанський 19» -> («пр Слобожанський», «19»); «Юрія Магалевського 6 (п.1-3)» -> (…, «6»)."""
    addr = address.split("->", 1)[-1]
    addr = addr.split("(", 1)[0]
    return parse_text_address(addr)


async def resolve(points: list[svitlobot.Point], known: dict) -> dict:
    out = dict(known)
    todo = [p for p in points if not p.grp and p.slug not in known]
    if not todo:
        return out
    cache: dict[str, dict] = {}
    async with dtek_site._Session() as s:
        for p in todo:
            street_q, house = split_address(p.address)
            entry = {"address": p.address, "provider": None, "grp": None}
            street = match_street(street_q, s.streets) if s.streets else None
            if not street or not house:
                out[p.slug] = {**entry, "why": "вулицю не знайдено на сайті ДТЕК" if house else "немає номера будинку"}
                continue
            if street not in cache:
                try:
                    cache[street] = {h.num: h for h in await s.houses(street)}
                except (httpx.HTTPError, ValueError) as e:
                    log.warning("ДТЕК: %s — %r", street, e)
                    continue  # не записуємо — спробуємо наступного запуску
                await asyncio.sleep(0.5 + random.random())
            found = find_house(cache[street], house)
            if not found:
                out[p.slug] = {**entry, "street": street, "house": norm_house(house), "why": "будинку немає на сайті ДТЕК"}
                continue
            num, h = found
            if h.cek:
                prov, grp = "cek", dtek_site.cek_group(h)
            else:
                prov, grp = "dtek", (h.groups[0] if h.groups else None)
            out[p.slug] = {**entry, "street": street, "house": num, "provider": prov, "grp": grp,
                           **({} if grp else {"why": "сайт ДТЕК не показав групу"})}
    return out


def city_points(raw: str) -> list[svitlobot.Point]:
    """Точки міста SVITLOBOT_CITY_FILTER (лише м. Дніпро — на сайті ДТЕК шукаємо в «м. Дніпро»)."""
    city = svitlobot.region_re(SVITLOBOT_CITY_FILTER or "Дніпро")
    return [p for p in svitlobot.parse_all(raw) if city.search(svitlobot.city_of(p.address))]


async def main(redo: bool):
    points = city_points(await svitlobot.fetch_raw())
    known = {} if redo else load()
    result = await resolve(points, known)
    MAP_PATH.write_text(json.dumps(dict(sorted(result.items())), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    ok = {k: v for k, v in result.items() if v.get("grp")}
    print(f"точок без групи: {sum(1 for p in points if not p.grp)}; визначено: {len(ok)}; ні: {len(result) - len(ok)}")
    for slug, v in sorted(result.items(), key=lambda kv: (kv[1].get("grp") is None, kv[1].get("grp") or "", kv[0])):
        where = f"{v.get('street', '')}, {v.get('house', '')}" if v.get("street") else ""
        tag = f"{v['provider']} {v['grp']}" if v.get("grp") else f"— {v.get('why')}"
        print(f"{tag:42} {v['address']}  [{where}]")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main("--all" in sys.argv))
