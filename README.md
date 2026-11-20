# Світло Дніпро — бот

1. `cp .env.example .env`, вписати `BOT_TOKEN` (для тесту меню без API: `SOURCE=demo`).
2. `pip install -r requirements.txt`
3. `python bot.py` — бот з меню і сповіщеннями.
4. (опційно) `python userbot.py` — читання чатів, окремий акаунт, потрібні `API_ID`, `API_HASH`, `CHATS`.

За замовчуванням (`SOURCE=dtek`) бот спочатку перевіряє сайт ДТЕК, далі шукає графік на YASNO. Графік ЦЕК бот бере з YASNO. З `SOURCE=yasno` бот звертається до YASNO для обох операторів. Якщо графіка на сайтах немає, бот перевіряє дані, збережені юзерботом з Telegram. [Графіки YASNO для Дніпра](https://static.yasno.ua/dnipro/outages).

Файли: `providers.py` (графіки), `parse.py` (розбір чатів + цензура),
`handlers.py`/`keyboards.py` (меню), `notifier.py` (пуші), `db.py` (SQLite).
