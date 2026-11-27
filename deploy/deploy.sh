#!/bin/sh
# Викатка з ПК однією командою (Git Bash):  sh deploy/deploy.sh
# Код — з останнього коміту (git archive HEAD), не з робочої папки; незакомічені зміни — зупиняємось.
# На сервері: deploy/server_update.sh (бекап БД, образ без мережі, перезапуск, перевірка старту).
# Налаштування: DEPLOY_HOST (aeza-vps), DEPLOY_KEY (~/.ssh/spotme_deploy), DEPLOY_DIR (/opt/svitlo-dnipro-bot).
set -e
cd "$(dirname "$0")/.."
HOST=${DEPLOY_HOST:-aeza-vps}
KEY=${DEPLOY_KEY:-$HOME/.ssh/spotme_deploy}
DIR=${DEPLOY_DIR:-/opt/svitlo-dnipro-bot}
SSH="ssh -o ConnectTimeout=15 -o IdentitiesOnly=yes -i $KEY $HOST"

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "Є незакомічені зміни — спершу commit (викатується останній коміт)."
    exit 1
fi
REV=$(git rev-parse --short HEAD)
echo "викатую: $(git log -1 --format='%h %s')"
git archive --format=tar HEAD | $SSH "tar -x -C $DIR && echo $REV > $DIR/REVISION"
$SSH "sh $DIR/deploy/server_update.sh"
