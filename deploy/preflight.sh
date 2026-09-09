#!/usr/bin/env bash
# Предполётная проверка стенда SO-101 перед первым запуском на железе.
#
#   deploy/preflight.sh
#
# Отвечает на один вопрос: можно ли уже запускать `hardware_type:=real`.
# Ничего не меняет и ничего не пишет в сервоприводы — только смотрит.
#
# Порядок проверок соответствует порядку, в котором всё ломается:
# устройство → права → порт → шина → калибровка → образ → фронтенд.
# Первая непройденная проверка обычно и есть причина; остальные всё равно
# выполняются, чтобы за один прогон увидеть всю картину.
#
# ЗАЧЕМ ОТДЕЛЬНО, А НЕ ПРОСТО ПОДНЯТЬ СТЕНД. Драйвер Feetech на недоступной
# шине не деградирует, а бросает исключение и роняет весь ros2_control_node
# (LibSerial::NotOpen). С `restart: unless-stopped` это выглядит как
# crash-loop контейнера, по которому не видно, что именно не так.

set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${SO101_PORT:-/dev/so101_follower}"

# Кто реально держит порт. Проверять «запущен ли контейнер so101-ros» мало:
# в mock-режиме устройство в него не проброшено, и шину он не занимает —
# получалось предупреждение там, где всё в порядке. Смотрим дескрипторы.
port_holders() {
    [ -e "$PORT" ] || return 0
    local target
    target=$(readlink -f "$PORT")
    for fd in /proc/[0-9]*/fd/*; do
        [ "$(readlink -f "$fd" 2>/dev/null)" = "$target" ] || continue
        echo "$fd" | cut -d/ -f3
    done 2>/dev/null | sort -u
}

pass=0; warn=0; fail=0
ok()   { echo "  ✅ $1"; pass=$((pass+1)); }
no()   { echo "  ❌ $1"; fail=$((fail+1)); }
hm()   { echo "  ⚠️  $1"; warn=$((warn+1)); }
head_() { echo; echo "── $1 ────────────────────────────────────────────"; }

echo "Предполётная проверка стенда SO-101"
echo "Репозиторий: $REPO"
echo "Порт руки:   $PORT"

# ---------------------------------------------------------------- устройство
head_ "1. Устройство"
if [ -e "$PORT" ]; then
    target=$(readlink -f "$PORT")
    ok "$PORT есть → $target"
    if [ "$target" = "$PORT" ]; then
        hm "это не симлинк. udev-правило не сработало; после передёргивания кабеля путь уедет"
    fi
else
    no "$PORT отсутствует"
    real_ports=$(ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null)
    if [ -n "$real_ports" ]; then
        echo "     Но последовательные порты в системе есть:"
        echo "$real_ports" | sed 's/^/       /'
        echo "     Значит переходник виден, а udev-правило до него не дотянулось."
        echo "     Посмотрите его признаки и сверьте с deploy/99-so101.rules:"
        echo "       udevadm info --query=property --name=$(echo "$real_ports" | head -1) | egrep 'ID_VENDOR_ID|ID_MODEL_ID'"
    else
        echo "     Последовательных портов нет вообще — рука не подключена или не запитана."
    fi
fi

if [ -f /etc/udev/rules.d/99-so101.rules ]; then
    if diff -q "$REPO/deploy/99-so101.rules" /etc/udev/rules.d/99-so101.rules >/dev/null 2>&1; then
        ok "udev-правила установлены и совпадают с репозиторием"
    else
        hm "udev-правила установлены, но ОТЛИЧАЮТСЯ от deploy/99-so101.rules"
    fi
else
    no "udev-правила не установлены: sudo cp deploy/99-so101.rules /etc/udev/rules.d/ && sudo udevadm control --reload-rules && sudo udevadm trigger"
fi

# -------------------------------------------------------------------- права
head_ "2. Права"
if id -nG | tr ' ' '\n' | grep -qx dialout; then
    ok "пользователь в группе dialout (в текущем сеансе)"
else
    if id -nG "$USER" >/dev/null 2>&1 && getent group dialout | grep -qw "$USER"; then
        hm "в dialout добавлен, но текущий сеанс о ней не знает — нужен перезаход"
    else
        no "пользователь НЕ в dialout: sudo usermod -aG dialout,video \$USER, затем перезайти"
    fi
fi

# --------------------------------------------------------------------- порт
head_ "3. Порт открывается"
PY=""
for cand in "$REPO/deploy/.venv/bin/python" "$REPO/tools/.venv-lerobot/bin/python"; do
    [ -x "$cand" ] && PY="$cand" && break
done
if [ -z "$PY" ]; then
    no "нет ни deploy/.venv, ни tools/.venv-lerobot — см. docs/setup-linux.md"
elif [ ! -e "$PORT" ]; then
    echo "     пропущено: устройства нет"
else
    if "$PY" - "$PORT" <<'PYEOF' 2>/dev/null
import sys, serial
serial.Serial(sys.argv[1], baudrate=1_000_000, timeout=0.05).close()
PYEOF
    then
        ok "порт открывается на запись/чтение"
    else
        no "порт есть, но не открывается — почти всегда права (dialout) или порт занят стендом"
        echo "     кто держит: sudo fuser -v $PORT ; стенд гасится docker compose ... stop ros"
    fi
fi

# --------------------------------------------------------------------- шина
head_ "4. Сервоприводы на шине"
if [ ! -e "$PORT" ] || [ -z "$PY" ]; then
    echo "     пропущено: нет устройства или python-окружения"
elif [ -n "$(port_holders)" ]; then
    hm "порт уже занят (pid: $(port_holders | tr '\n' ' ')) — сканировать нельзя"
    echo "     обычно это стенд: docker compose -f deploy/compose.prod.yml stop ros"
else
    "$PY" "$REPO/deploy/servo_scan.py" --port "$PORT"
    case $? in
        0) ok "все шесть суставов отвечают" ;;
        1) no "шина отвечает, но состав неполный (см. вывод выше)" ;;
        *) no "шина не отвечает (см. вывод выше)" ;;
    esac
fi

# --------------------------------------------------------------- калибровка
head_ "5. Калибровка"
CAL_DIR="${HOME}/.cache/huggingface/lerobot/calibration"
if compgen -G "${CAL_DIR}/robots/*/*.json" > /dev/null 2>&1; then
    for f in "${CAL_DIR}"/robots/*/*.json; do ok "калибровка LeRobot: $f"; done
else
    no "калибровки LeRobot нет (искал в ${CAL_DIR}/robots/)"
    echo "     Прошивка ID и калибровка — обязательны, см. docs/hardware-bringup.md шаги 3–4."
    echo "     Значения из upstream (follower_joints.yaml) — от ЧУЖОЙ руки, подставлять нельзя."
fi

if [ -x "$REPO/tools/.venv-lerobot/bin/lerobot-calibrate" ]; then
    ok "инструменты LeRobot установлены (tools/.venv-lerobot)"
else
    no "нет tools/.venv-lerobot — им прошиваются ID и делается калибровка"
fi

# ------------------------------------------------------------------- софт
head_ "6. Софт стенда"
if sg docker -c "docker image inspect so101-ros:dev" >/dev/null 2>&1; then
    if sg docker -c "docker run --rm so101-ros:dev bash -lc 'ls /ros2_ws/install'" 2>/dev/null | grep -qx feetech_ros2_driver; then
        ok "образ so101-ros:dev собран, драйвер feetech_ros2_driver внутри"
    else
        no "образ есть, но БЕЗ feetech_ros2_driver — hardware_type:=real не поднимется"
        echo "     docker compose -f deploy/compose.dev.yml build ros"
    fi
else
    no "образ so101-ros:dev не собран"
fi

if [ -f "$REPO/frontend/dist/index.html" ]; then
    ok "frontend/dist собран"
else
    no "frontend/dist пуст — nginx будет отдавать 404: cd frontend && npm run build"
fi

if [ -n "${ADMIN_TOKEN:-}" ]; then
    ok "ADMIN_TOKEN задан в окружении"
else
    hm "ADMIN_TOKEN не задан — боевой стек без него не поднимется"
    echo "     export ADMIN_TOKEN=\$(openssl rand -hex 24)"
fi

# ------------------------------------------------------------------- итог
head_ "Итог"
echo "  пройдено: $pass   предупреждений: $warn   провалено: $fail"
echo
if [ "$fail" -eq 0 ]; then
    echo "  ✅ Можно запускать боевой стенд:"
    echo "       docker compose -f deploy/compose.prod.yml up -d"
else
    echo "  ❌ Ещё рано. Разберитесь с провалёнными пунктами — по порядку сверху вниз."
    echo "     Подробности: docs/hardware-bringup.md"
fi
exit $(( fail > 0 ))
