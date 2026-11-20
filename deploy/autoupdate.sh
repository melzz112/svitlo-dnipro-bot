#!/bin/sh
# Автооновлення бота: є нові коміти в гілці на GitHub — тягнемо і перезбираємо. Нічого нового — тихо виходимо.
# Ставиться один раз (з папки бота):
#   (crontab -l 2>/dev/null; echo "*/2 * * * * $(pwd)/deploy/autoupdate.sh >> $(pwd)/autoupdate.log 2>&1") | crontab -
# Лог: tail autoupdate.log      Зняти: crontab -e і видалити рядок
cd "$(dirname "$0")/.." || exit 1
[ -f .autoupdate.lock ] && exit 0  # попереднє оновлення ще збирається
git fetch -q origin || exit 0      # GitHub недоступний — спробуємо за 2 хв
[ "$(git rev-parse HEAD)" = "$(git rev-parse '@{u}')" ] && exit 0
touch .autoupdate.lock
trap 'rm -f .autoupdate.lock' EXIT
echo "$(date '+%F %T') оновлення $(git rev-parse --short HEAD) -> $(git rev-parse --short '@{u}')"
git pull -q --ff-only || { echo "git pull не вдався (локальні зміни на сервері?)"; exit 1; }
docker compose up -d --build bot
if docker compose ps --services --status running | grep -qx userbot; then
    docker compose --profile userbot up -d --build userbot
fi
echo "$(date '+%F %T') готово: $(git log -1 --format='%h %s')"
