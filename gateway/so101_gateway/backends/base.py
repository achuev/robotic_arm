"""Интерфейс бэкенда робота.

Шлюз общается с рукой только через этот протокол. На macOS работает
``SimBackend`` (чистый Python), на стенде — ROS-бэкенд (появится позже в
``backends/ros2.py``; здесь про rclpy не знают вообще).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Protocol, Tuple, runtime_checkable

__all__ = ["RobotBackend", "JogResult", "RobotState"]

Point = Tuple[float, float, float]

#: Состояние робота: {"joints": {name: rad}, "ee": {"x","y","z"}, "connected": bool}
RobotState = Dict[str, object]


@dataclass(frozen=True)
class JogResult:
    """Результат декартова смещения схвата.

    ``error`` различает две причины отказа (docs/api.md §3): выход за
    объявленную ``workspace`` — это ``out_of_range`` с именем оси, а
    недостижимость точки ВНУТРИ зоны — ``ik_failed``. Проверка границ идёт
    первой, поэтому ``ik_failed`` никогда не возвращается для точки вне зоны.
    """

    ok: bool
    positions: Optional[Dict[str, float]] = None
    error: Optional[str] = None
    #: Ось, по которой запрашивали смещение — для адресного out_of_range.
    axis: Optional[str] = None
    #: Куда целились после подрезки в рабочую зону (для отладки и UI).
    point: Optional[Point] = None


@runtime_checkable
class RobotBackend(Protocol):
    """Минимум, который обязан уметь любой бэкенд."""

    def get_state(self) -> RobotState:
        """Текущее состояние: углы суставов, точка схвата, связь."""

    def send_joint_command(self, positions: Mapping[str, float]) -> None:
        """Отправить абсолютные цели суставов (уже прошедшие ограничитель)."""

    def go_home(self) -> None:
        """Увести руку в домашнюю позу (используется при передаче хода)."""

    def is_connected(self) -> bool:
        """Жив ли канал до железа. От этого зависит ``GET /api/health``."""

    # --- необязательное ---------------------------------------------- #
    # jog_ee(axis, delta) реализуют только бэкенды с кинематикой; наличие
    # метода включает features.cartesian в GET /api/config.
