#!/usr/bin/env python3
"""Сдвинуть ОДИН сустав на малый угол и показать, что получилось.

Запускается ВНУТРИ контейнера с поднятой рукой:

    docker exec so101-hwtest /entrypoint.sh python3 /d/jog_joint.py wrist_roll 0.1

Инструмент для шага «первое движение» (docs/hardware-bringup.md §7): подать
цель одному суставу и глазами сверить, что поехал именно он и в ту сторону.

ЧЕМ ОТЛИЧАЕТСЯ ОТ `ros2 topic pub`. Голая публикация требует вручную собрать
массив из шести чисел в порядке контроллера — а он НЕ алфавитный:

    команда:      shoulder_pan, shoulder_lift, elbow_flex, wrist_flex, wrist_roll, gripper
    joint_states: elbow_flex, gripper, shoulder_lift, shoulder_pan, wrist_flex, wrist_roll

Перепутать порядок — значит послать локтю цель плеча. Здесь текущие
положения читаются ПО ИМЕНИ и раскладываются в порядок контроллера, так что
все суставы кроме одного получают ровно то, где они и стоят.

ВАЖНО: пока работает шлюз стенда, этим пользоваться нельзя — у топика команд
должен быть ровно один издатель, иначе рука встанет (см. docs/runbook.md).
"""

from __future__ import annotations

import argparse
import sys
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from sensor_msgs.msg import JointState
from std_msgs.msg import Float64MultiArray

#: Порядок в команде — из follower_controllers.yaml, НЕ алфавитный.
COMMAND_ORDER = [
    "shoulder_pan", "shoulder_lift", "elbow_flex",
    "wrist_flex", "wrist_roll", "gripper",
]

#: Больше этого за один раз не двигаем: шаг «первое движение» на то и первый.
MAX_STEP_RAD = 0.35   # ≈ 20°


class Jogger(Node):
    def __init__(self) -> None:
        super().__init__("so101_jog_joint")
        # joint_states публикуется BEST_EFFORT — подписка обязана совпасть,
        # иначе QoS не сматчится и сообщения не придут вообще.
        qos = QoSProfile(
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            durability=QoSDurabilityPolicy.VOLATILE,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=10,
        )
        self.latest: dict[str, float] | None = None
        self.create_subscription(JointState, "/follower/joint_states", self._on_state, qos)
        self.pub = self.create_publisher(
            Float64MultiArray, "/follower/forward_controller/commands", 10
        )

    def _on_state(self, msg: JointState) -> None:
        # Строго по имени: порядок в joint_states алфавитный и не совпадает
        # с порядком команды. Сопоставление по индексу поехало бы не тем суставом.
        self.latest = dict(zip(msg.name, msg.position))

    def wait_state(self, timeout: float = 5.0) -> dict[str, float]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            rclpy.spin_once(self, timeout_sec=0.1)
            if self.latest:
                return dict(self.latest)
        raise SystemExit("❌ Нет данных в /follower/joint_states — рука поднята?")


def main() -> int:
    ap = argparse.ArgumentParser(description="Сдвинуть один сустав на малый угол")
    ap.add_argument("joint", choices=COMMAND_ORDER)
    ap.add_argument("delta", type=float, help="приращение в радианах, например 0.1")
    ap.add_argument("--settle", type=float, default=2.5, help="сколько ждать доезда, с")
    args = ap.parse_args()

    if abs(args.delta) > MAX_STEP_RAD:
        raise SystemExit(
            f"❌ {args.delta} рад — слишком много за раз (предел {MAX_STEP_RAD}).\n"
            "   Первое движение делается малой амплитудой."
        )

    rclpy.init()
    node = Jogger()
    try:
        before = node.wait_state()
        missing = [j for j in COMMAND_ORDER if j not in before]
        if missing:
            raise SystemExit(f"❌ В joint_states нет суставов: {missing}")

        target = dict(before)
        target[args.joint] = before[args.joint] + args.delta

        print(f"{args.joint}: {before[args.joint]:+.4f} → {target[args.joint]:+.4f} рад "
              f"({args.delta:+.3f} рад = {args.delta * 57.2958:+.1f}°)")
        print("остальные суставы получают своё текущее положение\n")

        msg = Float64MultiArray()
        msg.data = [float(target[j]) for j in COMMAND_ORDER]

        # Публикуем несколько раз: издатель только что создан, и первое
        # сообщение может уйти до того, как контроллер оформит подписку.
        deadline = time.time() + args.settle
        while time.time() < deadline:
            node.pub.publish(msg)
            rclpy.spin_once(node, timeout_sec=0.05)

        after = node.wait_state()

        print(f"{'сустав':<15} {'было':>9} {'стало':>9} {'сдвиг':>9}  {'':>6}")
        print("─" * 55)
        moved_others = []
        for j in COMMAND_ORDER:
            d = after[j] - before[j]
            deg = d * 57.2958
            if j == args.joint:
                mark = "✅ цель" if abs(after[j] - target[j]) < 0.03 else "⚠️ не доехал"
            elif abs(d) > 0.02:
                mark = "❌ ПОЕХАЛ"
                moved_others.append(j)
            else:
                mark = "стоит"
            print(f"{j:<15} {before[j]:>+9.4f} {after[j]:>+9.4f} {deg:>+8.1f}°  {mark}")

        if moved_others:
            print(f"\n❌ Двинулись посторонние суставы: {', '.join(moved_others)}")
            print("   Это означает путаницу в порядке команды. Не продолжайте.")
            return 1
        print("\n✅ Поехал только тот сустав, которому дали цель.")
        return 0
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    sys.exit(main())
