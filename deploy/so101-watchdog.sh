#!/usr/bin/env bash
# Сторож туннеля SO-101. Лечит протухший WireGuard, не трогая выключенный
# доступ: если интерфейса so101 нет — стенд намеренно закрыт, ничего не делаем.
set -u
IFACE=so101
MAX_AGE=120
WG_BIN=/usr/bin/wg

[ -x "$WG_BIN" ] || exit 0
if ! "$WG_BIN" show interfaces 2>/dev/null | grep -qx "$IFACE"; then
    # Интерфейса нет: доступ выключен намеренно. Единственное исключение —
    # TUNNEL_AUTO=1 в /etc/so101.env (стенд без присмотра, см. runbook).
    if [ "${TUNNEL_AUTO:-0}" = "1" ] && [ -f /etc/wireguard/${IFACE}.conf ]; then
        logger -t so101-watchdog "TUNNEL_AUTO: поднимаю туннель"
        /home/ubuntu/Desktop/robotic_arm/deploy/public.sh on >/dev/null 2>&1
    fi
    exit 0
fi

hs=$("$WG_BIN" show "$IFACE" latest-handshakes 2>/dev/null | awk '{print $2; exit}')
now=$(date +%s)
age=$(( now - ${hs:-0} ))
if [ -z "$hs" ] || [ "$age" -gt "$MAX_AGE" ]; then
    logger -t so101-watchdog "рукопожатие протухло (${age:-нет} с) — перезапускаю туннель"
    /home/ubuntu/Desktop/robotic_arm/deploy/public.sh off >/dev/null 2>&1
    sleep 2
    /home/ubuntu/Desktop/robotic_arm/deploy/public.sh on >/dev/null 2>&1
fi
exit 0
