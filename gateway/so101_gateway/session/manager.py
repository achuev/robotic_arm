"""Ядро очереди: кто управляет рукой, кто ждёт и когда ход меняется.

Модуль сознательно не знает ни про FastAPI, ни про WebSocket, ни про робота.
Он оперирует токенами и временем, а наружу отдаёт список событий, которые
транспортный слой превращает в сообщения протокола (см. docs/api.md §3).

Всё время берётся из инъецированных часов ``clock: Callable[[], float]``.
Прямых обращений к ``time.time()`` внутри логики нет — иначе тесты пришлось бы
писать через ``sleep``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from ..config import Config

log = logging.getLogger(__name__)

__all__ = [
    "ErrorCode",
    "Reason",
    "Event",
    "Granted",
    "Revoked",
    "QueueChanged",
    "GoHome",
    "EstopChanged",
    "SessionManager",
]


class ErrorCode:
    """Коды ошибок протокола (docs/api.md §3, «Коды ошибок»)."""

    NOT_CONTROLLER = "not_controller"
    NOT_QUEUED = "not_queued"
    QUEUE_FULL = "queue_full"
    COOLDOWN = "cooldown"
    RATE_LIMITED = "rate_limited"
    OUT_OF_RANGE = "out_of_range"
    IK_FAILED = "ik_failed"
    ESTOP = "estop"
    BAD_MESSAGE = "bad_message"


class Reason:
    """Причины в сообщении ``revoked``."""

    TIMEOUT = "timeout"
    IDLE = "idle"
    RELEASE = "release"
    ADMIN = "admin"
    ESTOP = "estop"
    DISCONNECT = "disconnect"


# --------------------------------------------------------------------------- #
# события
# --------------------------------------------------------------------------- #

class Event:
    """Базовый тип события менеджера."""


@dataclass(frozen=True)
class Granted(Event):
    """Ход выдан либо изменился его срок.

    ``duration_sec``/``expires_at`` = ``None`` означает «без ограничения»:
    посетитель на стенде один, торопить его некого. Как только в очередь
    кто-то встаёт, отсчёт начинается, и это же событие приходит повторно —
    уже со сроком.
    """

    token: str
    duration_sec: Optional[float]
    expires_at: Optional[float]


@dataclass(frozen=True)
class Revoked(Event):
    token: str
    reason: str


@dataclass(frozen=True)
class QueueChanged(Event):
    """Состав/порядок очереди изменился — разослать всем персональный ``queue``."""


@dataclass(frozen=True)
class GoHome(Event):
    """Руку надо увести в home: началась передача хода."""

    duration_sec: float


@dataclass(frozen=True)
class EstopChanged(Event):
    active: bool


# --------------------------------------------------------------------------- #
# клиент
# --------------------------------------------------------------------------- #

@dataclass
class ClientState:
    token: str
    connected: bool = True
    joined_at: float = 0.0
    disconnected_at: Optional[float] = None
    cooldown_until: float = 0.0
    #: Был ли клиент в очереди/управлял в момент обрыва — для отладки в админке.
    last_role: str = "observer"


# --------------------------------------------------------------------------- #
# менеджер
# --------------------------------------------------------------------------- #

class SessionManager:
    """Единственный источник истины о том, кто сейчас управляет рукой."""

    def __init__(self, config: Config, clock: Callable[[], float]) -> None:
        self._cfg = config
        self._clock = clock
        self._clients: Dict[str, ClientState] = {}
        self._waiting: List[str] = []
        self._controller: Optional[str] = None
        #: None — ход без срока: в очереди никого нет.
        self._control_expires_at: Optional[float] = None
        self._last_command_at: float = 0.0
        self._handover_until: Optional[float] = None
        self._estop: bool = False

    # ------------------------------------------------------------------ #
    # свойства
    # ------------------------------------------------------------------ #

    @property
    def config(self) -> Config:
        return self._cfg

    @property
    def controller(self) -> Optional[str]:
        return self._controller

    @property
    def control_expires_at(self) -> Optional[float]:
        return self._control_expires_at

    @property
    def queue_length(self) -> int:
        """Сколько человек ждёт (текущий оператор не считается)."""
        return len(self._waiting)

    @property
    def tokens(self) -> List[str]:
        return list(self._clients)

    @property
    def estop_active(self) -> bool:
        return self._estop

    @property
    def robot_mode(self) -> str:
        """``idle`` | ``moving`` | ``estop``.

        ``moving`` выставляется ТОЛЬКО на время передачи хода (ухода в home) —
        именно так это описано в docs/api.md §4. В обычной работе оператора
        режим остаётся ``idle``, иначе фронтенд, блокирующий органы управления
        при ``moving``, никогда не даст покомандовать. ``error`` добавляет
        транспортный слой, когда бэкенд робота отвалился.
        """
        if self._estop:
            return "estop"
        if self._handover_until is not None and self._clock() < self._handover_until:
            return "moving"
        return "idle"

    def is_controller(self, token: str) -> bool:
        return self._controller is not None and self._controller == token

    def is_queued(self, token: str) -> bool:
        return token in self._waiting

    def is_known(self, token: str) -> bool:
        return token in self._clients

    def cooldown_remaining(self, token: str) -> float:
        client = self._clients.get(token)
        if client is None:
            return 0.0
        return max(0.0, client.cooldown_until - self._clock())

    # ------------------------------------------------------------------ #
    # подключение / отключение
    # ------------------------------------------------------------------ #

    def hello(self, token: str) -> List[Event]:
        """Клиент представился (WS ``hello``).

        Возврат оператора в пределах RECONNECT_GRACE сохраняет ход: ему заново
        отдаётся ``granted`` с ИСХОДНЫМ ``expires_at`` — ход не продлевается.
        """
        now = self._clock()
        events: List[Event] = []
        client = self._clients.get(token)
        if client is None:
            client = ClientState(token=token, joined_at=now)
            self._clients[token] = client
            events.append(QueueChanged())
        else:
            was_disconnected = not client.connected
            client.connected = True
            client.disconnected_at = None
            if was_disconnected:
                events.append(QueueChanged())

        if self._controller == token:
            events.append(self._granted_event(token))
        return events

    def disconnect(self, token: str) -> List[Event]:
        """Обрыв связи.

        Оператору даётся RECONNECT_GRACE (истечёт в ``tick``). Наблюдатель и
        стоящий в очереди выбывают немедленно.
        """
        now = self._clock()
        client = self._clients.get(token)
        if client is None:
            return []

        if self._controller == token:
            client.connected = False
            client.disconnected_at = now
            client.last_role = "controller"
            return []

        if token in self._waiting:
            self._waiting.remove(token)
        self._clients.pop(token, None)
        return [QueueChanged()]

    # ------------------------------------------------------------------ #
    # очередь
    # ------------------------------------------------------------------ #

    def _granted_event(self, token: str) -> "Granted":
        """Событие ``granted`` с ФАКТИЧЕСКИМ сроком (возможно, без него)."""
        return Granted(
            token=token,
            duration_sec=self._cfg.control_duration if self._control_expires_at else None,
            expires_at=self._control_expires_at,
        )

    def _start_countdown_if_needed(self, now: float) -> List[Event]:
        """Запустить отсчёт, если появился первый ожидающий.

        До этого момента оператор был на стенде один и держал ход без срока.
        Как только кто-то встал в очередь, ему даётся полный CONTROL_DURATION
        с этой секунды — а не остаток от времени, которое он и так уже провёл.
        Иначе первый посетитель, простоявший десять минут в одиночестве,
        лишался бы хода ровно в тот миг, когда подошёл второй.
        """
        if self._controller is None or self._control_expires_at is not None:
            return []
        if not self._waiting:
            return []
        self._control_expires_at = now + self._cfg.control_duration
        log.info("в очереди появился ожидающий — отсчёт для %s до %.3f",
                 self._controller, self._control_expires_at)
        return [self._granted_event(self._controller)]

    def enqueue(self, token: str) -> Tuple[bool, Optional[str], List[Event]]:
        """Встать в очередь. Возвращает ``(ok, error_code, events)``."""
        now = self._clock()
        client = self._clients.get(token)
        if client is None:
            return False, ErrorCode.BAD_MESSAGE, []

        if self._controller == token or token in self._waiting:
            return True, None, []          # идемпотентно

        if client.cooldown_until > now:
            return False, ErrorCode.COOLDOWN, []

        # MAX_QUEUE считает всех участников очереди, включая того, кто держит ход
        # (он занимает позицию 0 в нумерации сообщения `queue`).
        occupants = len(self._waiting) + (1 if self._controller is not None else 0)
        if occupants >= self._cfg.max_queue:
            return False, ErrorCode.QUEUE_FULL, []

        self._waiting.append(token)
        client.last_role = "queued"
        events: List[Event] = [QueueChanged()]
        events.extend(self._start_countdown_if_needed(now))
        events.extend(self._maybe_grant(now))
        return True, None, events

    def release(self, token: str) -> List[Event]:
        """Досрочно отдать ход. Только для оператора.

        От всех остальных транспортный слой отвечает ``not_controller``;
        выход из очереди — это отдельное сообщение ``leave``.
        """
        now = self._clock()
        if self._controller != token:
            return []
        events = self._end_turn(now, Reason.RELEASE)
        events.extend(self._maybe_grant(now))
        return events

    def leave(self, token: str) -> List[Event]:
        """Выйти из очереди. Только для стоящего в ней.

        От всех остальных транспортный слой отвечает ``not_queued``.
        """
        if token not in self._waiting:
            return []
        self._waiting.remove(token)
        return [QueueChanged()]

    # ------------------------------------------------------------------ #
    # активность оператора
    # ------------------------------------------------------------------ #

    def note_activity(self, token: str, *, is_command: bool) -> None:
        """Отметить сообщение от клиента.

        Таймер бездействия сбрасывает только командная посылка. ``ping``
        приходит с ``is_command=False`` и от idle-таймаута не спасает
        (docs/api.md §4, «Бездействие»).
        """
        if not is_command:
            return
        if self._controller == token:
            self._last_command_at = self._clock()

    def can_command(self, token: str) -> Tuple[bool, Optional[str]]:
        """Можно ли принять команду от этого клиента прямо сейчас."""
        if self._estop:
            return False, ErrorCode.ESTOP
        if self._controller != token or self._controller is None:
            return False, ErrorCode.NOT_CONTROLLER
        if self._handover_until is not None and self._clock() < self._handover_until:
            # сюда попасть нельзя (во время передачи оператора нет), но пусть
            # инвариант будет явным
            return False, ErrorCode.NOT_CONTROLLER
        return True, None

    # ------------------------------------------------------------------ #
    # админ
    # ------------------------------------------------------------------ #

    def kick(self) -> List[Event]:
        """Снять текущего оператора, ход уходит следующему."""
        now = self._clock()
        if self._controller is None:
            return []
        events = self._end_turn(now, Reason.ADMIN)
        events.extend(self._maybe_grant(now))
        return events

    def estop(self) -> List[Event]:
        """Аварийная остановка: ход отзывается, очередь замирает."""
        now = self._clock()
        if self._estop:
            return []
        self._estop = True
        events: List[Event] = []
        if self._controller is not None:
            events.extend(self._end_turn(now, Reason.ESTOP, cooldown=False))
        self._handover_until = None
        events.append(EstopChanged(active=True))
        events.append(QueueChanged())
        return events

    def release_estop(self) -> List[Event]:
        """Снять аварийную остановку и вернуться к нормальной работе."""
        now = self._clock()
        if not self._estop:
            return []
        self._estop = False
        events: List[Event] = [EstopChanged(active=False)]
        # рука могла остаться в произвольной позе — уводим её в home, прежде чем
        # отдавать ход следующему
        self._handover_until = now + self._cfg.handover_home
        events.append(GoHome(duration_sec=self._cfg.handover_home))
        events.append(QueueChanged())
        return events

    # ------------------------------------------------------------------ #
    # такт времени
    # ------------------------------------------------------------------ #

    def tick(self) -> List[Event]:
        """Обработать истёкшие таймеры. Вызывается транспортным слоем ~10 Гц."""
        if self._estop:
            return []                     # очередь заморожена целиком

        now = self._clock()
        events: List[Event] = []

        if self._controller is not None:
            client = self._clients[self._controller]
            if (
                not client.connected
                and client.disconnected_at is not None
                and now - client.disconnected_at >= self._cfg.reconnect_grace
            ):
                events.extend(self._end_turn(now, Reason.DISCONNECT))
            elif self._control_expires_at is not None and now >= self._control_expires_at:
                events.extend(self._end_turn(now, Reason.TIMEOUT))
            elif now - self._last_command_at >= self._cfg.idle_timeout:
                events.extend(self._end_turn(now, Reason.IDLE))

        events.extend(self._maybe_grant(now))
        return events

    # ------------------------------------------------------------------ #
    # представления
    # ------------------------------------------------------------------ #

    def queue_view(self, token: str) -> Dict[str, object]:
        """Персональное содержимое сообщения ``queue``.

        ``position``: 0 — управляет, 1+ — место в очереди, -1 — просто зритель
        (в контракте такого значения нет, см. отчёт).
        ``ahead``: сколько ходов должно закончиться до твоего, включая текущего
        оператора. ``eta_sec = ahead * (CONTROL_DURATION + HANDOVER_HOME)``.
        """
        queue_length = len(self._waiting)
        if self._controller == token:
            return {
                "position": 0,
                "ahead": 0,
                "eta_sec": 0,
                "queue_length": queue_length,
                "you_control": True,
            }
        if token in self._waiting:
            index = self._waiting.index(token)
            ahead = index + (1 if self._controller is not None else 0)
            return {
                "position": index + 1,
                "ahead": ahead,
                "eta_sec": int(ahead * self._cfg.turn_slot_sec),
                "queue_length": queue_length,
                "you_control": False,
            }
        return {
            "position": -1,
            "ahead": 0,
            "eta_sec": 0,
            "queue_length": queue_length,
            "you_control": False,
        }

    def status(self) -> Dict[str, object]:
        """Публичный статус для ``GET /api/status`` (без поля ``robot``-error)."""
        occupied = self._controller is not None or self.robot_mode == "moving"
        ahead = len(self._waiting) + (1 if self._controller is not None else 0)
        return {
            "robot": self.robot_mode,
            "occupied": occupied,
            "queue_length": len(self._waiting),
            "estimated_wait_sec": int(ahead * self._cfg.turn_slot_sec),
        }

    def admin_snapshot(self) -> Dict[str, object]:
        """Полное состояние очереди для ``GET /api/admin/queue``."""
        now = self._clock()
        controller = None
        if self._controller is not None:
            client = self._clients[self._controller]
            controller = {
                "token": self._controller,
                "connected": client.connected,
                "expires_at": self._control_expires_at,
                "remaining_sec": (
                    None if self._control_expires_at is None
                    else max(0.0, self._control_expires_at - now)
                ),
                "idle_for_sec": max(0.0, now - self._last_command_at),
                "disconnected_for_sec": (
                    None if client.disconnected_at is None else now - client.disconnected_at
                ),
            }
        return {
            "now": now,
            "robot": self.robot_mode,
            "estop": self._estop,
            "handover_until": self._handover_until,
            "controller": controller,
            "queue": [
                {
                    "token": tok,
                    "position": i + 1,
                    "eta_sec": int(
                        (i + (1 if self._controller is not None else 0))
                        * self._cfg.turn_slot_sec
                    ),
                    "connected": self._clients[tok].connected,
                }
                for i, tok in enumerate(self._waiting)
            ],
            "observers": [
                tok
                for tok in self._clients
                if tok != self._controller and tok not in self._waiting
            ],
            "cooldowns": {
                tok: round(c.cooldown_until - now, 3)
                for tok, c in self._clients.items()
                if c.cooldown_until > now
            },
        }

    # ------------------------------------------------------------------ #
    # внутреннее
    # ------------------------------------------------------------------ #

    def _end_turn(self, now: float, reason: str, *, cooldown: bool = True) -> List[Event]:
        """Забрать ход у текущего оператора и (кроме estop) начать передачу."""
        token = self._controller
        assert token is not None
        client = self._clients.get(token)
        self._controller = None
        self._control_expires_at = None

        if client is not None:
            client.last_role = "observer"
            if cooldown:
                # COOLDOWN не начисляется только жертве estop: ход отобрали не
                # по её вине.
                client.cooldown_until = now + self._cfg.cooldown
            if not client.connected:
                # оператор так и не вернулся — окончательно забываем его
                self._clients.pop(token, None)

        events: List[Event] = [Revoked(token=token, reason=reason)]
        if reason != Reason.ESTOP:
            self._handover_until = now + self._cfg.handover_home
            events.append(GoHome(duration_sec=self._cfg.handover_home))
        events.append(QueueChanged())
        log.info("ход отобран у %s (%s)", token, reason)
        return events

    def _maybe_grant(self, now: float) -> List[Event]:
        """Выдать ход голове очереди, если это возможно прямо сейчас."""
        if self._estop or self._controller is not None:
            return []
        if self._handover_until is not None:
            if now < self._handover_until:
                return []
            self._handover_until = None
        if not self._waiting:
            return []

        token = self._waiting.pop(0)
        self._controller = token
        # Срок появляется, только если кто-то ждёт. Один посетитель на стенде
        # никого не задерживает, и выгонять его через 90 секунд незачем —
        # он просто уйдёт с ощущением, что ему не дали поиграть.
        self._control_expires_at = (
            now + self._cfg.control_duration if self._waiting else None
        )
        self._last_command_at = now
        client = self._clients.get(token)
        if client is not None:
            client.last_role = "controller"
        log.info("ход выдан %s до %s", token,
                 f"{self._control_expires_at:.3f}" if self._control_expires_at else "без срока")
        return [
            Granted(
                token=token,
                duration_sec=self._cfg.control_duration if self._control_expires_at else None,
                expires_at=self._control_expires_at,
            ),
            QueueChanged(),
        ]
