#!/usr/bin/env bash
# Публичный доступ к стенду: включить, выключить, посмотреть состояние.
#
#   deploy/public.sh init        один раз: создать ключи стенда
#   deploy/public.sh configure   один раз: прописать адрес сервера
#   deploy/public.sh on          перед мероприятием
#   deploy/public.sh off         после
#   deploy/public.sh status      что сейчас
#   deploy/public.sh mtu         замерить канал зала (важно для модема)
#   deploy/public.sh qr          табличка с QR на публичный адрес
#
# КАК ЭТО УСТРОЕНО. Стенд стоит в зале за NAT и своего публичного адреса не
# имеет, поэтому связь устанавливает ОН: поднимает WireGuard до арендованного
# сервера, а тот проксирует на него посетителей.
#
# Отсюда главное свойство: `off` не «запрещает» доступ настройкой, а убирает
# маршрут. Сервер после этого физически не может достучаться до стенда и
# показывает посетителям страницу «стенд сейчас выключен». Ошибиться в
# конфигурации и случайно оставить руку открытой миру нельзя — если туннеля
# нет, то нет и доступа.
#
# Рука при выключении доступа не трогается: `off` рвёт связь с интернетом, а
# не останавливает стенд. Из локальной сети он продолжает работать.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IFACE=so101                     # имя интерфейса; wg0 намеренно не занимаем
CONF=/etc/wireguard/${IFACE}.conf
KEYDIR=/etc/wireguard
STATE="$REPO/deploy/.public-access"   # домен и точка входа, для qr и status

die() { echo "❌ $*" >&2; exit 1; }
say() { echo "  $*"; }

need_root() {
    [ "$(id -u)" -eq 0 ] || die "нужны права root: sudo $0 $*"
}

need_wg() {
    command -v wg >/dev/null || die "WireGuard не установлен: sudo apt install wireguard"
}

# --------------------------------------------------------------------- init
cmd_init() {
    need_root init; need_wg
    umask 077
    mkdir -p "$KEYDIR"
    if [ -f "$KEYDIR/stand.key" ]; then
        say "ключи уже есть — оставляю (пересоздать значит перенастраивать сервер)"
    else
        wg genkey > "$KEYDIR/stand.key"
        wg pubkey < "$KEYDIR/stand.key" > "$KEYDIR/stand.pub"
        say "ключевая пара стенда создана"
    fi
    echo
    echo "════════ ПУБЛИЧНЫЙ КЛЮЧ СТЕНДА ════════"
    cat "$KEYDIR/stand.pub"
    echo "═══════════════════════════════════════"
    echo
    echo "Передайте его серверу:"
    echo "    sudo ./provision.sh --domain <ваш домен> --stand-pubkey $(cat "$KEYDIR/stand.pub")"
}

# ---------------------------------------------------------------- configure
cmd_configure() {
    need_root configure; need_wg
    local server_pubkey="" endpoint="" domain="" stand_ip=10.8.0.2 server_ip=10.8.0.1
    # 1280 — минимальный MTU, который обязана пропускать любая IPv6-сеть, и на
    # практике проходит везде. Значение по умолчанию именно такое, потому что
    # цена ошибки несимметрична: лишние 10% накладных расходов против «туннель
    # поднялся, рукопожатие есть, а сайт не открывается». См. `$0 mtu`.
    local mtu=1280
    while [ $# -gt 0 ]; do
        case "$1" in
            --server-pubkey) server_pubkey="$2"; shift 2 ;;
            --endpoint)      endpoint="$2"; shift 2 ;;
            --domain)        domain="$2"; shift 2 ;;
            --mtu)           mtu="$2"; shift 2 ;;
            *) die "неизвестный аргумент: $1" ;;
        esac
    done
    [ -n "$server_pubkey" ] || die "нужен --server-pubkey (его печатает provision.sh)"
    [ -n "$endpoint" ] || die "нужен --endpoint вида 1.2.3.4:51820"
    [ -f "$KEYDIR/stand.key" ] || die "сначала: sudo $0 init"

    umask 077
    cat > "$CONF" <<EOF
