#!/usr/bin/env bash
# Обёртка над docker compose для боевого стенда.
#
#   deploy/stand.sh up            поднять стек целиком
#   deploy/stand.sh down          погасить стек целиком
#   deploy/stand.sh restart       down + up (см. ниже, почему не `compose restart`)
#   deploy/stand.sh status        контейнеры и /api/health одним взглядом
#   deploy/stand.sh logs -f ros   всё прочее уходит в compose как есть
#
# ЗАЧЕМ. Две вещи, на которых легко обжечься руками.
#
# 1. ТОКЕН. `compose.prod.yml` объявляет ADMIN_TOKEN обязательным
#    (`${ADMIN_TOKEN:?}`), и без него падает ЛЮБАЯ команда compose, включая
#    безобидные `ps` и `logs`. Лежит он в /etc/so101.env с правами 600 root,
#    так что каждый раз приходится вспоминать заклинание с sudo. Скрипт
#    достаёт токен сам.
#
# 2. СТЕК ЦЕЛИКОМ. Четыре контейнера живут в сетевом пространстве контейнера
#    `ros`. Перезапуск одного `ros` оставляет соседей в уже уничтоженном
#    namespace: снаружи 502, docker при этом показывает `running` и сам это
#    не чинит (docs/runbook.md, «Симптом → причина»). Поэтому `restart` здесь
#    — это down + up, а не `compose restart`.
set -euo pipefail

cd "$(dirname "$0")/.."
COMPOSE=(docker compose -f deploy/compose.prod.yml)
ENV_FILE=/etc/so101.env

# Подсказка не должна требовать ни токена, ни sudo.
case "${1:-}" in
    ""|-h|--help) sed -n '2,7p' "$0" | sed 's/^# \?//'; exit 0 ;;
esac

if [ -z "${ADMIN_TOKEN:-}" ]; then
    if [ -r "$ENV_FILE" ]; then
        ADMIN_TOKEN=$(sed -n 's/^ADMIN_TOKEN=//p' "$ENV_FILE")
    elif [ -e "$ENV_FILE" ] && [ "${1:-}" != "down" ]; then
        # Не для `down`: гасить стенд нужно и тогда, когда sudo недоступен.
        # `|| true` — чтобы отказ sudo не убил скрипт через set -e, а дошёл
        # до внятного сообщения ниже.
        echo "  $ENV_FILE читается только root — запрашиваю sudo для токена." >&2
        ADMIN_TOKEN=$(sudo sed -n 's/^ADMIN_TOKEN=//p' "$ENV_FILE" || true)
    fi
fi

# `down` обязан работать всегда: это аварийная команда, и упереться в ненайденный
# токен в момент, когда стенд надо погасить, — худшее, что может сделать обёртка.
# Интерполяции достаточно любого непустого значения: `down` ничего не запускает.
if [ -z "${ADMIN_TOKEN:-}" ] && [ "${1:-}" = "down" ]; then
    echo "  ADMIN_TOKEN не найден, но для остановки он не нужен — гашу." >&2
    ADMIN_TOKEN=unused-for-down
fi

if [ -z "${ADMIN_TOKEN:-}" ]; then
    echo "❌ ADMIN_TOKEN не найден: ни в окружении, ни в $ENV_FILE" >&2
    echo "   Завести (боевой стенд без него не поднимется):" >&2
    echo "       sudo sh -c 'echo ADMIN_TOKEN=\$(openssl rand -hex 24) > $ENV_FILE'" >&2
    echo "       sudo chmod 600 $ENV_FILE" >&2
    echo "   В deploy/.env класть НЕЛЬЗЯ — файл отслеживается гитом." >&2
    exit 1
fi
export ADMIN_TOKEN

case "${1:-}" in
    up)
        # --remove-orphans: после смены профилей остаются лишние контейнеры,
        # которые держат порты и сбивают с толку.
        shift; exec "${COMPOSE[@]}" up -d --remove-orphans "$@"
        ;;
    down)
        shift; exec "${COMPOSE[@]}" down "$@"
        ;;
    restart)
        shift
        "${COMPOSE[@]}" down
        exec "${COMPOSE[@]}" up -d --remove-orphans "$@"
        ;;
    status)
        "${COMPOSE[@]}" ps
        echo
        code=$(curl -s --max-time 3 -o /tmp/so101-health.$$ -w '%{http_code}' \
               http://localhost:8080/api/health 2>/dev/null) || true
        [ -z "${code:-}" ] && code=000
        if [ "$code" = "000" ]; then
            echo "/api/health: соединения нет — стек не поднят"
        else
            echo "/api/health: HTTP $code $(cat /tmp/so101-health.$$ 2>/dev/null)"
        fi
        rm -f /tmp/so101-health.$$
        ;;
    *)
        exec "${COMPOSE[@]}" "$@"
        ;;
esac
