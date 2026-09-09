"""Сборка приложения: сервис + FastAPI.

Запуск на macOS без ROS:

    ADMIN_TOKEN=secret uvicorn so101_gateway.app:create_app --factory

``create_app`` вызывается без аргументов — конфигурация читается из окружения,
бэкендом становится ``SimBackend``.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import logging
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

from fastapi import FastAPI, WebSocket

from .backends.base import RobotBackend
from .config import Config, load_config
from .gestures import GESTURES, PARK_POSE, Gesture
from .rest import build_router
from .safety.limiter import CommandLimiter, CommandResult, WorkspaceBox
from .safety.limits import GRIPPER, JointLimits, load_joint_limits
from .session.manager import (
    ErrorCode,
    EstopChanged,
    GoHome,
    Granted,
    QueueChanged,
    Revoked,
    SessionManager,
)
from .ws import ConnectionHub, websocket_endpoint

log = logging.getLogger(__name__)

__all__ = ["GatewayService", "create_app"]


def _num(value: float):
    """Целое, если значение целое — контракт обещает int в duration_sec."""
    fvalue = float(value)
    return int(fvalue) if fvalue.is_integer() else fvalue


class GatewayService:
    """Связывает очередь, ограничитель, бэкенд и соединения."""

    def __init__(
        self,
        config: Config,
        backend: RobotBackend,
        limits: JointLimits,
        clock: Callable[[], float],
    ) -> None:
        self.config = config
        self.backend = backend
        self.limits = limits
        self.clock = clock

        self.manager = SessionManager(config=config, clock=clock)
        self.workspace = WorkspaceBox.from_config(config)
        self.hub = ConnectionHub()

        #: Проигрываемый жест: список точек, индекс, накопленная поза и
        #: сроки. None — жеста нет.
        self._gesture: Optional[Gesture] = None
        self._gesture_idx: int = 0
        self._gesture_pose: Dict[str, float] = {}
        self._gesture_hold_until: float = 0.0
        self._gesture_deadline: float = 0.0

        #: С какого момента стенд свободен (нет оператора и очереди).
        self._free_since: Optional[float] = None
        #: Когда последний раз играли жест привлечения внимания.
        self._last_attract_at: float = 0.0

        state = backend.get_state()
        self.limiter = CommandLimiter(
            limits=limits, config=config, clock=clock, initial=state.get("joints")  # type: ignore[arg-type]
        )
        self._home_pose: Dict[str, float] = self._resolve_home_pose()
        self._homing_until = 0.0
        # Лимитер выше построен из get_state() в момент создания, а ROS-бэкенд
        # в этот момент ещё НЕ получил ни одного joint_states и честно отдаёт
        # нули. Публиковать их нельзя: на железе перезапуск шлюза увёл бы руку
        # из текущего положения в нули. Поэтому до первой достоверной обратной
        # связи не публикуем ничего, а получив её — синхронизируемся с ФАКТОМ.
        self._synced = False
        self._tasks: List[asyncio.Task] = []

    # ------------------------------------------------------------------ #
    # свойства
    # ------------------------------------------------------------------ #

    @property
    def cartesian_enabled(self) -> bool:
        """features.cartesian: доступен ли декартов режим.

        Мало того, что бэкенд его умеет — он должен быть ещё и разрешён.
        Флаг выключен по умолчанию: на стенде режим «точка» убран, и вкладка
        не появляется у клиента, а `jog_ee` отбивается на сервере. Одной
        проверки на клиенте недостаточно — команда приходит по WebSocket и
        может прийти от чего угодно.
        """
        return self.config.feature_cartesian and callable(getattr(self.backend, "jog_ee", None))

    def robot_mode(self) -> str:
        if not self.backend.is_connected():
            return "error"
        return self.manager.robot_mode

    def public_status(self) -> Dict[str, Any]:
        status = self.manager.status()
        status["robot"] = self.robot_mode()
        return status

    def client_config(self) -> Dict[str, Any]:
        cfg = self.config
        return {
            "protocol": cfg.protocol,
            "joints": self.limits.as_config_payload(),
            "workspace": self.workspace.as_payload(),
            "ee_jog_step_m": cfg.ee_jog_step_m,
            "control_duration_sec": int(cfg.control_duration),
            "idle_timeout_sec": int(cfg.idle_timeout),
            "cooldown_sec": int(cfg.cooldown),
            "reconnect_grace_sec": int(cfg.reconnect_grace),
            "watchdog_timeout_sec": int(cfg.watchdog_timeout),
            "max_msg_per_sec": int(cfg.max_msg_per_sec),
            "max_queue": cfg.max_queue,
            "video_url": cfg.video_url,
            "features": {"cartesian": self.cartesian_enabled, "video": cfg.feature_video},
        }

    # ------------------------------------------------------------------ #
    # жизненный цикл
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._control_loop(), name="so101-control"),
            asyncio.create_task(self._state_loop(), name="so101-state"),
        ]

    async def stop(self) -> None:
        # Парковка ДО остановки циклов: после cancel() публиковать команды
        # уже некому, и рука останется там, где её застали. Циклы при этом
        # ещё работают, поэтому park() двигает лимитер сам — иначе два
        # источника тикали бы его одновременно.
        if self.config.park_on_shutdown:
            for task in self._tasks:
                task.cancel()
            for task in self._tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self._tasks = []
            try:
                await self.park()
            except Exception:
                # Не «suppress»: молчаливое проглатывание здесь однажды уже
                # спрятало настоящую причину — рука двинулась на треть секунды
                # и встала, а в логе не было ни ошибки, ни отчёта о доезде.
                log.exception("мягкая посадка не удалась")

        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []

    async def _control_loop(self) -> None:
        """CMD_RATE_HZ: продвигаем командную точку и публикуем её в железо."""
        period = 1.0 / max(1.0, self.config.cmd_rate_hz)
        while True:
            await asyncio.sleep(period)
            try:
                if not self._synced:
                    if not self.backend.is_connected():
                        continue          # где рука — неизвестно, молчим
                    measured = self.backend.get_state().get("joints") or {}
                    # reset() заодно сбрасывает цель в текущую точку — это его
                    # прямое назначение («мы узнали, где рука, и не двигаемся»),
                    # но успей оператор отдать команду до первого такта, она бы
                    # пропала. Поэтому цель сохраняем и возвращаем.
                    pending = (
                        dict(self.limiter.target)
                        if self.manager.controller is not None
                        else None
                    )
                    self.limiter.reset(measured)                # type: ignore[arg-type]
                    # Холостой такт: с момента создания лимитера прошло время,
                    # и первый же tick() отработал бы его целиком одним шагом.
                    # Сейчас цель равна текущей точке, поэтому такт ничего не
                    # двигает — он только обнуляет накопленное время.
                    self.limiter.tick()
                    self._synced = True
                    if pending is not None:
                        self.limiter.set_pose_rad(pending)
                    else:
                        # Стенд встречает первого посетителя в домашней позе, а
                        # не в той, где рука осталась. Едем туда через
                        # ограничитель скорости, а не вызовом backend.go_home():
                        # иначе рука дёрнулась бы на полной скорости мимо MAX_VEL.
                        self._begin_homing(self.config.handover_home)
                if self.manager.estop_active:
                    # публикация команд прекращается, рука замирает
                    self._cancel_gesture()
                    self.limiter.hold()
                    continue
                # Жест продвигается ЗДЕСЬ, а не по таймеру: точка считается
                # пройденной по фактическому доезду командной точки, поэтому
                # ограничитель скорости остаётся хозяином положения.
                self._maybe_attract()
                self._advance_gesture()
                positions = self.limiter.tick()
                self.backend.send_joint_command(positions)
            except Exception:                          # pragma: no cover
                log.exception("сбой в командном цикле")

    async def _state_loop(self) -> None:
        """STATE_RATE_HZ: таймеры очереди + рассылка ``state`` всем."""
        period = 1.0 / max(1.0, self.config.state_rate_hz)
        while True:
            await asyncio.sleep(period)
            try:
                await self.dispatch(self.manager.tick())
                await self.hub.broadcast(self.state_payload())
            except Exception:                          # pragma: no cover
                log.exception("сбой в цикле состояния")

    # ------------------------------------------------------------------ #
    # исходящие сообщения
    # ------------------------------------------------------------------ #

    def state_payload(self) -> Dict[str, Any]:
        state = self.backend.get_state()
        joints = {k: round(float(v), 5) for k, v in dict(state.get("joints", {})).items()}
        if GRIPPER in joints:
            # захват на проводе всюду в долях 0..1 (docs/api.md §2), чтобы
            # слайдер клиента и state говорили на одном языке
            joints[GRIPPER] = round(self.limits.gripper_to_unit(joints[GRIPPER]), 5)
        ee = {k: round(float(v), 5) for k, v in dict(state.get("ee", {})).items()}
        return {
            "t": "state",
            "joints": joints,
            "ee": ee,
            "robot": self.robot_mode(),
            "ts": self.clock(),
        }

    async def broadcast_queue(self) -> None:
        for token in self.hub.tokens:
            await self.hub.send_to_token(
                token, {"t": "queue", **self.manager.queue_view(token)}
            )

    async def dispatch(self, events: Iterable[Any]) -> None:
        """Превратить события менеджера в сообщения протокола."""
        queue_dirty = False
        for event in events:
            if isinstance(event, Granted):
                # ход выдан живому оператору — с этого момента сторожим связь
                self.limiter.arm_watchdog()
                await self.hub.send_to_token(
                    event.token,
                    {
                        "t": "granted",
                        # None означает «без срока»: посетитель на стенде один.
                        # Отсчёт начнётся, когда в очередь кто-то встанет, и
                        # это же сообщение придёт повторно — уже со сроком.
                        "duration_sec": (
                            None if event.duration_sec is None else _num(event.duration_sec)
                        ),
                        "expires_at": event.expires_at,
                    },
                )
            elif isinstance(event, Revoked):
                # оператора нет — серверные движения доезжают без надзора
                self.limiter.disarm_watchdog()
                await self.hub.send_to_token(
                    event.token, {"t": "revoked", "reason": event.reason}
                )
            elif isinstance(event, GoHome):
                self._begin_homing(event.duration_sec)
            elif isinstance(event, EstopChanged):
                if event.active:
                    self.limiter.hold()
                    self._homing_until = 0.0
            elif isinstance(event, QueueChanged):
                queue_dirty = True
        if queue_dirty:
            await self.broadcast_queue()

    # ------------------------------------------------------------------ #
    # входящие сообщения
    # ------------------------------------------------------------------ #

    async def on_hello(self, conn, token: str) -> None:
        """Клиент представился.

        Второе подключение по тому же токену ВЫТЕСНЯЕТ старое: человек на
        стенде регулярно открывает вторую вкладку, и это не должно стоить ему
        хода. Новое соединение привязывается ДО закрытия старого — иначе
        ``on_disconnect`` увидел бы токен без соединений и снял бы человека
        с очереди.
        """
        self.hub.attach(conn, token)
        await self.hub.evict_others(conn, token)
        # hello от вернувшегося в grace-окне оператора заново отдаёт granted с
        # исходным expires_at (docs/api.md §4)
        await self.dispatch(self.manager.hello(token))
        await conn.send({"t": "queue", **self.manager.queue_view(token)})
        await conn.send(self.state_payload())

    async def on_disconnect(self, conn) -> None:
        token = conn.token
        self.hub.remove(conn)
        if token is not None and not self.hub.has_token(token):
            await self.dispatch(self.manager.disconnect(token))

    async def on_enqueue(self, conn) -> None:
        ok, error, events = self.manager.enqueue(conn.token)
        if not ok:
            await self._send_error(conn, error)
            return
        await self.dispatch(events)
        await conn.send({"t": "queue", **self.manager.queue_view(conn.token)})

    async def on_leave(self, conn) -> None:
        """``leave`` — только от стоящего в очереди."""
        if not self.manager.is_queued(conn.token):
            await self._send_error(conn, ErrorCode.NOT_QUEUED)
            return
        await self.dispatch(self.manager.leave(conn.token))
        await conn.send({"t": "queue", **self.manager.queue_view(conn.token)})

    async def on_command(self, conn, kind: str, msg: Dict[str, Any]) -> None:
        token = conn.token

        # release — только от оператора; всем остальным not_controller
        if kind == "release":
            if not self.manager.is_controller(token):
                await self._send_error(conn, ErrorCode.NOT_CONTROLLER)
                return
            await self.dispatch(self.manager.release(token))
            return

        allowed, error = self.manager.can_command(token)
        if not allowed:
            await self._send_error(conn, error)
            return

        # любая командная посылка сбрасывает таймер бездействия
        self.manager.note_activity(token, is_command=True)

        # Ручная команда прерывает жест: человек, схватившийся за слайдер
        # посреди махания, получает управление немедленно. `preset` сам решает,
        # начать новый жест или отменить текущий.
        if kind != "preset":
            self._cancel_gesture()

        if kind == "set_joint":
            joint, position = msg.get("joint"), msg.get("position")
            if not isinstance(joint, str):
                await conn.send_error(ErrorCode.BAD_MESSAGE, "нет поля joint")
                return
            result = self.limiter.set_joint(joint, position)
        elif kind == "set_joints":
            positions = msg.get("positions")
            if not isinstance(positions, dict):
                await conn.send_error(ErrorCode.BAD_MESSAGE, "нет поля positions")
                return
            result = self.limiter.set_joints(positions)
        elif kind == "gripper":
            result = self.limiter.set_gripper_unit(msg.get("value"))
        elif kind == "preset":
            result = self._apply_preset(msg.get("name"))
        elif kind == "jog_ee":
            result = self._apply_jog(msg.get("axis"), msg.get("delta"))
        else:                                          # pragma: no cover
            return

        if result.error is not None:
            await self._send_error(conn, result.error, **result.extra)

    # ------------------------------------------------------------------ #
    # внутреннее
    # ------------------------------------------------------------------ #

    # --- жесты ---------------------------------------------------------- #

    #: Насколько командная точка должна подойти к цели, чтобы считать точку
    #: пройденной. 0.02 рад ≈ 1.1° — меньше, чем видно глазом.
    GESTURE_EPS = 0.02
    #: Предел ожидания одной точки. Страховка: если сустав упёрся и цель
    #: недостижима, жест не должен зависнуть навсегда.
    GESTURE_WAYPOINT_TIMEOUT = 5.0

    def _start_gesture(self, name: str) -> CommandResult:
        """Запустить жест по имени."""
        gesture = GESTURES.get(name)
        if gesture is None:
            return CommandResult(ok=False, error=ErrorCode.BAD_MESSAGE)
        return self._play(gesture)

    def _play(self, gesture: Gesture) -> CommandResult:
        """Начать проигрывание последовательности с первой точки."""
        self._gesture = gesture
        self._gesture_idx = -1          # _advance перейдёт на нулевую
        self._gesture_pose = {}
        self._gesture_hold_until = 0.0
        self._gesture_deadline = 0.0
        self._advance_gesture(force=True)
        return CommandResult(ok=True)

    def _cancel_gesture(self) -> None:
        """Прервать жест.

        Зовётся на любой ручной команде: человек, схватившийся за слайдер
        посреди махания, должен получить управление немедленно, а не досматривать
        анимацию. Рука при этом не дёргается — цель просто перестаёт меняться.
        """
        self._gesture = None

    def _advance_gesture(self, force: bool = False) -> None:
        """Перейти к следующей точке жеста, если текущая пройдена."""
        if self._gesture is None:
            return
        now = self.clock()

        if not force:
            if now < self._gesture_hold_until:
                return
            target = self.limiter.target
            q = self.limiter.q_cmd
            reached = all(
                abs(target.get(j, 0.0) - q.get(j, 0.0)) <= self.GESTURE_EPS
                for j in target
            )
            if not reached and now < self._gesture_deadline:
                return

        self._gesture_idx += 1
        if self._gesture_idx >= len(self._gesture):
            self._gesture = None
            return

        pose, hold = self._gesture[self._gesture_idx]
        # Точки после первой могут быть ЧАСТИЧНЫМИ — в них перечислены только
        # те суставы, что двигаются. Остальные обязаны остаться там, где их
        # оставила предыдущая точка, иначе «кивок» на каждом такте возвращал бы
        # плечо в ноль.
        self._gesture_pose.update(pose)
        self.limiter.set_pose_rad(self._gesture_pose)
        self._gesture_hold_until = 0.0 if hold <= 0 else now + hold
        self._gesture_deadline = now + self.GESTURE_WAYPOINT_TIMEOUT

    # --- привлечение внимания -------------------------------------------- #

    def _maybe_attract(self) -> None:
        """Пошевелиться, когда стенд давно пустует.

        Стоящий манипулятор читается как выключенный, и мимо него проходят.
        Шевелящийся притягивает взгляд — ради этого стенд и стоит.

        Жест играется, только когда робот НИКОМУ не принадлежит: ни оператора,
        ни очереди. Появился человек — отсчёт простоя начинается заново, а
        начатый жест обрывается при передаче хода (`_begin_homing`).
        """
        cfg = self.config
        if not cfg.attract_enabled or not self._synced:
            return
        if self.manager.estop_active:
            return
        if self.manager.controller is not None or self.manager.queue_length > 0:
            self._free_since = None
            return
        if self._gesture is not None:
            return

        now = self.clock()
        if self._free_since is None:
            self._free_since = now
            return
        if now - self._free_since < cfg.attract_after_sec:
            return
        if now - self._last_attract_at < cfg.attract_period_sec:
            return

        name = random.choice(sorted(GESTURES))
        self._last_attract_at = now
        # Отсчёт простоя с этого момента: иначе следующий жест начался бы
        # сразу после текущего, и рука махала бы без остановки.
        self._free_since = now
        log.info("никого нет %.0f с — играю жест «%s»", cfg.attract_after_sec, name)
        # В конец добавляется home: рука не должна оставаться в поднятой позе,
        # иначе следующий посетитель встретит её задранной, а не в home.
        self._play(list(GESTURES[name]) + [(dict(self._resolve_home_pose()), 0.0)])

    # --- мягкая посадка -------------------------------------------------- #

    async def park(self) -> bool:
        """Увести руку в низкую позу и дождаться доезда.

        Зовётся перед остановкой стенда. Без этого рука остаётся там, где её
        застали, и при снятии момента падает с этой высоты. Поза парковки —
        схват в 5 см над столом, путь из home проверен по FK.

        Возвращает True, если доехали; False — если не успели за отведённое
        время (тогда всё равно останавливаемся, ждать дольше нельзя).
        """
        if not self.backend.is_connected():
            return False
        self._cancel_gesture()
        self.limiter.set_pose_rad(PARK_POSE)
        log.info("мягкая посадка: увожу руку в позу парковки")

        period = 1.0 / max(1.0, self.config.cmd_rate_hz)
        deadline = self.clock() + self.config.park_timeout_sec
        while self.clock() < deadline:
            await asyncio.sleep(period)
            positions = self.limiter.tick()
            self.backend.send_joint_command(positions)
            q = self.limiter.q_cmd
            if all(abs(PARK_POSE[j] - q.get(j, 0.0)) <= self.GESTURE_EPS for j in PARK_POSE):
                log.info("мягкая посадка: рука на месте")
                return True
        log.warning("мягкая посадка: не успели за %.1f с", self.config.park_timeout_sec)
        return False

    def _apply_preset(self, name: Any):
        if isinstance(name, str) and name in GESTURES:
            return self._start_gesture(name)
        # Не жест — значит одиночная поза от бэкенда («домой»).
        self._cancel_gesture()
        getter = getattr(self.backend, "preset", None)
        pose = getter(name) if callable(getter) and isinstance(name, str) else None
        if not pose:
            return CommandResult(ok=False, error=ErrorCode.BAD_MESSAGE)
        return self.limiter.set_pose_rad(pose)

    def _apply_jog(self, axis: Any, delta: Any) -> CommandResult:
        if not self.cartesian_enabled:
            # Режим выключен флагом либо бэкенд его не умеет. features.cartesian
            # уже сказал об этом клиенту, но проверять надо и здесь: сообщение
            # приходит по WebSocket, а туда может прийти что угодно.
            return CommandResult(ok=False, error=ErrorCode.IK_FAILED)
        jog = getattr(self.backend, "jog_ee", None)
        if not callable(jog):
            return CommandResult(ok=False, error=ErrorCode.IK_FAILED)
        if not isinstance(axis, str):
            return CommandResult(ok=False, error=ErrorCode.BAD_MESSAGE)

        # считаем от текущей ЦЕЛИ ограничителя, а не от измеренной позы:
        # иначе быстрые повторные джоги накапливаются неверно
        result = jog(axis, delta, from_joints=self.limiter.target)
        if not result.ok:
            return CommandResult(ok=False, error=result.error, axis=result.axis)

        # решение IK — уже радианы, нормировать захват не надо
        self.limiter.set_pose_rad(result.positions)
        return CommandResult(ok=True, error=result.error, axis=result.axis)

    async def _send_error(self, conn, code: Optional[str], **extra: Any) -> None:
        code, message, described = self._describe(code, conn.token)
        described.update(extra)
        await conn.send_error(code, message, **described)

    def _describe(self, code: Optional[str], token: Optional[str]):
        """Код ошибки → (code, message, extra) для WS-сообщения ``error``."""
        code = code or ErrorCode.BAD_MESSAGE
        messages = {
            ErrorCode.NOT_CONTROLLER: "роботом управляет кто-то другой",
            ErrorCode.NOT_QUEUED: "вы не в очереди",
            ErrorCode.QUEUE_FULL: "очередь заполнена",
            ErrorCode.RATE_LIMITED: "слишком много сообщений",
            ErrorCode.OUT_OF_RANGE: "значение вне допустимого диапазона",
            ErrorCode.IK_FAILED: "сюда рука не дотянется",
            ErrorCode.ESTOP: "активна аварийная остановка",
            ErrorCode.BAD_MESSAGE: "некорректное сообщение",
        }
        if code == ErrorCode.COOLDOWN:
            remaining = self.manager.cooldown_remaining(token or "")
            return (
                ErrorCode.COOLDOWN,
                f"дайте другим попробовать, ещё {int(remaining) + 1} с",
                {"retry_after_sec": round(remaining, 1)},
            )
        return (code, messages.get(code, "ошибка"), {})

    def _resolve_home_pose(self) -> Dict[str, float]:
        getter = getattr(self.backend, "preset", None)
        if callable(getter):
            pose = getter("home")
            if pose:
                return dict(pose)
        return self.limits.zeros()

    def _begin_homing(self, duration: float) -> None:
        # Передача хода прерывает начатый жест: следующий оператор должен
        # получить руку в home, а не досматривать чужое махание.
        self._cancel_gesture()
        """Передача хода: рука едет в home через ограничитель скорости.

        Watchdog в этот момент разоружён (оператора нет), поэтому движение
        доезжает до конца само — специальных подпорок не нужно.
        """
        self._homing_until = self.clock() + duration
        self.limiter.set_pose_rad(self._home_pose)


# --------------------------------------------------------------------------- #
# фабрика приложения
# --------------------------------------------------------------------------- #

def create_app(
    backend: Optional[RobotBackend] = None,
    config: Optional[Config] = None,
    clock: Optional[Callable[[], float]] = None,
) -> FastAPI:
    """Собрать FastAPI-приложение.

    Без аргументов пригодно для ``uvicorn ... --factory``.
    """
    cfg = config or load_config()
    tick = clock or time.time

    # Uvicorn настраивает СВОИ логгеры, а прикладные оставляет на умолчании
    # (WARNING). Из-за этого «играю жест», «мягкая посадка», «ход выдан» не
    # попадали в вывод контейнера — стенд работал молча, и понять по логам,
    # что он делал ночью, было нельзя.
    if not logging.getLogger("so101_gateway").handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
    limits = load_joint_limits(cfg.urdf_path)

    if backend is None:
        if cfg.backend == "sim":
            from .backends.sim import SimBackend

            backend = SimBackend(limits=limits, config=cfg, clock=tick)
        else:
            # rclpy импортируется исключительно внутри backends.ros2 и только
            # если бэкенд действительно запрошен
            from .backends.ros2 import Ros2Backend  # type: ignore[attr-defined]

            backend = Ros2Backend(limits=limits, config=cfg, clock=tick)

    service = GatewayService(config=cfg, backend=backend, limits=limits, clock=tick)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    app = FastAPI(title="SO-101 gateway", version="1", lifespan=lifespan)
    app.state.service = service
    app.include_router(build_router(service))

    @app.websocket("/ws")
    async def ws_route(websocket: WebSocket) -> None:
        await websocket_endpoint(websocket, service)

    log.info(
        "шлюз собран: бэкенд=%s, пределы=%s, очередь=%d, ход=%.0f с",
        type(backend).__name__,
        limits.source,
        cfg.max_queue,
        cfg.control_duration,
    )
    return app
