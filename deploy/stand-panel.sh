#!/usr/bin/env bash
# Панель управления на самом стенде — запасной вход, когда интернета нет.
#
#   deploy/stand-panel.sh            панель на весь экран
#   deploy/stand-panel.sh --screen   только видео (для второго монитора)
#   deploy/stand-panel.sh --url      напечатать адрес и выйти
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
case "${1:-}" in
    --screen) target="$SCREEN" ;;
    --url)    echo "$PANEL"; exit 0 ;;
    "")       ;;
    *) echo "Неизвестный аргумент: $1" >&2; sed -n '2,8p' "$0" | sed 's/^# \?//' >&2; exit 1 ;;
esac

if ! curl -fsS --max-time 3 "${URL_BASE}/api/health" >/dev/null 2>&1; then
    echo "❌ Стенд не отвечает на ${URL_BASE}/api/health"
    echo "   Поднимите его: docker compose -f deploy/compose.prod.yml up -d"
    exit 1
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