# Туннель до ретранслятора SO-101. Поднимается стендом.
[Interface]
Address = ${stand_ip}/24
PrivateKey = $(cat "$KEYDIR/stand.key")
# Сотовый модем добавляет свою инкапсуляцию, и полтора килобайта в него часто
# не влезают. Пакет с установленным DF молча теряется — а рукопожатие
# WireGuard короткое и проходит. Получается худший вид поломки: связь «есть»,
# но ничего не работает. Отсюда заниженный MTU.
MTU = ${mtu}

[Peer]
PublicKey = ${server_pubkey}
Endpoint = ${endpoint}
# ТОЛЬКО адрес сервера. Маршрут по умолчанию не трогаем: иначе весь трафик
# стенда, включая ROS и обновления, пошёл бы через арендованный сервер.
AllowedIPs = ${server_ip}/32
# Стенд за NAT: без периодических пакетов запись в таблице трансляции
# протухает через пару минут, и сервер перестаёт до него дозваниваться.
PersistentKeepalive = 25
EOF
    chmod 600 "$CONF"

    printf 'ENDPOINT=%s\nDOMAIN=%s\n' "$endpoint" "$domain" > "$STATE"
    chmod 644 "$STATE"

    say "туннель настроен на ${endpoint}, MTU ${mtu}"
    [ -n "$domain" ] && say "публичный адрес: https://${domain}"
    echo
    echo "Включить публичный доступ: sudo $0 on"
}

# ----------------------------------------------------------------- on / off
cmd_on() {
    need_root on; need_wg
    [ -f "$CONF" ] || die "туннель не настроен: sudo $0 configure ..."

    # Стенд должен быть поднят: иначе посетители придут на страницу «выключен»,
    # а вы будете думать, что открыли доступ.
    if ! curl -fsS --max-time 3 http://localhost:8080/api/health >/dev/null 2>&1; then
        echo "⚠️  Стенд не отвечает на http://localhost:8080/api/health"
        echo "    Публичный доступ откроется, но посетители увидят «стенд выключен»."
        echo "    Поднимите стенд: docker compose -f deploy/compose.prod.yml up -d"
        echo
    fi

    wg-quick up "$IFACE" 2>&1 | sed 's/^/    /' || die "не поднялся туннель"
    sleep 2
    cmd_status
}

cmd_off() {
    need_root off; need_wg
    if ip link show "$IFACE" >/dev/null 2>&1; then
        wg-quick down "$IFACE" 2>&1 | sed 's/^/    /'
        say "публичный доступ закрыт"
    else
        say "публичный доступ и так был закрыт"
    fi
    say "стенд продолжает работать в локальной сети"
}

# ------------------------------------------------------------------ status
cmd_status() {
    local domain="" endpoint=""
    [ -f "$STATE" ] && . "$STATE" 2>/dev/null || true

    echo
    if ! ip link show "$IFACE" >/dev/null 2>&1; then
        echo "  Публичный доступ: ЗАКРЫТ"
        echo "  Посетители по ссылке видят «стенд сейчас выключен»."
        [ -n "${DOMAIN:-}" ] && echo "  Открыть: sudo $0 on"
        return 0
    fi

    echo "  Публичный доступ: ОТКРЫТ"
    [ -n "${DOMAIN:-}" ] && echo "  Адрес: https://${DOMAIN}"

    # Рукопожатие — единственное надёжное доказательство, что связь есть.
    # Поднятый интерфейс сам по себе ничего не значит: он поднимется и в
    # пустоту, если сервер недоступен.
    local hs
    hs="$(wg show "$IFACE" latest-handshakes 2>/dev/null | awk '{print $2}' | head -1)"
    if [ -n "$hs" ] && [ "$hs" != "0" ]; then
        local ago=$(( $(date +%s) - hs ))
        if [ "$ago" -lt 180 ]; then
            echo "  Связь с сервером: есть (рукопожатие $ago с назад)"
        else
            echo "  Связь с сервером: ⚠️ рукопожатия не было $ago с — проверьте сеть зала"
        fi
    else
        echo "  Связь с сервером: ⚠️ рукопожатия ещё не было"
        echo "    Обычно это закрытый исходящий UDP в сети зала либо неверный --endpoint."
    fi

    wg show "$IFACE" transfer 2>/dev/null | awk '{printf "  Передано: %s принято, %s отправлено\n", $2, $3}'

    if curl -fsS --max-time 3 http://localhost:8080/api/health >/dev/null 2>&1; then
        echo "  Стенд: работает"
    else
        echo "  Стенд: ⚠️ не отвечает — посетители увидят «стенд выключен»"
    fi
    echo
}

