#!/usr/bin/env bash
# Развёртывание сервера-ретранслятора SO-101 на чистой Ubuntu 24.04.
#
# Запускается ОДИН РАЗ на арендованном сервере, от root:
#
#   ./provision.sh --domain arm.example.ru \
#                  --stand-pubkey <ключ со стенда> \
#                  --email you@example.com
#
# Публичный ключ стенда берётся из `deploy/public.sh init`, который вы
# запускаете на машине стенда ДО этого скрипта.
#
# ЧТО ЗДЕСЬ ПОЯВИТСЯ
#
#   WireGuard   туннель до стенда. Сервер только ЖДЁТ подключения: инициирует
#               его стенд, потому что стоит за NAT в зале и своего публичного
#               адреса не имеет.
#   Caddy       TLS и проксирование. Сертификат Let's Encrypt выписывается и
#               продлевается сам, ничего настраивать не нужно.
#   ufw         снаружи открыты только 22 (SSH), 80/443 (сайт) и 51820/udp
#               (туннель). Остальное закрыто.
#
# ЕСЛИ ЗАЛ СИДИТ НА СОТОВОМ МОДЕМЕ. Два необязательных параметра:
#   --mtu N     размер пакета в туннеле; замеряется на стенде `public.sh mtu`
#   --port 443  если оператор режет UDP на нестандартных портах. Тогда HTTP/3
#               выключается сам — этот порт уходит туннелю.
#
# ЧЕГО ЗДЕСЬ НЕТ И ПОЧЕМУ. Ни ROS, ни шлюза, ни драйвера — они обязаны быть на
# машине, к которой рука подключена по USB. Сервер только пробрасывает трафик.
# Из этого следует главное свойство: пока туннель опущен, сервер физически не
# имеет маршрута до стенда, и «выключить публичный доступ» — не настройка, а
# отсутствие связи.
#
# Скрипт можно перезапускать: ключи и сертификаты не перевыпускаются, меняются
# только конфиги.

set -euo pipefail

DOMAIN=""
STAND_PUBKEY=""
ACME_EMAIL=""
WG_PORT=51820
WG_MTU=1280
MTU_GIVEN=0
PORT_GIVEN=0
WG_NET=10.8.0
SERVER_IP="${WG_NET}.1"
STAND_IP="${WG_NET}.2"

die() { echo "❌ $*" >&2; exit 1; }
say() { echo "  $*"; }
step() { echo; echo "── $* ────────────────────────────────────────"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --domain)       DOMAIN="$2"; shift 2 ;;
        --stand-pubkey) STAND_PUBKEY="$2"; shift 2 ;;
        --email)        ACME_EMAIL="$2"; shift 2 ;;
        --port)         WG_PORT="$2"; PORT_GIVEN=1; shift 2 ;;
        --mtu)          WG_MTU="$2"; MTU_GIVEN=1; shift 2 ;;
        -h|--help)      sed -n '2,40p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) die "неизвестный аргумент: $1" ;;
    esac
done

[ "$(id -u)" -eq 0 ] || die "запускайте от root: sudo ./provision.sh ..."

# Повторный запуск: недостающее берём из уже развёрнутой конфигурации. Скрипт
# и раньше был идемпотентным, но требовал заново передать ключ стенда — а
# типичный повторный запуск делается ради ОДНОГО параметра: приехал модем,
# замер показал другой MTU, оператор режет порт. Искать ради этого ключ в
# переписке незачем.
if [ -f /etc/wireguard/wg0.conf ]; then
    [ -n "$STAND_PUBKEY" ] || STAND_PUBKEY="$(awk '/^PublicKey/{print $3; exit}' /etc/wireguard/wg0.conf)"
    [ "$MTU_GIVEN" = "1" ] || WG_MTU="$(awk '/^MTU/{print $3; exit}' /etc/wireguard/wg0.conf || true)"
    [ -n "$WG_MTU" ] || WG_MTU=1280
    [ "$PORT_GIVEN" = "1" ] || WG_PORT="$(awk '/^ListenPort/{print $3; exit}' /etc/wireguard/wg0.conf)"
fi
if [ -z "$DOMAIN" ] && [ -f /etc/caddy/so101.env ]; then
    prev_site="$(awk -F= '/^SO101_SITE=/{print $2; exit}' /etc/caddy/so101.env)"
    case "$prev_site" in
        :*|"") ;;                       # был режим «голый IP» — так и оставляем
        *) DOMAIN="$prev_site"; say "домен взят из прежней настройки: $DOMAIN" ;;
    esac
fi

[ -n "$STAND_PUBKEY" ] || die "нужен --stand-pubkey (возьмите из 'deploy/public.sh init' на стенде)"

# Домен необязателен: без него сайт поднимется по HTTP на голом IP. Так можно
# проверить связь до покупки домена, а потом перезапустить скрипт с --domain.
if [ -n "$DOMAIN" ]; then
    SITE="$DOMAIN"
    say "сайт: https://$DOMAIN (сертификат выпишется автоматически)"
else
    SITE=":80"
    echo "⚠️  Домен не задан: сайт будет работать по HTTP на голом IP."
    echo "    Браузеры покажут «Не защищено». Купите домен, пропишите A-запись"
    echo "    на этот сервер и перезапустите скрипт с --domain."
fi

step "1. Пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq wireguard wireguard-tools ufw curl ca-certificates \
                       debian-keyring debian-archive-keyring apt-transport-https
say "wireguard, ufw — готово"

