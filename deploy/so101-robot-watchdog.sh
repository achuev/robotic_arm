#!/usr/bin/env bash
# Сторож робота SO-101. Шина Feetech нередко зависает (read timeout) — стек
# остаётся жив, но рука в error и не слушается. Этот сторож замечает и
# перезапускает ros-контейнер (драйвер переподключается) и шлюз (сброс
# состояния error). Не более одного рестарта в 3 минуты, чтобы не гонять
# кругами при настоящей поломке.
set -u
STAMP=/run/so101-robot-watchdog.last
COOLDOWN=180

now=$(date +%s)
last=$(cat "$STAMP" 2>/dev/null || echo 0)
[ $(( now - last )) -lt "$COOLDOWN" ] && exit 0

status=$(curl -s --max-time 5 http://localhost:8080/api/status || echo '{}')
ros_h=$({ docker inspect -f '{{.State.Health.Status}}' so101-ros 2>/dev/null || echo missing; } )
gw_h=$(  { docker inspect -f '{{.State.Health.Status}}' so101-gateway 2>/dev/null || echo missing; } )

need=0
echo "$status" | grep -q '"robot":"error"' && need=1
[ "$ros_h" != "healthy" ] && need=1

if [ "$need" = "1" ]; then
    echo "$now" > "$STAMP"
    logger -t so101-robot-watchdog "робот в ошибке/недоступен (ros=$ros_h gw=$gw_h) — перезапускаю ros+gateway"
    docker restart so101-ros >/dev/null 2>&1
    sleep 45
    docker restart so101-gateway >/dev/null 2>&1
fi
exit 0
