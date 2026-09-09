"""Симулятор руки: чистый Python, без ROS.

Повторяет наблюдаемое поведение ``mock_components/GenericSystem`` из
ros2_control: позиция сустава едет к команде (здесь — апериодическое звено
первого порядка), команда подрезается по пределам URDF.

Кинематика НЕ своя: и FK, и IK берутся из ``so101_gateway.kinematics``, которая
собирает цепь ``base_link → gripper_frame_link`` из того же URDF, что читает
``robot_state_publisher`` на стенде. Поэтому симулятор и ROS-бэкенд (тот берёт
положение схвата из TF) совпадают по построению, а не по совпадению: в нулевой
позе обе модели дают x=+0.391, y=0.000, z=+0.226.

До этого здесь жила плоская трёхзвенная модель со своим соглашением об углах;
она расходилась с TF на треть метра и обслуживала другую рабочую зону.
"""

from __future__ import annotations

import logging
import math
from typing import Callable, Dict, Mapping, Optional

from ..config import Config
from ..kinematics import load_kinematics
from ..safety.limits import JOINT_ORDER, JointLimits
from ..safety.limiter import WorkspaceBox
from ..session.manager import ErrorCode
from .base import JogResult, Point, RobotState

log = logging.getLogger(__name__)

__all__ = ["SimBackend", "HOME_POSE", "PRESETS"]

#: Домашняя поза: рука сложена перед собой, схват с запасом внутри рабочей зоны.
#: По URDF-кинематике это (x, y, z) = (+0.293, 0.000, +0.206) — далеко и от
#: стола, и от полного вылета (0.47 м), где якобиан вырождается.
#: Знаки здесь — из URDF: положительный ``shoulder_lift`` опускает плечо,
#: положительный ``elbow_flex`` складывает локоть вниз.
HOME_POSE: Dict[str, float] = {
    "shoulder_pan": 0.0,
    "shoulder_lift": -0.9,
    "elbow_flex": 0.9,
    "wrist_flex": 0.0,
    "wrist_roll": 0.0,
    "gripper": 0.0,
}

#: Пресеты из контракта (``preset``: "home" | "wave").
#: «Взмах» — та же поза, поднятая выше: (+0.216, -0.008, +0.351).
PRESETS: Dict[str, Dict[str, float]] = {
    "home": dict(HOME_POSE),
    "wave": {
        "shoulder_pan": 0.0,
        "shoulder_lift": -1.2,
        "elbow_flex": 0.6,
        "wrist_flex": 0.0,
        "wrist_roll": 1.2,
        "gripper": 0.8,
    },
}


def _is_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


