"""Черги (групи) відключень: будь-які коди формату X.Y — «1.1», «6.2», «14.1», «18.1» і далі.

- is_group — формат коду (для даних ДТЕК/ЦЕК/YASNO: там черга завжди явна, «GPV14.1»).
- known — реєстр черг, які бот уже бачив (типові 1.1–6.2 + усе, що прийшло з сайту ДТЕК, YASNO, довідника
  будинків). З нього будуються кнопки, і ним фільтруємо вільний текст з чатів/каналів, де «16.30» чи «20.11» —
  це час і дата, а не черга.
"""
import re

CODE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})$")

DEFAULT = [f"{q}.{s}" for q in range(1, 7) for s in (1, 2)]  # 1.1 … 6.2 — поки даних немає

_known: set[str] = set(DEFAULT)


def norm(code) -> str | None:
    """«14.1» / «GPV14.1» / «14.01» -> «14.1»; не черга -> None."""
    s = str(code or "").strip().upper()
    if s.startswith("GPV"):
        s = s[3:]
    m = CODE_RE.match(s)
    if not m:
        return None
    q, sub = int(m.group(1)), int(m.group(2))
    return f"{q}.{sub}" if q > 0 and sub > 0 else None


def is_group(code) -> bool:
    """Рівно «X.Y» у канонічному вигляді: «14.1» так; « 14.1», «14.01», «GPV14.1», «1,1» — ні."""
    return isinstance(code, str) and norm(code) == code


def sort_key(code: str):
    q, _, s = code.partition(".")
    return (int(q), int(s)) if q.isdigit() and s.isdigit() else (999, 999)


def sort(codes) -> list[str]:
    return sorted(set(codes), key=sort_key)


def register(codes) -> list[str]:
    """Нові черги з даних -> у реєстр. Повертає ті, яких ще не було."""
    new = []
    for c in codes:
        g = norm(c)
        if g and g not in _known:
            _known.add(g)
            new.append(g)
    return new


def known() -> list[str]:
    return sort(_known)


def is_known(code) -> bool:
    return code in _known


def from_dtek_reason(reason) -> str | None:
    """«GPV14.1» з sub_type_reason сайту ДТЕК -> «14.1». ЦЕК там «GPV1002.1» — це не черга ДТЕК."""
    m = re.fullmatch(r"GPV(\d{1,2}\.\d{1,2})", str(reason or "").strip(), re.I)
    return norm(m.group(1)) if m else None


def from_cek(text) -> str | None:
    """Поле «cek» сайту ДТЕК -> черга ЦЕК, якщо є («2.1», «GPV1002.1», «ЦЕК 14.1»). Формат поля невідомий."""
    t = str(text or "")
    if m := re.search(r"GPV100(\d{1,2})\.(\d{1,2})(?!\d)", t, re.I):
        return norm(f"{m.group(1)}.{m.group(2)}")
    m = re.search(r"(?<![\d.])(\d{1,2})\.(\d{1,2})(?![\d.])", t)
    return norm(f"{m.group(1)}.{m.group(2)}") if m else None


def queue_of(code: str) -> str:
    return code.partition(".")[0]


def reset():
    """Для тестів."""
    _known.clear()
    _known.update(DEFAULT)