# ---------------------------------------------------------------------- qr
cmd_qr() {
    [ -f "$STATE" ] && . "$STATE" 2>/dev/null || true
    [ -n "${DOMAIN:-}" ] || die "домен неизвестен: настройте через 'configure --domain ...'"
    local out="${1:-tablet-public.pdf}"
    "$REPO/deploy/.venv/bin/python" "$REPO/deploy/qr.py" "https://${DOMAIN}" -o "$out"
    echo
    echo "  Перед печатью проверьте код своим телефоном — камерой, а не глазами."
}

# --------------------------------------------------------------------- mtu
# Ищет самый большой пакет, который канал пропускает целиком, и считает от
# него MTU туннеля.
#
# Зачем отдельная команда. На сотовом модеме путь до сервера почти никогда не
# держит стандартные 1500 байт: оператор заворачивает трафик в свой туннель и
# отъедает десятки байт. Обычный ping этого не показывает — он шлёт короткие
# пакеты. Нужен ping с запретом фрагментации (-M do): такой пакет, если не
# влезает, не режется, а отбрасывается. Именно так теряется видео, пока
# рукопожатие WireGuard (оно короткое) проходит без единой жалобы.
cmd_mtu() {
    [ -f "$STATE" ] && . "$STATE" 2>/dev/null || true
    local host="${1:-}"
    [ -n "$host" ] || host="${ENDPOINT%%:*}"
    [ -n "$host" ] || die "неизвестен адрес сервера: $0 mtu <IP сервера> либо сначала configure"

    echo
    echo "  Проверяю путь до ${host}. Это займёт несколько секунд."

    # Полезная нагрузка ICMP = размер IP-пакета минус 28 (20 IP + 8 ICMP).
    probe() { ping -M do -c 1 -W 2 -s $(( $1 - 28 )) "$host" >/dev/null 2>&1; }

    if ! ping -c 1 -W 2 "$host" >/dev/null 2>&1; then
        echo "  ⚠️ Сервер не отвечает на ping."
        echo "     Либо он недоступен, либо у него закрыт ICMP — тогда измерить"
        echo "     нельзя, оставляйте MTU 1280."
        return 1
    fi

    local lo=576 hi=1500
    if probe $hi; then
        echo "  Путь держит полный пакет 1500 байт — канал без лишней обёртки."
        lo=$hi
    else
        # Двоичный поиск: lo заведомо проходит, hi заведомо нет.
        local mid
        while [ $(( hi - lo )) -gt 1 ]; do
            mid=$(( (lo + hi) / 2 ))
            if probe $mid; then lo=$mid; else hi=$mid; fi
        done
        echo "  Наибольший проходящий пакет: ${lo} байт."
    fi

    # Накладные расходы WireGuard поверх IPv4: 20 IP + 8 UDP + 16 заголовок
    # + 16 метка подлинности = 60 байт.
    local rec=$(( lo - 60 ))
    echo
    echo "  Рекомендуемый MTU туннеля: ${rec}"
    if [ "$rec" -ge 1280 ]; then
        echo "  Значение по умолчанию (1280) безопасно и менять его незачем."
        echo "  Поднять до ${rec} можно ради ~$(( (rec - 1280) * 100 / 1280 ))% экономии — выигрыш невелик."
    else
        echo "  ⚠️ Это МЕНЬШЕ значения по умолчанию. Обязательно задайте явно:"
        echo "        sudo $0 configure ... --mtu ${rec}"
        echo "     И то же значение на сервере: ./provision.sh ... --mtu ${rec}"
    fi
    echo
}

case "${1:-}" in
    init)      shift; cmd_init "$@" ;;
    configure) shift; cmd_configure "$@" ;;
    on)        shift; cmd_on "$@" ;;
    off)       shift; cmd_off "$@" ;;
    status)    shift; cmd_status "$@" ;;
    mtu)       shift; cmd_mtu "$@" ;;
    qr)        shift; cmd_qr "$@" ;;
    *)
        sed -n '2,25p' "$0" | sed 's/^# \?//'
        exit 1 ;;
esac
