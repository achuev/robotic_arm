#!/usr/bin/env python3
"""Калибровка руки SO-101 — неинтерактивно, по шагам.

    deploy/.venv/bin/python deploy/servo_calibrate.py reset
    # поставить руку в среднее положение
    deploy/.venv/bin/python deploy/servo_calibrate.py center
    # крутить суставы от упора до упора, пока идёт запись
    deploy/.venv/bin/python deploy/servo_calibrate.py record --seconds 60
    deploy/.venv/bin/python deploy/servo_calibrate.py save --id stand

ЗАЧЕМ, ЕСЛИ ЕСТЬ `lerobot-calibrate`. Штатная команда интерактивная: ждёт
Enter между фазами. Там, где стандартный ввод не подключён к терминалу,
она падает с EOF. Здесь те же фазы разнесены по отдельным вызовам, а
промежуточное состояние лежит в `deploy/.calibration-state.json`.

ЧТО СЧИТАЕТСЯ. Ровно то же, что у LeRobot (сверено с `so_follower.calibrate`,
`MotorsBus.set_half_turn_homings` и `FeetechMotorsBus._get_half_turn_homings`):

    homing_offset = позиция_в_середине - 2047
    range_min / range_max = минимум и максимум за время прокрутки

Прошивка `Present_Position = Actual_Position - Homing_Offset` выполняется
самим сервоприводом, поэтому после `center` середина хода читается как 2047.

ДВА ЗНАКОВЫХ ПОЛЯ, И БИТЫ У НИХ РАЗНЫЕ. Feetech хранит знак отдельным битом,
а не дополнительным кодом: у `Homing_Offset` это бит 11, у `Present_Position`
— бит 15. Перепутать легко, а последствие тихое: рука уезжает в другую
сторону или считает, что стоит за пределами хода.

`wrist_roll` полнооборотный: у него диапазон всегда 0…4095, упоров нет, и
прокручивать его не нужно — LeRobot поступает так же.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Dict

_spec = importlib.util.spec_from_file_location(
    "servo_scan", Path(__file__).with_name("servo_scan.py")
)
scan = importlib.util.module_from_spec(_spec)
sys.modules["servo_scan"] = scan
_spec.loader.exec_module(scan)

_spec2 = importlib.util.spec_from_file_location(
    "servo_set_id", Path(__file__).with_name("servo_set_id.py")
)
setid = importlib.util.module_from_spec(_spec2)
sys.modules["servo_set_id"] = setid
_spec2.loader.exec_module(setid)

STATE = Path(__file__).with_name(".calibration-state.json")

# Регистры STS/SMS. Сверено с lerobot/motors/feetech/tables.py.
REG_MIN_LIMIT = (9, 2)
REG_MAX_LIMIT = (11, 2)
REG_HOMING_OFFSET = (31, 2)
REG_OPERATING_MODE = (33, 1)
REG_PRESENT_POSITION = (56, 2)

#: Разрешение энкодера. 4096 отсчётов, максимум 4095, середина 2047.
MAX_RES = 4095
HALF_TURN = MAX_RES // 2          # 2047 — так же считает LeRobot

#: Знаковые биты. Разные у разных полей, см. шапку.
SIGN_BIT_HOMING = 11
SIGN_BIT_POSITION = 15

#: Полнооборотный сустав: упоров нет, диапазон всегда полный.
FULL_TURN_JOINT = "wrist_roll"

JOINT_IDS = {name: sid for sid, name in scan.SO101_JOINTS.items()}


def encode_sign_magnitude(value: int, sign_bit: int) -> int:
    """Знак отдельным битом, модуль — в остальных. Не дополнительный код."""
    max_magnitude = (1 << sign_bit) - 1
    magnitude = abs(value)
    if magnitude > max_magnitude:
        raise ValueError(f"|{value}| не влезает в {sign_bit} бит")
    return ((1 if value < 0 else 0) << sign_bit) | magnitude


def decode_sign_magnitude(raw: int, sign_bit: int) -> int:
    magnitude = raw & ((1 << sign_bit) - 1)
    return -magnitude if (raw >> sign_bit) & 1 else magnitude


class Arm(setid.Writer):
    """Шина со знанием про знаковые поля и про то, что EEPROM надо разблокировать."""

    def write_retry(self, servo_id: int, reg, value: int, tries: int = 4) -> bool:
        """Запись с повтором.

        Запись в EEPROM у Feetech заметно медленнее чтения, и одиночный
        потерянный ответ — обычное дело: сервопривод значение принял, а
        подтверждение не успело. Без повтора это выглядит как «сервопривод
        не подтвердил запись» на середине калибровки, хотя всё в порядке.
        """
        for attempt in range(tries):
            if self.write(servo_id, reg, value):
                return True
            time.sleep(0.03 * (attempt + 1))
        return False

    def read_position(self, servo_id: int):
        raw = self.read(servo_id, REG_PRESENT_POSITION)
        return None if raw is None else decode_sign_magnitude(raw, SIGN_BIT_POSITION)

    def unlock(self, servo_id: int) -> bool:
        """Момент выключить, EEPROM разблокировать — иначе запись не пройдёт."""
        ok = self.write_retry(servo_id, setid.REG_TORQUE_ENABLE, 0)
        return self.write_retry(servo_id, setid.REG_LOCK, 0) and ok

    def lock(self, servo_id: int) -> bool:
        return self.write_retry(servo_id, setid.REG_LOCK, 1)


def open_bus(args) -> Arm:
    try:
        return Arm(args.port, args.baud, args.timeout)
    except Exception as exc:
        raise SystemExit(f"❌ Порт не открывается: {exc}")


def require_all(bus: Arm) -> Dict[int, object]:
    """Все шесть обязаны быть на шине: калибровать половину руки бессмысленно."""
    found = scan.scan(bus, sorted(scan.SO101_JOINTS))
    missing = sorted(set(scan.SO101_JOINTS) - set(found))
    if missing:
        names = ", ".join(f"{scan.SO101_JOINTS[i]} (ID {i})" for i in missing)
        raise SystemExit(
            f"❌ На шине нет: {names}\n"
            "   Проверьте цепочку шлейфов и питание: deploy/servo_scan.py"
        )
    return found


def load_state() -> dict:
    if STATE.is_file():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# --------------------------------------------------------------------- reset
def cmd_reset(bus: Arm, args) -> int:
    require_all(bus)
    print("Сбрасываю калибровку: homing_offset = 0, пределы 0…4095\n")
    for name, sid in sorted(JOINT_IDS.items(), key=lambda kv: kv[1]):
        if not bus.unlock(sid):
            print(f"  ❌ {name}: не разблокировался EEPROM")
            return 1
        okh = bus.write_retry(sid, REG_HOMING_OFFSET, 0)
        okmin = bus.write_retry(sid, REG_MIN_LIMIT, 0)
        okmax = bus.write_retry(sid, REG_MAX_LIMIT, MAX_RES)
        # Режим позиционирования: рука должна ходить по углу, а не крутиться.
        okmode = bus.write_retry(sid, REG_OPERATING_MODE, 0)
        if not (okh and okmin and okmax and okmode):
            print(f"  ❌ {name}: сервопривод не подтвердил запись")
            return 1
        print(f"  ✅ {name:<14} (ID {sid})")

    STATE.unlink(missing_ok=True)
    print("\nМомент выключен — рука сейчас проворачивается руками.")
    print("Поставьте её в СРЕДНЕЕ положение каждого сустава и запустите:")
    print("    deploy/.venv/bin/python deploy/servo_calibrate.py center")
    return 0


# -------------------------------------------------------------------- center
def cmd_center(bus: Arm, args) -> int:
    require_all(bus)
    selected = getattr(args, "joints", None) or sorted(JOINT_IDS, key=lambda n: JOINT_IDS[n])
    targets = [(n, JOINT_IDS[n]) for n in sorted(selected, key=lambda n: JOINT_IDS[n])]
    print(f"Фиксирую середину хода: {', '.join(n for n, _ in targets)}\n")
    print(f"{'сустав':<14} {'позиция':>8} {'homing_offset':>14}")
    print("─" * 40)

    # СНАЧАЛА обнулить старое смещение, и только потом читать позицию.
    # Сервопривод отдаёт Present_Position = Actual - Homing_Offset, то есть
    # уже смещённую. Посчитать по ней новое смещение — значит вычесть его
    # дважды: при повторном запуске `center` рука уезжает, а прошлая
    # калибровка затирается. LeRobot по той же причине начинает
    # set_half_turn_homings() с reset_calibration().
    for name, sid in targets:
        if not bus.unlock(sid) or not bus.write_retry(sid, REG_HOMING_OFFSET, 0):
            print(f"  ❌ {name}: не удалось обнулить старое смещение")
            return 1
    time.sleep(0.1)

    offsets = {}
    for name, sid in targets:
        pos = bus.read_position(sid)
        if pos is None:
            print(f"  ❌ {name}: позиция не читается")
            return 1
        offset = pos - HALF_TURN
        if not bus.unlock(sid):
            print(f"  ❌ {name}: не разблокировался EEPROM")
            return 1
        if not bus.write_retry(sid, REG_HOMING_OFFSET,
                               encode_sign_magnitude(offset, SIGN_BIT_HOMING)):
            print(f"  ❌ {name}: homing_offset не записался")
            return 1
        offsets[name] = offset
        # Сырая позиция у края шкалы (близко к 0 или 4095) НЕ означает, что
        # сустав уперся: это может быть точка перехода энкодера через ноль,
        # попавшая внутрь хода. Схват SO-101 — типичный случай: с одной
        # стороны читается ~4070, с другой ~60, а физически это соседние
        # положения. Смещение как раз и уводит разрыв из рабочей зоны.
        # Поэтому здесь ничего не проверяем: настоящие проверки —
        # величина хода в `record` и границы 0…4095 в `save`.
        wrap = "  (разрыв энкодера внутри хода — смещение его уберёт)" \
            if pos < 0.15 * MAX_RES or pos > 0.85 * MAX_RES else ""
        print(f"{name:<14} {pos:>8} {offset:>14}{wrap}")

    # Проверяем на железе: после записи середина обязана читаться как 2047.
    time.sleep(0.1)
    print("\nПроверка — теперь должны показывать примерно 2047:")
    bad = []
    for name, sid in targets:
        pos = bus.read_position(sid)
        mark = "✅" if pos is not None and abs(pos - HALF_TURN) <= 20 else "❌"
        if mark == "❌":
            bad.append(name)
        print(f"  {mark} {name:<14} {pos}")
    if bad:
        print(f"\n❌ Не приняли смещение: {', '.join(bad)}")
        print("   Обычно это заблокированный EEPROM или включённый момент.")
        return 1

    state = load_state()
    # Смещение сдвигает Present_Position, поэтому ранее снятый диапазон этого
    # сустава становится недействительным — выбрасываем, чтобы `save` не
    # записал в EEPROM пределы, посчитанные от старого нуля.
    state.setdefault("homing_offsets", {}).update(offsets)
    for name in offsets:
        state.get("ranges", {}).pop(name, None)
    save_state(state)

    print("\nТеперь прогоните каждый сустав от упора до упора"
          f" (кроме {FULL_TURN_JOINT} — он полнооборотный):")
    print("    deploy/.venv/bin/python deploy/servo_calibrate.py record --seconds 60")
    return 0


# -------------------------------------------------------------------- record
def cmd_record(bus: Arm, args) -> int:
    state = load_state()
    if "homing_offsets" not in state:
        raise SystemExit("❌ Сначала `center` — без середины диапазоны не имеют смысла.")
    require_all(bus)

    # Накапливаем поверх прошлого запуска: если не успели прокрутить всё за
    # один заход, команду можно повторить, а не начинать заново.
    ranges = {k: dict(v) for k, v in state.get("ranges", {}).items()}

    selected = getattr(args, "joints", None)
    def wanted(name: str) -> bool:
        return name in selected if selected else name != FULL_TURN_JOINT

    targets = [(n, i) for n, i in sorted(JOINT_IDS.items(), key=lambda kv: kv[1])
               if wanted(n)]

    print(f"Пишу диапазоны {args.seconds} с. Крутите суставы от упора до упора.")
    print(f"({FULL_TURN_JOINT} не нужен — у него полный оборот)\n")

    deadline = time.time() + args.seconds
    samples = 0
    last_print = 0.0
    while time.time() < deadline:
        for name, sid in targets:
            pos = bus.read_position(sid)
            if pos is None:
                continue
            r = ranges.setdefault(name, {"min": pos, "max": pos})
            r["min"] = min(r["min"], pos)
            r["max"] = max(r["max"], pos)
        samples += 1

        now = time.time()
        if now - last_print >= 5.0:
            left = int(deadline - now)
            spans = " ".join(
                f"{n[:5]}:{ranges[n]['max'] - ranges[n]['min']:>4}"
                for n, _ in targets if n in ranges
            )
            print(f"  осталось {left:>3} с | ход: {spans}")
            last_print = now

    ranges[FULL_TURN_JOINT] = {"min": 0, "max": MAX_RES}
    state["ranges"] = ranges
    save_state(state)

    print(f"\nСнято {samples} проходов по шине\n")
    print(f"{'сустав':<14} {'min':>6} {'max':>6} {'ход':>6}  {'':>4}")
    print("─" * 44)
    narrow = []
    for name, _ in sorted(JOINT_IDS.items(), key=lambda kv: kv[1]):
        r = ranges.get(name)
        if not r:
            print(f"{name:<14} {'—':>6} {'—':>6} {'—':>6}  ❌ не двигали")
            narrow.append(name)
            continue
        span = r["max"] - r["min"]
        # Меньше 500 отсчётов — это примерно 44°, для суставов SO-101 мало.
        # Почти всегда означает «этот сустав забыли прокрутить».
        mark = "✅" if span >= 500 or name == FULL_TURN_JOINT else "⚠️ мало"
        if mark != "✅":
            narrow.append(name)
        print(f"{name:<14} {r['min']:>6} {r['max']:>6} {span:>6}  {mark}")

    if narrow:
        print(f"\n⚠️  Похоже, не прокручены: {', '.join(narrow)}")
        print("   Повторите `record` — результаты накапливаются, заново не начнёте.")
        return 1

    print("\nВсё снято. Сохранить:")
    print("    deploy/.venv/bin/python deploy/servo_calibrate.py save --id stand")
    return 0


# ---------------------------------------------------------------------- save
def cmd_save(bus: Arm, args) -> int:
    state = load_state()
    if "homing_offsets" not in state or "ranges" not in state:
        raise SystemExit("❌ Нет данных. Пройдите reset → center → record.")
    require_all(bus)

    offsets = state["homing_offsets"]
    ranges = state["ranges"]

    # Регистры пределов БЕЗЗНАКОВЫЕ. Отрицательный range_min означает, что
    # середина была поставлена у упора: записанное значение свернулось бы в
    # большое положительное, и сустав считал бы, что стоит вне своего хода.
    bad = {n: r for n, r in ranges.items()
           if r["min"] < 0 or r["max"] > MAX_RES}
    if bad:
        print("❌ Диапазон вышел за 0…4095:")
        for n, r in bad.items():
            print(f"   {n:<14} {r['min']} … {r['max']}")
        print("\n   Значит середина для этих суставов была у упора.")
        print("   Поставьте их посередине хода и пройдите заново:")
        print("     servo_calibrate.py reset → center → record")
        return 1

    # Текущее положение обязано лежать ВНУТРИ записанного диапазона. Если нет —
    # сустав не прогнали до упора, и записанный предел уже его отсекает.
    # На живой руке это выглядит так: драйвер задаёт целью текущее положение,
    # сервопривод не принимает цель вне своих пределов и выдавливает сустав к
    # ближайшему допустимому. У схвата это ощущается как «сам сжался и упёрся».
    outside = []
    for name, sid in sorted(JOINT_IDS.items(), key=lambda kv: kv[1]):
        pos = bus.read_position(sid)
        r = ranges[name]
        if pos is not None and not (r["min"] <= pos <= r["max"]):
            outside.append((name, pos, r["min"], r["max"]))
    if outside:
        print("❌ Сустав стоит вне своего записанного диапазона:")
        for name, pos, lo, hi in outside:
            print(f"   {name:<14} сейчас {pos}, а записано {lo}…{hi}")
        print("\n   Значит его не прогнали до упора при записи.")
        print("   Повторите `record` и пройдите этими суставами весь ход,")
        print("   диапазоны накопятся поверх уже снятых.")
        return 1

    print("Записываю пределы в EEPROM\n")
    calibration = {}
    for name, sid in sorted(JOINT_IDS.items(), key=lambda kv: kv[1]):
        r = ranges[name]
        if not bus.unlock(sid):
            print(f"  ❌ {name}: не разблокировался EEPROM")
            return 1
        if not (bus.write_retry(sid, REG_MIN_LIMIT, r["min"])
                and bus.write_retry(sid, REG_MAX_LIMIT, r["max"])):
            print(f"  ❌ {name}: пределы не записались")
            return 1
        bus.lock(sid)
        calibration[name] = {
            "id": sid,
            "drive_mode": 0,
            "homing_offset": offsets[name],
            "range_min": r["min"],
            "range_max": r["max"],
        }
        print(f"  ✅ {name:<14} {r['min']:>5} … {r['max']:>5}")

    # Тот же путь и формат, что пишет lerobot-calibrate: дальше по цепочке
    # (deploy/lerobot_calib_to_yaml.py, deploy/preflight.sh) ничего менять
    # не придётся.
    out = Path.home() / ".cache/huggingface/lerobot/calibration/robots/so101_follower"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{args.id}.json"
    path.write_text(json.dumps(calibration, indent=4) + "\n", encoding="utf-8")

    print(f"\n✅ Калибровка сохранена: {path}")
    print("\nДальше — положить её в репозиторий:")
    print(f"    deploy/.venv/bin/python deploy/lerobot_calib_to_yaml.py {path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Пошаговая калибровка SO-101 без интерактивного ввода",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--port", default="/dev/so101_follower")
    ap.add_argument("--baud", type=int, default=1_000_000)
    ap.add_argument("--timeout", type=float, default=0.06)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("reset", help="сбросить калибровку и выключить момент")
    cen = sub.add_parser("center", help="зафиксировать середину хода")
    cen.add_argument("--joints", nargs="+", choices=sorted(JOINT_IDS),
                     help="только эти суставы (по умолчанию все шесть)")
    rec = sub.add_parser("record", help="писать диапазоны, пока крутите суставы")
    rec.add_argument("--seconds", type=float, default=60.0)
    rec.add_argument("--joints", nargs="+", choices=sorted(JOINT_IDS),
                     help="только эти суставы (по умолчанию все, кроме wrist_roll)")
    sv = sub.add_parser("save", help="записать в EEPROM и сохранить файл")
    sv.add_argument("--id", default="stand", help="имя робота в файле калибровки")

    args = ap.parse_args()
    bus = open_bus(args)
    try:
        return {"reset": cmd_reset, "center": cmd_center,
                "record": cmd_record, "save": cmd_save}[args.cmd](bus, args)
    finally:
        bus.close()


if __name__ == "__main__":
    sys.exit(main())
