#!/bin/sh
# Оновлення бота на сервері (викликає deploy/deploy.sh з ПК після копіювання коду; можна й вручну):
#   1) бекап БД (останні 7 у /opt/svitlo-backup);
#   2) базовий образ (Python, Chromium, залежності) — лише якщо змінились requirements.txt / Dockerfile.base;
#   3) образ коду поверх базового (без мережі), перезапуск bot, потім userbot; чекаємо «Start polling».
set -e
cd "$(dirname "$0")/.."
BASE=svitlo-dnipro-bot-base:latest
BACKUPS=/opt/svitlo-backup

mkdir -p "$BACKUPS"
if [ -f data/svitlo.db ]; then
    out="$BACKUPS/svitlo-$(date +%F-%H%M).db"
    if command -v sqlite3 >/dev/null 2>&1; then sqlite3 data/svitlo.db ".backup '$out'"
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c "import sqlite3,sys; sqlite3.connect('data/svitlo.db').backup(sqlite3.connect(sys.argv[1]))" "$out"
    else cp data/svitlo.db "$out"; fi
    ls -1t "$BACKUPS"/svitlo-*.db | tail -n +8 | xargs -r rm -f
    echo "бекап: $out"
fi

REQ=$(cat requirements.txt Dockerfile.base | sha1sum | cut -c1-12)
if ! docker image inspect "$BASE" >/dev/null 2>&1 && docker image inspect svitlo-dnipro-bot-bot:latest >/dev/null 2>&1 \
        && [ ! -f .base_req ]; then
    # перший запуск нової схеми: поточний образ бота вже має Python, Chromium і залежності — беремо його за базовий
    docker tag svitlo-dnipro-bot-bot:latest "$BASE"
    echo "$REQ" > .base_req
    echo "базовий образ: взято поточний образ бота"
fi
if ! docker image inspect "$BASE" >/dev/null 2>&1 || [ "$(cat .base_req 2>/dev/null)" != "$REQ" ]; then
    echo "базовий образ: збираю (змінились залежності)"
    docker build -f Dockerfile.base -t "$BASE" .
    echo "$REQ" > .base_req
fi

docker compose build bot
docker compose up -d bot
if docker compose --profile userbot ps --services --status running 2>/dev/null | grep -qx userbot \
        || docker compose --profile userbot ps -a --services 2>/dev/null | grep -qx userbot; then
    docker compose --profile userbot build userbot
    docker compose --profile userbot up -d userbot
fi
docker image prune -f >/dev/null

i=0
until docker compose logs --since 2m bot 2>&1 | grep -q "Start polling"; do
    i=$((i + 1))
    [ $i -gt 30 ] && { echo "бот не стартував за 60 с:"; docker compose logs --tail 30 bot; exit 1; }
    sleep 2
done
docker compose --profile userbot ps --format "{{.Service}} {{.Status}}"
echo "готово: $(cat REVISION 2>/dev/null || echo '?')"
