"""WebSocket-протокол шлюза (docs/api.md §3).

Здесь только транспорт и разбор сообщений: вся логика очереди живёт в
``session.manager``, вся логика безопасности — в ``safety.limiter``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Set

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from .config import Config
from .safety.limiter import RateLimiter
from .session.manager import ErrorCode

log = logging.getLogger(__name__)

__all__ = ["Connection", "ConnectionHub", "websocket_endpoint", "COMMAND_TYPES"]

#: Сообщения, которые может слать только оператор (docs/api.md §3).
COMMAND_TYPES = frozenset({"set_joint", "set_joints", "jog_ee", "gripper", "preset", "release"})

#: Сообщения, требующие нахождения в очереди.
QUEUE_TYPES = frozenset({"leave"})

#: Максимальная длина токена, которую вообще имеет смысл принимать.
MAX_TOKEN_LEN = 128

#: Код закрытия для соединения, вытесненного второй вкладкой того же токена.
#:
#: Именно частный код 4409, а НЕ 1000. Замысел с 1000 («обычное закрытие, не
#: ошибка») не работает: 1000 неотличим от закрытия при уходе со страницы или
#: остановке сервера, и клиент с переподключением полезет обратно. А значит
#: вытеснит вкладку, которая только что вытеснила его, та вытеснит его снова —
#: и две вкладки будут выбивать друг друга по кругу, долбя шлюз.
#: Диапазон 4000–4999 зарезервирован для приложения ровно под такие случаи.
WS_EVICTED_CODE = 4409


class Connection:
    """Одно WS-соединение. У одного токена их может быть несколько (вкладки)."""

    def __init__(self, websocket: WebSocket, config: Config, clock) -> None:
        self.ws = websocket
        self.token: Optional[str] = None
        self.rate = RateLimiter(config.max_msg_per_sec, clock)
        self.alive = True

    async def send(self, payload: Dict[str, Any]) -> None:
        if not self.alive:
            return
        try:
            await self.ws.send_json(payload)
        except (WebSocketDisconnect, RuntimeError):
            self.alive = False
        except Exception:                      # pragma: no cover - защита цикла
            log.debug("не удалось отправить сообщение", exc_info=True)
            self.alive = False

    async def send_error(self, code: str, message: str, **extra: Any) -> None:
        await self.send({"t": "error", "code": code, "message": message, **extra})


class ConnectionHub:
    """Реестр живых соединений."""

    def __init__(self) -> None:
        self._all: Set[Connection] = set()
        self._by_token: Dict[str, Set[Connection]] = {}

    def add(self, conn: Connection) -> None:
        self._all.add(conn)

    def attach(self, conn: Connection, token: str) -> None:
        conn.token = token
        self._by_token.setdefault(token, set()).add(conn)

    def remove(self, conn: Connection) -> None:
        self._all.discard(conn)
        if conn.token is not None:
            peers = self._by_token.get(conn.token)
            if peers is not None:
                peers.discard(conn)
                if not peers:
                    self._by_token.pop(conn.token, None)

    def has_token(self, token: str) -> bool:
        return bool(self._by_token.get(token))

    def peers(self, token: str) -> List[Connection]:
        return list(self._by_token.get(token, ()))

    async def evict_others(self, keep: Connection, token: str) -> int:
        """Закрыть прежние соединения этого токена.

        Второе подключение вытесняет первое (docs/api.md §4). Место в очереди
        и ход остаются за токеном: ``keep`` уже привязан к нему, поэтому
        ``has_token`` останется истинным и ``on_disconnect`` не снимет человека.
        """
        evicted = 0
        for conn in self.peers(token):
            if conn is keep:
                continue
            conn.alive = False
            try:
                await conn.ws.close(code=WS_EVICTED_CODE)
            except Exception:              # соединение уже мертво — не страшно
                log.debug("не удалось закрыть вытесненное соединение", exc_info=True)
            self.remove(conn)
            evicted += 1
        if evicted:
            log.info("вытеснено соединений по токену: %d", evicted)
        return evicted

    @property
    def tokens(self) -> List[str]:
        return list(self._by_token)

    @property
    def connections(self) -> List[Connection]:
        return list(self._all)

    async def broadcast(self, payload: Dict[str, Any]) -> None:
        for conn in list(self._all):
            await conn.send(payload)

    async def send_to_token(self, token: str, payload: Dict[str, Any]) -> None:
        for conn in list(self._by_token.get(token, ())):
            await conn.send(payload)


# --------------------------------------------------------------------------- #
# разбор входящих сообщений
# --------------------------------------------------------------------------- #

async def handle_raw(service, conn: Connection, raw: str) -> None:
    """Разобрать и исполнить одно входящее сообщение."""
    # ЛЮБОЙ пришедший кадр — доказательство того, что соединение живо, поэтому
    # watchdog кормится раньше всех проверок, в том числе rate limit.
    # Таймер бездействия при этом НЕ сбрасывается: им ведает SessionManager.
    if conn.token is not None and service.manager.is_controller(conn.token):
        service.limiter.note_activity()

    if not conn.rate.allow():
        await conn.send_error(ErrorCode.RATE_LIMITED, "слишком много сообщений")
        return

    try:
        msg = json.loads(raw)
    except (ValueError, TypeError):
        await conn.send_error(ErrorCode.BAD_MESSAGE, "некорректный JSON")
        return

    if not isinstance(msg, dict):
        await conn.send_error(ErrorCode.BAD_MESSAGE, "ожидается JSON-объект")
        return

    kind = msg.get("t")
    if not isinstance(kind, str):
        await conn.send_error(ErrorCode.BAD_MESSAGE, "нет поля t")
        return

    # hello обязателен первым сообщением
    if conn.token is None:
        if kind != "hello":
            await conn.send_error(ErrorCode.BAD_MESSAGE, "первым сообщением должен быть hello")
            return
        token = msg.get("token")
        if not isinstance(token, str) or not (0 < len(token) <= MAX_TOKEN_LEN):
            await conn.send_error(ErrorCode.BAD_MESSAGE, "нет поля token")
            return
        await service.on_hello(conn, token)
        return

    if kind == "hello":
        # повторный hello в том же соединении: просто подтверждаем состояние
        await conn.send({"t": "queue", **service.manager.queue_view(conn.token)})
        return

    if kind == "ping":
        # ping НЕ сбрасывает таймер бездействия (docs/api.md §4)
        service.manager.note_activity(conn.token, is_command=False)
        await conn.send({"t": "pong", "ts": service.clock()})
        return

    if kind == "enqueue":
        await service.on_enqueue(conn)
        return

    if kind == "leave":
        await service.on_leave(conn)
        return

    if kind in COMMAND_TYPES:
        await service.on_command(conn, kind, msg)
        return

    # неизвестный тип игнорируется — совместимость вперёд
    log.debug("игнорирую неизвестное сообщение %r", kind)


async def websocket_endpoint(websocket: WebSocket, service) -> None:
    """Точка входа ``/ws``."""
    await websocket.accept()
    conn = Connection(websocket, service.config, service.clock)
    service.hub.add(conn)
    try:
        while True:
            raw = await websocket.receive_text()
            await handle_raw(service, conn, raw)
    except WebSocketDisconnect:
        pass
    except RuntimeError:
        # соединение закрыли из другого места
        pass
    finally:
        conn.alive = False
        await service.on_disconnect(conn)
