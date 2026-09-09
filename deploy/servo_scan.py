#!/usr/bin/env python3
"""Сканер шины сервоприводов Feetech — первое, что запускают с новым железом.

Отвечает на вопрос «шина вообще жива и кто на ней есть», НЕ поднимая ROS.
Ничего не пишет в сервоприводы: только PING и чтение — запускать безопасно
в любой момент, в том числе при поднятом стенде (хотя лучше не надо: на шине
окажется два хозяина, см. ниже).

    deploy/.venv/bin/python deploy/servo_scan.py
    deploy/.venv/bin/python deploy/servo_scan.py --port /dev/ttyACM0
    deploy/.venv/bin/python deploy/servo_scan.py --all      # весь диапазон 1..253

Зачем отдельный инструмент, если есть `lerobot-find-port` и ros2_control:

  * ros2_control на недоступной шине не деградирует, а роняет весь
    `ros2_control_node` (LibSerial::NotOpen). Отлаживать проводку по
    crash-loop-у контейнера — плохая идея;
  * сюда же попадает случай «ID не прошиты»: с завода все сервоприводы имеют
    ID 1, и на шине они конфликтуют. Сканер это прямо покажет — вместо
    шести строк будет одна и куча таймаутов.

ВАЖНО: пока шину слушает ros2_control, отвечать сканеру будет некому (двух
хозяев на полудуплексной шине быть не должно). Сначала гасите стенд:

    docker compose -f deploy/compose.prod.yml stop ros

Протокол — SCS/STS (тот же, что у Dynamixel 1.0). Адреса регистров и таблица
скоростей сверены с LeRobot (`lerobot/motors/feetech/tables.py`), порядок
байт у STS — младший вперёд.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

try:
    import serial
except ImportError:  # pragma: no cover - подсказка вместо трассировки
    sys.exit(
        "Нет pyserial. Поставьте:\n"
        "    deploy/.venv/bin/pip install -r deploy/requirements-qr.txt\n"
        "или используйте окружение LeRobot: tools/.venv-lerobot/bin/python"
    )

# --- протокол --------------------------------------------------------------

HEADER = b"\xff\xff"
INST_PING = 0x01
INST_READ = 0x02

# Регистры STS/SMS (адрес, длина). Сверено с lerobot/motors/feetech/tables.py.
REG_MODEL_NUMBER = (3, 2)
REG_ID = (5, 1)
REG_BAUD_RATE = (6, 1)
REG_PRESENT_POSITION = (56, 2)
REG_PRESENT_VOLTAGE = (62, 1)
REG_PRESENT_TEMPERATURE = (63, 1)

#: Значение регистра 6 → скорость. Из STS_SMS_SERIES_BAUDRATE_TABLE.
BAUD_CODE_TO_RATE = {
    0: 1_000_000, 1: 500_000, 2: 250_000, 3: 128_000,
    4: 115_200, 5: 57_600, 6: 38_400, 7: 19_200,
}
#: Порядок перебора при автоопределении: у SO-101 с завода LeRobot — 1 Мбод.
BAUD_CANDIDATES = [1_000_000, 500_000, 250_000, 128_000, 115_200, 57_600, 38_400, 19_200]

#: Суставы SO-101 в порядке ID (so101_ros2_control.xacro).
SO101_JOINTS = {
    1: "shoulder_pan",
    2: "shoulder_lift",
    3: "elbow_flex",
    4: "wrist_flex",
    5: "wrist_roll",
    6: "gripper",
}
#: Разрешение STS3215 — 4096 тиков на оборот (MODEL_RESOLUTION в LeRobot).
TICKS_PER_TURN = 4096

#: Знаковый бит поля Present_Position (STS_SMS_SERIES_ENCODINGS_TABLE).
SIGN_BIT_POSITION = 15


def decode_sign_magnitude(raw: int, sign_bit: int) -> int:
    """Знак отдельным битом, модуль — в остальных. Не дополнительный код."""
    magnitude = raw & ((1 << sign_bit) - 1)
    return -magnitude if (raw >> sign_bit) & 1 else magnitude


def checksum(payload: Iterable[int]) -> int:
    """~(сумма байт после заголовка) & 0xFF — как в SCS/STS и Dynamixel 1.0."""
    return (~sum(payload)) & 0xFF


def build_packet(servo_id: int, instruction: int, params: bytes = b"") -> bytes:
    """FF FF ID LEN INST [PARAMS] CHK, где LEN = len(params) + 2."""
    body = bytes([servo_id, len(params) + 2, instruction]) + params
    return HEADER + body + bytes([checksum(body)])


@dataclass
class Servo:
    servo_id: int
    model: Optional[int] = None
    position: Optional[int] = None
    voltage: Optional[float] = None
    temperature: Optional[int] = None
    baud_code: Optional[int] = None


class Bus:
    """Полудуплексная шина: пишем пакет, читаем ответ, разбираем."""

    def __init__(self, port: str, baudrate: int, timeout: float) -> None:
        self.port = serial.Serial(port, baudrate=baudrate, timeout=timeout)

    def close(self) -> None:
        self.port.close()

    def _transact(self, packet: bytes, expected_params: int) -> Optional[bytes]:
        """Отправить пакет и вернуть тело ответа, либо None при таймауте/сбое."""
        self.port.reset_input_buffer()
        self.port.write(packet)
        self.port.flush()

        # Ответ: FF FF ID LEN ERR [DATA...] CHK
        head = self.port.read(4)
        if len(head) < 4 or head[:2] != HEADER:
            return None
        resp_id, length = head[2], head[3]
        rest = self.port.read(length)          # ERR + DATA + CHK — ровно length байт
        if len(rest) < length:
            return None
        err, data, chk = rest[0], rest[1:-1], rest[-1]
        if checksum(bytes([resp_id, length]) + bytes([err]) + data) != chk:
            return None
        if len(data) != expected_params:
            return None
        return data

    def ping(self, servo_id: int) -> bool:
        return self._transact(build_packet(servo_id, INST_PING), 0) is not None

    def read(self, servo_id: int, reg: Tuple[int, int]) -> Optional[int]:
        addr, size = reg
        data = self._transact(
            build_packet(servo_id, INST_READ, bytes([addr, size])), size
        )
        if data is None:
            return None
        raw = int.from_bytes(data, "little")    # у STS младший байт вперёд
        if reg == REG_PRESENT_POSITION:
            # Позиция знаковая, причём знак — ОТДЕЛЬНЫМ битом (15), а не
            # дополнительным кодом. До калибровки значения 0…4095 и знак не
            # встречается, но после неё середина хода становится 2047, и
            # позиция уходит в минус — без разбора знака это читалось бы как
            # 32769 вместо -1.
            return decode_sign_magnitude(raw, SIGN_BIT_POSITION)
        return raw


def scan(bus: Bus, ids: List[int]) -> Dict[int, Servo]:
    found: Dict[int, Servo] = {}
    for servo_id in ids:
        if not bus.ping(servo_id):
            continue
        s = Servo(servo_id)
        s.model = bus.read(servo_id, REG_MODEL_NUMBER)
        s.position = bus.read(servo_id, REG_PRESENT_POSITION)
        raw_v = bus.read(servo_id, REG_PRESENT_VOLTAGE)
        s.voltage = None if raw_v is None else raw_v / 10.0
        s.temperature = bus.read(servo_id, REG_PRESENT_TEMPERATURE)
        s.baud_code = bus.read(servo_id, REG_BAUD_RATE)
        found[servo_id] = s
    return found


def autodetect(port: str, ids: List[int], timeout: float) -> Tuple[Optional[int], Dict[int, Servo]]:
    """Перебрать скорости, вернуть первую, на которой кто-то ответил."""
    for baud in BAUD_CANDIDATES:
        bus = Bus(port, baud, timeout)
        try:
            found = scan(bus, ids)
        finally:
            bus.close()
        if found:
            return baud, found
    return None, {}


def ticks_to_deg(ticks: int) -> float:
    return ticks * 360.0 / TICKS_PER_TURN


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Сканер шины сервоприводов Feetech (только чтение)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--port", default="/dev/so101_follower",
                    help="последовательный порт (по умолчанию %(default)s)")
    ap.add_argument("--baud", type=int, default=None,
                    help="скорость; без неё перебираются все известные")
    ap.add_argument("--all", action="store_true",
                    help="сканировать 1..253, а не только шесть суставов SO-101")
    ap.add_argument("--timeout", type=float, default=0.05,
                    help="таймаут ответа, с (по умолчанию %(default)s)")
    args = ap.parse_args()

    ids = list(range(1, 254)) if args.all else sorted(SO101_JOINTS)

    try:
        if args.baud is None:
            print(f"Порт {args.port}: перебираю скорости {BAUD_CANDIDATES[0]}…{BAUD_CANDIDATES[-1]}")
            baud, found = autodetect(args.port, ids, args.timeout)
            if baud is None:
                print("\n❌ Ни на одной скорости никто не ответил.")
                print("   Проверьте: питание сервоприводов включено; кабель шины воткнут;")
                print("   порт не занят стендом (`docker compose ... stop ros`).")
                return 2
        else:
            baud = args.baud
            bus = Bus(args.port, baud, args.timeout)
            try:
                found = scan(bus, ids)
            finally:
                bus.close()
    except serial.SerialException as exc:
        print(f"❌ Порт не открывается: {exc}")
        print("   `ls -l /dev/so101_follower`; состоите ли в группе dialout (`id -nG`);")
        print("   после `usermod -aG dialout` нужен новый сеанс входа.")
        return 2

    print(f"\nСкорость шины: {baud} бод")
    print(f"Найдено сервоприводов: {len(found)}\n")

    if found:
        print(f"{'ID':>3}  {'сустав':<14} {'модель':>7} {'позиция':>16} {'питание':>9} {'t°':>5}  скорость в EEPROM")
        print("─" * 88)
        for sid in sorted(found):
            s = found[sid]
            joint = SO101_JOINTS.get(sid, "—")
            pos = "—" if s.position is None else f"{s.position:>5} ({ticks_to_deg(s.position):6.1f}°)"
            volt = "—" if s.voltage is None else f"{s.voltage:.1f} В"
            temp = "—" if s.temperature is None else f"{s.temperature}°C"
            eeprom = BAUD_CODE_TO_RATE.get(s.baud_code, "?") if s.baud_code is not None else "—"
            print(f"{sid:>3}  {joint:<14} {str(s.model):>7} {pos:>16} {volt:>9} {temp:>5}  {eeprom}")

    # --- выводы, а не только таблица ---------------------------------------
    print()
    expected = set(SO101_JOINTS)
    missing = sorted(expected - set(found))

    if not args.all and missing:
        print(f"❌ Не отвечают ID: {missing}")
        print("   Ожидаются 1…6 — по одному на сустав.")
        if found == {1: found.get(1)} or set(found) == {1}:
            print("   Ответил только ID 1: похоже, сервоприводы НЕ прошиты — с завода у всех ID 1,")
            print("   и на общей шине они конфликтуют. Прошейте по одному:")
            print("     tools/.venv-lerobot/bin/lerobot-setup-motors \\")
            print("       --robot.type=so101_follower --robot.port=" + args.port)
        return 1

    hot = [s for s in found.values() if s.temperature is not None and s.temperature >= 55]
    if hot:
        print(f"⚠️  Горячие сервоприводы (≥55 °C): {[s.servo_id for s in hot]} — дайте остыть.")

    low = [s for s in found.values() if s.voltage is not None and s.voltage < 6.0]
    if low:
        print(f"⚠️  Низкое питание (<6 В): {[(s.servo_id, s.voltage) for s in low]} — проверьте БП.")

    if not args.all:
        print("✅ Все шесть суставов на шине и отвечают.")
        print("   Дальше — калибровка: см. docs/hardware-bringup.md, шаг 4.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
