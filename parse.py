"""Розбір повідомлень із чатів: є/нема світла + район. Без LLM, на регулярках."""
import re

from config import DISTRICTS

LIGHT = re.compile(r"світл|свiтл|свет|електр|электр|дтек|ясно|черг|очеред|груп")
ON = re.compile(
    r"\bдали\b|є світл|есть свет|світло є|свет есть|включили|увімкнули|ввімкнули|вкл[юі]чили|"
    r"з.явилось|з.явилося|появил|дали світ|дали свет"
)
OFF = re.compile(
    r"вимкнули|виключили|выключили|вирубили|вырубили|нема світл|немає світл|нет света|"
    r"відключили|отключили|пропал|зникло|без світла|без света"
)
# Воєнна цензура: такі повідомлення не зберігаємо взагалі
CENSOR = re.compile(
    r"прил[еіi]т|приліт|вибух|взрыв|ппо|пво|ракет|шахед|дрон|бпла|збил|збит|сбил|сбит|уламк|обломк|"
    r"тривог|тревог|пуск|летить|летит"
)


def classify(text: str) -> tuple[str, str | None] | None:
    """Повертає (event, district) або None якщо повідомлення нерелевантне."""
    t = text.lower().replace("’", "'").replace("`", "'")
    if len(t) > 500 or CENSOR.search(t) or not LIGHT.search(t):
        return None
    on, off = bool(ON.search(t)), bool(OFF.search(t))
    if on == off:  # нічого або суперечливо
        return None
    district = next((name for name, keys in DISTRICTS.items() if any(k in t for k in keys)), None)
    return ("on" if on else "off"), district
