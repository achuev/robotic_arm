#!/usr/bin/env python3
"""Прошивка ID одного сервопривода Feetech — неинтерактивно, по одному вызову.

    deploy/.venv/bin/python deploy/servo_set_id.py gripper
    deploy/.venv/bin/python deploy/servo_set_id.py shoulder_pan

ЗАЧЕМ, ЕСЛИ ЕСТЬ `lerobot-setup-motors`. Штатный инструмент интерактивный:
он проводит по всем шести сервоприводам и на каждом шаге ждёт Enter. Это
удобно за живым терминалом и не работает там, где стандартный ввод не
подключён к терминалу — а именно так команды и запускаются в нашей рабочей
сессии (`input()` немедленно получает EOF). Здесь тот же результат
достигается шестью отдельными вызовами.

Последовательность записи повторяет `MotorsBus.setup_motor` и
`FeetechMotorsBus._disable_torque` из LeRobot:

    Torque_Enable (40) = 0     момент выключить — иначе EEPROM не пишется
    Lock          (55) = 0     разблокировать EEPROM
    ID            (5)  = цель  (пишется по СТАРОМУ адресу)
    Baud_Rate     (6)  = 0     код 0 = 1 Мбод (пишется уже по НОВОМУ адресу)
    Lock          (55) = 1     заблокировать обратно

Адрес `Lock` у серий разный: 55 у STS/SMS и 48 у SCS. Здесь STS/SMS — тот же
набор, что использует драйвер стенда (модель 777 = sts3215).

ЗАЩИТА ОТ ГЛАВНОЙ ОШИБКИ. Перед записью шина сканируется, и если на ней
отвечает больше одного сервопривода — скрипт отказывается работать. Команда
«стань шестым» ушла бы всем сразу, и они снова стали бы неразличимы. Ровно
поэтому прошивают по одному, физически отсоединив остальные.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from pathlib import Path

# servo_scan лежит рядом; берём из него протокол, чтобы не держать две копии.
_spec = importlib.util.spec_from_file_location(
    "servo_scan", Path(__file__).with_name("servo_scan.py")
)
scan = importlib.util.module_from_spec(_spec)
sys.modules["servo_scan"] = scan
_spec.loader.exec_module(scan)

INST_WRITE = 0x03

# Регистры STS/SMS (адрес, длина). Сверено с
# lerobot/motors/feetech/tables.py → STS_SMS_SERIES_CONTROL_TABLE.
REG_TORQUE_ENABLE = (40, 1)
REG_LOCK = (55, 1)
REG_ID = (5, 1)
REG_BAUD_RATE = (6, 1)

#: Код скорости в EEPROM: 0 = 1 Мбод. Та же таблица, что у сканера.
BAUD_CODE_1M = 0

#: Сустав → ID. Порядок из so101_ros2_control.xacro.
JOINT_IDS = {name: sid for sid, name in scan.SO101_JOINTS.items()}


class Writer(scan.Bus):
    """Шина плюс запись регистров."""

    def write(self, servo_id: int, reg: tuple[int, int], value: int) -> bool:
        addr, size = reg
        payload = bytes([addr]) + int(value).to_bytes(size, "little")
        # Ответ на запись — статус-пакет без параметров.
        return self._transact(scan.build_packet(servo_id, INST_WRITE, payload), 0) is not None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Прошивка ID одного сервопривода SO-101",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("joint", nargs="?", choices=sorted(JOINT_IDS),
                    help="какой сустав подключён к плате сейчас")
    ap.add_argument("--port", default="/dev/so101_follower")
    ap.add_argument("--baud", type=int, default=1_000_000)
    ap.add_argument("--timeout", type=float, default=0.08)
    ap.add_argument("--list", action="store_true", help="показать соответствие суставов и ID")
    ap.add_argument("--force", action="store_true",
                    help="разрешить перепрошивку сервопривода, уже настроенного под другой сустав")
    args = ap.parse_args()

    if args.list or not args.joint:
        print("Сустав → ID (порядок прошивки: от схвата к основанию)\n")
        for sid in sorted(scan.SO101_JOINTS, reverse=True):
            print(f"  {scan.SO101_JOINTS[sid]:<14} → {sid}")
        print("\nПрошивать по одному, остальные сервоприводы отсоединить.")
        return 0

    target = JOINT_IDS[args.joint]

    try:
        bus = Writer(args.port, args.baud, args.timeout)
    except Exception as exc:
        print(f"❌ Порт не открывается: {exc}")
        return 2

    try:
        # --- кто на шине -----------------------------------------------
        found = sorted(scan.scan(bus, list(range(1, 254))))

        if not found:
            print("❌ На шине никто не отвечает.")
            print("   Питание включено? Шлейф воткнут? Джамперы на B?")
            return 2

        if len(found) > 1:
            print(f"❌ На шине несколько сервоприводов: ID {found}")
            print("   Прошивать можно только когда подключён РОВНО ОДИН —")
            print("   иначе новый номер получат все сразу и снова станут одинаковыми.")
            print("   Отсоедините лишние и повторите.")
            return 1

        current = found[0]

        # ЗАЩИТА ОТ ПЕРЕПРОШИВКИ УЖЕ НАСТРОЕННОГО. С завода у всех ID 1,
        # поэтому «на шине не 1 и не то, что просят» означает ровно одно:
        # подключён сервопривод, который уже прошит под ДРУГОЙ сустав.
        # Почти всегда это «забыли переткнуть шлейф». Без этой проверки
        # предыдущий сустав молча теряет свой номер, а пропущенный остаётся
        # с заводским — и на собранной шине оказываются два ID 1 и ни одного
        # нужного. Выясняется это через полчаса, на непонятном отказе.
        if current != 1 and current != target and not args.force:
            other = scan.SO101_JOINTS.get(current, "неизвестный сустав")
            print(f"❌ На шине сервопривод с ID {current} — это уже прошитый «{other}».")
            print(f"   Вы просите сделать его «{args.joint}» (ID {target}).")
            print("   Скорее всего, шлейф не переткнули на следующий сервопривод.")
            print(f"   Если это правда нужно — повторите с --force.")
            return 1

        # Совпадение ID — не повод ничего не делать. shoulder_pan и с завода
        # имеет ID 1, но скорость и блокировку EEPROM ему всё равно надо
        # проставить, чтобы он ничем не отличался от остальных пяти.
        same = current == target
        if same:
            print(f"{args.joint}: ID уже {target}, проставляю скорость и блокировку")
        else:
            print(f"{args.joint}: ID {current} → {target}")

        # --- запись ------------------------------------------------------
        steps = [
            ("выключаю момент",      current, REG_TORQUE_ENABLE, 0),
            ("разблокирую EEPROM",   current, REG_LOCK,          0),
        ]
        if not same:
            # дальше сервопривод отзывается уже на новый адрес
            steps.append(("пишу новый ID", current, REG_ID, target))
        steps += [
            ("ставлю 1 Мбод",        target,  REG_BAUD_RATE,     BAUD_CODE_1M),
            ("блокирую EEPROM",      target,  REG_LOCK,          1),
        ]
        for label, sid, reg, value in steps:
            if not bus.write(sid, reg, value):
                print(f"  ❌ {label}: сервопривод не подтвердил запись")
                print("     Если это первый шаг — проверьте питание и шлейф.")
                print("     Если после смены ID — сервопривод мог остаться на старом номере;")
                print("     просканируйте шину (deploy/servo_scan.py --all) и повторите.")
                return 1
            print(f"  ✅ {label}")
            time.sleep(0.02)   # EEPROM пишется не мгновенно

        # --- проверка ----------------------------------------------------
        time.sleep(0.1)
        check = scan.scan(bus, [target])
        if target not in check:
            print(f"❌ После записи ID {target} не отвечает. Просканируйте шину целиком.")
            return 1

        s = check[target]
        volts = "—" if s.voltage is None else f"{s.voltage:.1f} В"
        print(f"\n✅ {args.joint} = ID {target}   питание {volts}, "
              f"t° {s.temperature}°C, модель {s.model}")

        # Порядок прошивки фиксированный: от схвата (6) к основанию (1).
        # Поэтому «следующий» вычисляется, а не угадывается — список
        # «осталось» скрипт составить не может, он не знает, что уже сделано.
        if target > 1:
            nxt = scan.SO101_JOINTS[target - 1]
            print(f"\nСледующий по порядку: {nxt} → ID {target - 1}")
            print("Отсоедините этот сервопривод, подключите следующий.")
        else:
            print("\nЭто был последний. Теперь соедините сервоприводы шлейфами")
            print("в цепочку и подключите shoulder_pan (ID 1) к плате,")
            print("затем проверьте: deploy/servo_scan.py")
        return 0
    finally:
        bus.close()


if __name__ == "__main__":
    sys.exit(main())