class SimBackend:
    """Модель руки для запуска шлюза без ROS."""

    def __init__(
        self,
        limits: JointLimits,
        config: Config,
        clock: Callable[[], float],
        tau: float = 0.05,
    ) -> None:
        self._limits = limits
        self._cfg = config
        self._clock = clock
        self._tau = max(1e-3, tau)
        self._workspace = WorkspaceBox.from_config(config)
        self._kin = load_kinematics(config.urdf_path, limits)

        start = {n: limits.clamp(n, HOME_POSE.get(n, 0.0)) for n in limits.names}
        self._pos: Dict[str, float] = dict(start)
        self._cmd: Dict[str, float] = dict(start)
        self._last_update = clock()

    # ------------------------------------------------------------------ #
    # протокол RobotBackend
    # ------------------------------------------------------------------ #

    def is_connected(self) -> bool:
        return True

    def get_state(self) -> RobotState:
        self._integrate()
        x, y, z = self.forward_kinematics(self._pos)
        return {
            "joints": dict(self._pos),
            "ee": {"x": x, "y": y, "z": z},
            "connected": True,
        }

    def send_joint_command(self, positions: Mapping[str, float]) -> None:
        self._integrate()
        for name, value in positions.items():
            if name in self._cmd and _is_number(value):
                self._cmd[name] = self._limits.clamp(name, float(value))

    def go_home(self) -> None:
        self.send_joint_command(HOME_POSE)

    # ------------------------------------------------------------------ #
    # пресеты
    # ------------------------------------------------------------------ #

    def preset(self, name: str) -> Optional[Dict[str, float]]:
        """Целевая поза пресета или ``None``, если пресет неизвестен.

        Пресет — одна поза, а не траектория: до ROS-бэкенда генератора
        траекторий нет, и «махание» получается одноразовым взмахом.
        """
        pose = PRESETS.get(name)
        if pose is None:
            return None
        return {n: self._limits.clamp(n, v) for n, v in pose.items()}

    # ------------------------------------------------------------------ #
    # кинематика
    # ------------------------------------------------------------------ #

    def forward_kinematics(self, joints: Mapping[str, float]) -> Point:
        """Углы → положение схвата. Цепь и origin-ы — из URDF."""
        return self._kin.forward_kinematics(joints)

    def inverse_kinematics(
        self, point: Point, seed: Optional[Mapping[str, float]] = None
    ) -> Optional[Dict[str, float]]:
        """Положение схвата → углы, либо ``None``, если не дотянуться.

        Численный спуск методом затухающих наименьших квадратов (см.
        ``kinematics.Kinematics.inverse_kinematics``). Задаётся ТОЛЬКО позиция:
        у руки пять степеней свободы, шести уравнений ей не потянуть.

        ``seed`` — стартовая поза спуска; по умолчанию текущая поза руки, чтобы
        решение оказалось ближайшим к ней и джог не «щёлкал» позой.
        """
        if seed is None:
            seed = self._pos
        return self._kin.inverse_kinematics(point, seed=seed)

    def jog_ee(
        self,
        axis: str,
        delta: object,
        from_joints: Optional[Mapping[str, float]] = None,
    ) -> JogResult:
        """Сместить схват на ``delta`` метров по оси ``axis``.

        Точка подрезается в рабочую зону (тогда в ответе ``out_of_range``, но
        движение всё равно исполняется), недостижимая точка даёт ``ik_failed``.
        """
        if axis not in ("x", "y", "z"):
            return JogResult(ok=False, error=ErrorCode.BAD_MESSAGE)
        if not _is_number(delta):
            return JogResult(ok=False, error=ErrorCode.BAD_MESSAGE, axis=axis)

        base = dict(from_joints) if from_joints is not None else self.get_state()["joints"]
        x, y, z = self.forward_kinematics(base)
        shift = {"x": [1.0, 0.0, 0.0], "y": [0.0, 1.0, 0.0], "z": [0.0, 0.0, 1.0]}[axis]
        target = (
            x + shift[0] * float(delta),
            y + shift[1] * float(delta),
            z + shift[2] * float(delta),
        )

        # ПОРЯДОК ВАЖЕН: сначала границы рабочей зоны, только потом IK.
        # Иначе клиент получил бы ik_failed там, где на деле упёрся в зону.
        (cx, cy, cz), out_of_zone = self._workspace.clamp(*target)

        # стартуем спуск из текущей позы, чтобы решение было ближайшим к ней
        solution = self.inverse_kinematics((cx, cy, cz), seed=base)

        if solution is None:
            # вне зоны — сообщаем именно об этом, даже если подрезанная точка
            # вдобавок недостижима; ik_failed остаётся для точек ВНУТРИ зоны
            error = ErrorCode.OUT_OF_RANGE if out_of_zone else ErrorCode.IK_FAILED
            return JogResult(ok=False, error=error, axis=axis, point=(cx, cy, cz))

        # суставы, которых декартов джог не касается, остаются как были
        positions = {n: float(base.get(n, 0.0)) for n in JOINT_ORDER}
        positions.update(solution)
        return JogResult(
            ok=True,
            positions=positions,
            error=ErrorCode.OUT_OF_RANGE if out_of_zone else None,
            axis=axis,
            point=(cx, cy, cz),
        )

    # ------------------------------------------------------------------ #
    # внутреннее
    # ------------------------------------------------------------------ #

    def _integrate(self) -> None:
        """Апериодическое звено первого порядка: pos → cmd."""
        now = self._clock()
        dt = now - self._last_update
        if dt <= 0.0:
            return
        self._last_update = now
        alpha = 1.0 - math.exp(-dt / self._tau)
        for name, cmd in self._cmd.items():
            self._pos[name] += (cmd - self._pos[name]) * alpha