# Caddy ставится из своего репозитория: в Ubuntu его либо нет, либо версия
# старая, а нам нужен `file_server { status }` (Caddy 2.7+).
if ! command -v caddy >/dev/null; then
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq
    apt-get install -y -qq caddy
fi
say "caddy $(caddy version | head -1)"

step "2. Ключи WireGuard"
umask 077
mkdir -p /etc/wireguard
if [ ! -f /etc/wireguard/server.key ]; then
    wg genkey > /etc/wireguard/server.key
    wg pubkey < /etc/wireguard/server.key > /etc/wireguard/server.pub
    say "ключевая пара создана"
else
    say "ключевая пара уже была — оставляю (иначе стенд перестанет подключаться)"
fi
SERVER_PUBKEY="$(cat /etc/wireguard/server.pub)"

step "3. Туннель"
cat > /etc/wireguard/wg0.conf <<EOF
# Ретранслятор SO-101. Сервер только ждёт: подключается стенд.
[Interface]
Address = ${SERVER_IP}/24
ListenPort = ${WG_PORT}
PrivateKey = $(cat /etc/wireguard/server.key)
# Должен совпадать с MTU на стенде. Определяющая сторона — канал зала: если
# он сотовый, полтора килобайта туда не влезают, и слишком большие пакеты
# теряются молча. Замерить: `deploy/public.sh mtu` на стенде.
MTU = ${WG_MTU}

# Стенд. AllowedIPs строго один адрес: через туннель ходит только он,
# маршрут по умолчанию не трогается.
[Peer]
PublicKey = ${STAND_PUBKEY}
AllowedIPs = ${STAND_IP}/32
EOF
chmod 600 /etc/wireguard/wg0.conf
systemctl enable --now wg-quick@wg0 >/dev/null 2>&1 || systemctl restart wg-quick@wg0
say "wg0 поднят на ${SERVER_IP}, порт ${WG_PORT}/udp, MTU ${WG_MTU}"

step "4. Caddy"
mkdir -p /var/www/so101 /etc/caddy
install -m 644 "$(dirname "$0")/offline.html" /var/www/so101/offline.html
install -m 644 "$(dirname "$0")/Caddyfile" /etc/caddy/Caddyfile

# Если туннель забрал 443/udp, HTTP/3 придётся выключить: этот порт занят
# WireGuard, и Caddy на нём не поднимется. Потеря невелика — HTTP/2 остаётся,
# а возможность пройти сквозь сеть, где открыт только 443, важнее.
if [ "$WG_PORT" = "443" ]; then
    HTTP_PROTOCOLS="h1 h2"
    say "порт 443/udp отдан туннелю → HTTP/3 выключен"
else
    HTTP_PROTOCOLS="h1 h2 h3"
fi

cat > /etc/caddy/so101.env <<EOF
SO101_SITE=${SITE}
SO101_UPSTREAM=${STAND_IP}:8080
SO101_ACME_EMAIL=${ACME_EMAIL:-admin@${DOMAIN:-example.com}}
SO101_HTTP_PROTOCOLS=${HTTP_PROTOCOLS}
EOF
chmod 600 /etc/caddy/so101.env

# Штатный юнит Caddy не читает переменные окружения — доучиваем через drop-in,
# чтобы не править сам юнит (его перезапишет обновление пакета).
mkdir -p /etc/systemd/system/caddy.service.d
cat > /etc/systemd/system/caddy.service.d/so101.conf <<'EOF'
[Service]
EnvironmentFile=/etc/caddy/so101.env
EOF
systemctl daemon-reload
caddy validate --config /etc/caddy/Caddyfile --envfile /etc/caddy/so101.env >/dev/null \
    || die "Caddyfile не проходит проверку"
systemctl enable caddy >/dev/null 2>&1 || true
systemctl restart caddy
say "caddy перезапущен"

step "5. Межсетевой экран"
ufw --force reset >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp    comment 'SSH' >/dev/null
ufw allow 80/tcp    comment 'HTTP (перенаправление и выпуск сертификата)' >/dev/null
ufw allow 443/tcp   comment 'HTTPS' >/dev/null
ufw allow 443/udp   comment 'HTTP/3' >/dev/null
ufw allow ${WG_PORT}/udp comment 'WireGuard: туннель до стенда' >/dev/null
ufw --force enable >/dev/null
say "открыты: 22, 80, 443 (tcp+udp), ${WG_PORT}/udp"

step "Готово"
PUBLIC_IP="$(curl -s --max-time 5 https://api.ipify.org || hostname -I | awk '{print $1}')"
cat <<EOF

Сервер настроен. Теперь ОДНА команда на машине стенда:

    deploy/public.sh configure \\
        --server-pubkey ${SERVER_PUBKEY} \\
        --endpoint ${PUBLIC_IP}:${WG_PORT} \\
        --mtu ${WG_MTU}${DOMAIN:+ \\
        --domain ${DOMAIN}}

После неё публичный доступ включается и выключается так:

    deploy/public.sh on       перед мероприятием
    deploy/public.sh off      после
    deploy/public.sh status   посмотреть, что сейчас

EOF
if [ -n "$DOMAIN" ]; then
cat <<EOF
ПЕРЕД ЭТИМ убедитесь, что A-запись домена ${DOMAIN} указывает на ${PUBLIC_IP}.
Сертификат выпишется при первом обращении; пока запись не разошлась по DNS,
Let's Encrypt будет отказывать, и сайт отдаст ошибку TLS.

    dig +short ${DOMAIN}     должно вернуть ${PUBLIC_IP}

EOF
fi
