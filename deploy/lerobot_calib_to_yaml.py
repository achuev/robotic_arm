#!/usr/bin/env python3
"""Калибровка LeRobot → `joint_config_file` драйвера Feetech.

    deploy/.venv/bin/python deploy/lerobot_calib_to_yaml.py \
        ~/.cache/huggingface/lerobot/calibration/robots/so101_follower/stand.json \
        -o deploy/so101_follower_joints.yaml

ЗАЧЕМ ЭТО НУЖНО, ЕСЛИ КАЛИБРОВКА И ТАК В EEPROM. `lerobot-calibrate` пишет
`homing_offset` и пределы в EEPROM сервоприводов, и драйвер берёт их оттуда —
на этом стенд уже поедет. Но EEPROM живёт в конкретной железке: сгорел
сервопривод, приехал новый, кто-то запустил чужой инструмент — и калибровки
больше нет, а восстановить её неоткуда. Файл кладётся в репозиторий и
возвращает известно-хорошие значения одной командой.

Драйвер применяет YAML поверх URDF и ЗАПИСЫВАЕТ значения в EEPROM при старте
(см. upstream/feetech_ros2_driver/doc/user.md). То есть этот файл — не только
резервная копия, но и способ навязать значения железу.

ВНИМАНИЕ. `so101_bringup/config/hardware/follower_joints.yaml` из апстрима —
калибровка ЧУЖОЙ руки. Её нельзя подставлять своей: нули суставов окажутся не
там, и рука пойдёт в механические упоры. Этот скрипт существует ровно для
того, чтобы такой файл был СВОЙ.

Проверка корректности: скрипт воспроизводит `follower_joints.yaml` апстрима
из его же `lerobot_follower_arm.json` байт в байт — см. docs/hardware-bringup.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

#: Суставы в порядке ID — как в so101_ros2_control.xacro.
JOINT_ORDER = [
    "shoulder_pan", "shoulder_lift", "elbow_flex",
    "wrist_flex", "wrist_roll", "gripper",
]

#: Настройки регулятора, которых нет в выводе LeRobot. Значения — из
#: so101_ros2_control.xacro, то есть те же, что применились бы без YAML.
TUNING = {
    "p_coefficient": 16,
    "i_coefficient": 0,
    "d_coefficient": 32,
    "return_delay_time": 0,
    "acceleration": 254,
}

#: Защита схвата. LeRobot таких значений не производит, а без них сервопривод
#: схвата легко перегрузить: он единственный постоянно упирается в предмет.
GRIPPER_PROTECTION = {
    "max_torque_limit": 500,
    "protection_current": 250,
    "overload_torque": 25,
}

#: Поля, которые берём из калибровки, в порядке вывода.
CALIB_FIELDS = ["id", "homing_offset", "range_min", "range_max"]


def load_calibration(path: Path) -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path}: не разбирается как JSON — {exc}")
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: ожидался объект вида {{сустав: {{...}}}}")
    return data


def validate(calib: Dict[str, Dict[str, Any]], path: Path) -> None:
    missing = [j for j in JOINT_ORDER if j not in calib]
    if missing:
        raise SystemExit(
            f"{path}: в калибровке нет суставов {missing}.\n"
            f"Найдено: {sorted(calib)}\n"
            "Похоже, это калибровка не follower-руки SO-101."
        )
    ids = {}
    for joint in JOINT_ORDER:
        entry = calib[joint]
        for field in CALIB_FIELDS:
            if field not in entry:
                raise SystemExit(f"{path}: у сустава {joint} нет поля {field!r}")
        sid = entry["id"]
        if sid in ids:
            raise SystemExit(
                f"{path}: ID {sid} у двух суставов ({ids[sid]} и {joint}). "
                "Сервоприводы не прошиты — на шине конфликт."
            )
        ids[sid] = joint
        if entry["range_min"] >= entry["range_max"]:
            raise SystemExit(
                f"{path}: у {joint} range_min ({entry['range_min']}) "
                f"не меньше range_max ({entry['range_max']})"
            )


def render(calib: Dict[str, Dict[str, Any]], source: Path) -> str:
    lines = [
        "# joint_config_file для feetech_ros2_driver — калибровка ЭТОЙ руки.",
        "#",
        f"# Сгенерировано deploy/lerobot_calib_to_yaml.py из {source.name}",
        "# Руками не править: перекалибровали — перегенерируйте.",
        "#",
        "# Драйвер ЗАПИСЫВАЕТ эти значения в EEPROM сервоприводов при старте.",
        "joints:",
    ]
    for i, joint in enumerate(JOINT_ORDER):
        entry = calib[joint]
        if i:
            lines.append("")
        lines.append(f"  {joint}:")
        for field in CALIB_FIELDS:
            lines.append(f"    {field}: {entry[field]}")
        for key, value in TUNING.items():
            lines.append(f"    {key}: {value}")
        if joint == "gripper":
            for key, value in GRIPPER_PROTECTION.items():
                lines.append(f"    {key}: {value}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Калибровка LeRobot → joint_config_file драйвера Feetech",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("calibration", type=Path,
                    help="JSON из ~/.cache/huggingface/lerobot/calibration/robots/so101_follower/")
    ap.add_argument("-o", "--out", type=Path,
                    default=Path("deploy/so101_follower_joints.yaml"),
                    help="куда писать (по умолчанию %(default)s)")
    args = ap.parse_args()

    if not args.calibration.is_file():
        raise SystemExit(f"Нет файла {args.calibration}.\n"
                         "Сначала откалибруйте руку: см. docs/hardware-bringup.md, шаг 4.")

    calib = load_calibration(args.calibration)
    validate(calib, args.calibration)
    text = render(calib, args.calibration)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")

    print(f"{args.out}  ← {args.calibration}")
    print()
    print(f"{'сустав':<15} {'ID':>3} {'homing':>8} {'range_min':>10} {'range_max':>10}")
    print("─" * 50)
    for joint in JOINT_ORDER:
        e = calib[joint]
        print(f"{joint:<15} {e['id']:>3} {e['homing_offset']:>8} "
              f"{e['range_min']:>10} {e['range_max']:>10}")
    print()
    print("Подключить к стенду — раскомментировать в deploy/compose.prod.yml")
    print("блок joint_config_file (см. комментарий там же).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
