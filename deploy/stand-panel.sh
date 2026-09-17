#!/usr/bin/env bash
# Панель управления на самом стенде — запасной вход, когда интернета нет.
#
#   deploy/stand-panel.sh            панель на весь экран
#   deploy/stand-panel.sh --screen   только видео (для второго монитора)
#   deploy/stand-panel.sh --url      напечатать адрес и выйти
#   deploy/stand-panel.sh --force    открыть, даже если робот неисправен
#
# ЗАЧЕМ. Публичный доступ живёт через сотовый модем, а модем теряет связь:
# ушла сота, кончился баланс, перегрелся. Стенд от этого не ломается — сайт
# раздаёт его собственный nginx, и всё, что нужно посетителю, есть локально.
# Этот скрипт открывает ту же самую панель на экране стенда, чтобы
# мероприятие продолжалось, пока связь восстанавливается.
#
# Раскладка на широком экране своя: ползунки слева, рука справа, ничего не
# скроллится. Это та же страница, что и на телефоне, — не копия, которая
# начнёт отставать от оригинала.

set -euo pipefail

URL_BASE="http://localhost:8080"
PANEL="${URL_BASE}/control?video=1"     # video=1: поток локальный, он бесплатен
SCREEN="${URL_BASE}/screen/"

target="$PANEL"
force=0
for arg in "$@"; do
    case "$arg" in
        --screen) target="$SCREEN" ;;
        --force)  force=1 ;;
        --url)    echo "$PANEL"; exit 0 ;;
        "")       ;;
        *) echo "Неизвестный аргумент: $arg" >&2; sed -n '2,8p' "$0" | sed 's/^# \?//' >&2; exit 1 ;;
    esac
done

# Две РАЗНЫЕ беды, и советы у них противоположные:
#   нет соединения — стек не поднят, надо поднимать;
#   HTTP 503      — стек на месте и честно докладывает, что сломана рука.
# Старая проверка (`curl -fsS`) валила их в одну кучу и в обоих случаях
# советовала `up -d`. На стенде с поднятым стеком это тупик: команду выполняешь,
# ничего не меняется, и непонятно, куда смотреть.
body=$(mktemp)
trap 'rm -f "$body"' EXIT
code=$(curl -s --max-time 3 -o "$body" -w '%{http_code}' "${URL_BASE}/api/health" 2>/dev/null) || true
[ -z "$code" ] && code=000

if [ "$code" = "000" ]; then
    echo "❌ Стенд не отвечает на ${URL_BASE}/api/health — соединения нет."
    echo "   Стек не поднят. Поднимите его:"
    echo "       deploy/stand.sh up"
    exit 1
elif [ "$code" != "200" ]; then
    reason=$(sed -n 's/.*"reason"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$body")
    echo "⚠️  Стек поднят и отвечает (HTTP $code), но сам сообщает о неисправности:"
    echo "       ${reason:-причина не указана}"
    echo
    echo "   Поднимать стек заново НЕ нужно — он уже работает. Смотреть надо руку:"
    echo "       docker logs so101-ros | grep -iE 'timeout|Deactivating'"
    echo "       deploy/stand.sh stop ros                            # освободить порт"
    echo "       deploy/.venv/bin/python deploy/servo_scan.py         # жива ли шина"
    echo
    if [ "$force" != "1" ]; then
        echo "   Панель покажет «Робот остановлен» и управляться не будет."
        echo "   Открыть её всё равно (например, показать посетителям): --force"
        exit 1
    fi
    echo "   --force: открываю несмотря на неисправность."
fi

# Киоск: без адресной строки и вкладок, чтобы посетитель не ушёл со страницы.
for b in chromium chromium-browser google-chrome google-chrome-stable; do
    if command -v "$b" >/dev/null; then
        echo "  Открываю панель в $b. Выход — Alt+F4."
        exec "$b" --kiosk --no-first-run --disable-translate \
                  --disable-features=TranslateUI \
                  --autoplay-policy=no-user-gesture-required "$target"
    fi
done

if command -v firefox >/dev/null; then
    echo "  Открываю панель в firefox (полноэкранный режим — F11)."
    exec firefox --new-window "$target"
fi

echo "⚠️  Браузер не найден. Откройте вручную:"
echo "      $target"
