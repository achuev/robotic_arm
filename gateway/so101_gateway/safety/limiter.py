"""Ограничитель команд: через него проходит ЛЮБАЯ команда оператора.

Три независимых механизма:

* **клэмп** — цель подрезается по пределам суставов (источник — URDF), а
  клиенту сообщается, какой именно сустав не принял значение;
* **ограничение скорости** — лимитер ведёт собственную командную точку
  ``q_cmd`` и на каждом тике двигает её к цели не быстрее MAX_VEL_RAD_S.
  Скачок цели на весь диапазон превращается в плавный проезд;
* **watchdog** — следит за ЖИВОСТЬЮ соединения, а не за потоком команд. Если
  от клиента не приходит вообще ничего (ни команд, ни ``ping``) дольше
  WATCHDOG_TIMEOUT, движение замирает на текущей позиции. Уже принятая цель
  при живом клиенте доезжает до конца: повторять её не нужно.

Watchdog взводится только на время чьего-то хода (``arm_watchdog``). Когда
оператора нет, сторожить некого, и серверные движения — уход в ``home`` при
передаче хода, пресеты — доезжают без помех.

Плюс два вспомогательных: ``RateLimiter`` (MAX_MSG_PER_SEC на сессию) и
``WorkspaceBox`` (коробка рабочей зоны для декартова режима).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Callable, Dict, Mapping, Optional, Tuple

from ..config import Config
from ..safety.limits import GRIPPER, JointLimits
from ..session.manager import ErrorCode

__all__ = ["CommandLimiter", "CommandResult", "RateLimiter", "WorkspaceBox"]

Point = Tuple[float, float, float]


def _is_real_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


@dataclass(frozen=True)
class CommandResult:
    """Итог приёма команды.

    ``ok`` — исполнять ли команду (при ``out_of_range`` значение клэмпится, но
    движение НЕ отменяется). ``joint``/``axis`` говорят UI, что именно
    подсветить: контракт требует адресной ошибки ``out_of_range``.
    """

    ok: bool
    error: Optional[str] = None
    joint: Optional[str] = None
    axis: Optional[str] = None

    @property
    def extra(self) -> Dict[str, str]:
        if self.joint is not None:
            return {"joint": self.joint}
        if self.axis is not None:
            return {"axis": self.axis}
        return {}


# --------------------------------------------------------------------------- #
# рабочая зона
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class WorkspaceBox:
    """Прямоугольная рабочая зона схвата (метры)."""

    x: Tuple[float, float]
    y: Tuple[float, float]
    z: Tuple[float, float]

    @classmethod
    def from_config(cls, config: Config) -> "WorkspaceBox":
        return cls(x=config.workspace_x, y=config.workspace_y, z=config.workspace_z)

    def contains(self, x: float, y: float, z: float) -> bool:
        return (
            self.x[0] <= x <= self.x[1]
            and self.y[0] <= y <= self.y[1]
            and self.z[0] <= z <= self.z[1]
        )

    def clamp(self, x: float, y: float, z: float) -> Tuple[Point, bool]:
        """Подрезать точку в коробку. Второй элемент — «пришлось подрезать»."""
        cx = min(max(x, self.x[0]), self.x[1])
        cy = min(max(y, self.y[0]), self.y[1])
        cz = min(max(z, self.z[0]), self.z[1])
        clamped = (cx, cy, cz) != (x, y, z)
        return (cx, cy, cz), clamped

    def as_payload(self) -> Dict[str, list]:
        return {
            "x": [self.x[0], self.x[1]],
            "y": [self.y[0], self.y[1]],
            "z": [self.z[0], self.z[1]],
        }


# --------------------------------------------------------------------------- #
# частота сообщений
# --------------------------------------------------------------------------- #

class RateLimiter:
    """Скользящее окно на секунду. Один экземпляр на WS-сессию."""

    def __init__(self, max_per_sec: float, clock: Callable[[], float], window: float = 1.0):
        self._max = int(max_per_sec)
        self._clock = clock
        self._window = window
        self._hits: deque = deque()

    def allow(self) -> bool:
        now = self._clock()
        horizon = now - self._window
        while self._hits and self._hits[0] <= horizon:
            self._hits.popleft()
        if len(self._hits) >= self._max:
            return False
        self._hits.append(now)
        return True

    @property
    def used(self) -> int:
        return len(self._hits)


# --------------------------------------------------------------------------- #
# ограничитель команд
# --------------------------------------------------------------------------- #

class CommandLimiter:
    """Командная точка робота и всё, что её защищает."""

    #: Потолок шага интегрирования. Подвисший на минуту цикл не должен
    #: превращаться в один гигантский скачок командной точки.
    MAX_TICK_DT = 0.1

    def __init__(
        self,
        limits: JointLimits,
        config: Config,
        clock: Callable[[], float],
        initial: Optional[Mapping[str, float]] = None,
    ) -> None:
        self._limits = limits
        self._cfg = config
        self._clock = clock

        start = dict(initial) if initial else limits.zeros()
        self._q_cmd: Dict[str, float] = {
            name: limits.clamp(name, float(start.get(name, 0.0))) for name in limits.names
        }
        self._target: Dict[str, float] = dict(self._q_cmd)
        now = clock()
        self._last_tick = now
        self._last_seen_at = now
        self._armed = False
        self._held_for_watchdog = False

    # ------------------------------------------------------------------ #
    # состояние
    # ------------------------------------------------------------------ #

    @property
    def q_cmd(self) -> Dict[str, float]:
        """Командная точка в РАДИАНАХ (внутреннее представление)."""
        return dict(self._q_cmd)

    @property
    def target(self) -> Dict[str, float]:
        """Цель в РАДИАНАХ."""
        return dict(self._target)

    @property
    def watchdog_armed(self) -> bool:
        return self._armed

    @property
    def watchdog_tripped(self) -> bool:
        if not self._armed:
            return False
        return (self._clock() - self._last_seen_at) > self._cfg.watchdog_timeout

    # ------------------------------------------------------------------ #
    # watchdog
    # ------------------------------------------------------------------ #

    def arm_watchdog(self) -> None:
        """Ход выдан: с этого момента следим за живостью оператора."""
        self._armed = True
        self._last_seen_at = self._clock()
        self._held_for_watchdog = False

    def disarm_watchdog(self) -> None:
        """Хода нет: серверные движения доезжают без надзора."""
        self._armed = False
        self._held_for_watchdog = False

    def note_activity(self) -> None:
        """Любое сообщение от клиента — признак живого соединения.

        Кормит watchdog. Таймер БЕЗДЕЙСТВИЯ этим не сбрасывается: им ведает
        ``SessionManager`` и сбрасывают его только командные посылки.
        """
        self._last_seen_at = self._clock()
        self._held_for_watchdog = False

    # ------------------------------------------------------------------ #
    # приём команд с провода
    # ------------------------------------------------------------------ #

    def set_joints(self, positions: Mapping[str, object]) -> CommandResult:
        """Абсолютные цели суставов, пришедшие от клиента.

        Захват (``gripper``) на проводе всюду нормирован в 0..1 — здесь он
        переводится в радианы. Остальные суставы приходят в радианах.

        Значение вне пределов НЕ отбрасывается: оно подрезается и исполняется,
        но клиенту уходит ``out_of_range`` с именем сустава.
        """
        if not isinstance(positions, Mapping) or not positions:
            return CommandResult(ok=False, error=ErrorCode.BAD_MESSAGE)

        checked: Dict[str, float] = {}
        offender: Optional[str] = None
        for name, value in positions.items():
            if not isinstance(name, str) or not self._limits.has(name):
                return CommandResult(
                    ok=False,
                    error=ErrorCode.OUT_OF_RANGE,
                    joint=name if isinstance(name, str) else None,
                )
            if not _is_real_number(value):
                return CommandResult(ok=False, error=ErrorCode.BAD_MESSAGE, joint=name)

            fvalue = float(value)
            if name == GRIPPER:
                # доля 0..1 → радианы
                if not 0.0 <= fvalue <= 1.0 and offender is None:
                    offender = name
                checked[name] = self._limits.gripper_from_unit(fvalue)
                continue

            clamped = self._limits.clamp(name, fvalue)
            if clamped != fvalue and offender is None:
                offender = name
            checked[name] = clamped

        self._target.update(checked)
        self.note_activity()
        return CommandResult(
            ok=True,
            error=ErrorCode.OUT_OF_RANGE if offender else None,
            joint=offender,
        )

    def set_joint(self, name: str, value: object) -> CommandResult:
        return self.set_joints({name: value})

    def set_gripper_unit(self, value: object) -> CommandResult:
        """Сообщение ``gripper``: доля 0..1."""
        return self.set_joints({GRIPPER: value})

    # ------------------------------------------------------------------ #
    # внутренние (серверные) цели
    # ------------------------------------------------------------------ #

    def set_pose_rad(self, positions: Mapping[str, float]) -> CommandResult:
        """Поза целиком в РАДИАНАХ: пресеты, уход в home, решение IK.

        Нормировки захвата здесь нет: эти позы не приходят с провода.
        """
        if not isinstance(positions, Mapping) or not positions:
            return CommandResult(ok=False, error=ErrorCode.BAD_MESSAGE)
        for name, value in positions.items():
            if self._limits.has(name) and _is_real_number(value):
                self._target[name] = self._limits.clamp(name, float(value))
        return CommandResult(ok=True)

    def hold(self) -> None:
        """Остановить движение: цель = текущая командная точка."""
        self._target = dict(self._q_cmd)

    def reset(self, positions: Mapping[str, float]) -> None:
        """Синхронизировать командную точку с реальным положением руки."""
        for name in self._limits.names:
            if name in positions and _is_real_number(positions[name]):
                self._q_cmd[name] = self._limits.clamp(name, float(positions[name]))
        self._target = dict(self._q_cmd)

    # ------------------------------------------------------------------ #
    # такт
    # ------------------------------------------------------------------ #

    def tick(self) -> Dict[str, float]:
        """Продвинуть командную точку к цели. Возвращает новую ``q_cmd``."""
        now = self._clock()
        dt = now - self._last_tick
        self._last_tick = now
        if dt <= 0.0:
            return self.q_cmd

        if self.watchdog_tripped:
            # клиент замолчал совсем — удерживаем текущую позицию
            if not self._held_for_watchdog:
                self.hold()
                self._held_for_watchdog = True
            return self.q_cmd

        # телепорт невозможен ни при каких обстоятельствах
        dt = min(dt, self.MAX_TICK_DT)
        max_step = self._cfg.max_vel_rad_s * dt

        for name, goal in self._target.items():
            current = self._q_cmd[name]
            delta = goal - current
            if delta > max_step:
                delta = max_step
            elif delta < -max_step:
                delta = -max_step
            self._q_cmd[name] = current + delta
        return self.q_cmd
