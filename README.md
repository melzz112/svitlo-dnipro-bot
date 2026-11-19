# Світло Дніпро — бот

1. `cp .env.example .env`, вписати `BOT_TOKEN` (для тесту меню без API: `SOURCE=demo`).
2. `pip install -r requirements.txt`
3. `python bot.py` — бот з меню і сповіщеннями.
4. (опційно) `python userbot.py` — читання чатів, окремий акаунт, потрібні `API_ID`, `API_HASH`, `CHATS`.

Файли: `providers.py` (графіки YASNO/demo), `parse.py` (розбір чатів + цензура),
`handlers.py`/`keyboards.py` (меню), `notifier.py` (пуші), `db.py` (SQLite).
