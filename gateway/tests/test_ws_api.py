"""Тесты WebSocket-протокола (docs/api.md §3) против SimBackend."""

from __future__ import annotations

import contextlib
import uuid

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from so101_gateway.app import create_app

ADMIN = "test-admin-token"


@pytest.fixture
def fast_config(config):
    """Короткие таймеры: тесты API идут в реальном времени."""
    return config.replace(
        admin_token=ADMIN,
        control_duration=0.6,
        idle_timeout=0.4,
        handover_home=0.2,
        reconnect_grace=0.3,
        cooldown=0.3,
        state_rate_hz=25.0,
        cmd_rate_hz=50.0,
    )


@pytest.fixture
def client(fast_config):
    with TestClient(create_app(config=fast_config)) as c:
        yield c


@pytest.fixture
def cartesian_client(fast_config):
    """Клиент с ВКЛЮЧЁННЫМ декартовым режимом.

    На стенде он выключен (Config.feature_cartesian=False), но код жив и
    должен оставаться проверенным — тесты джоггинга берут этого клиента.
    """
    cfg = fast_config.replace(feature_cartesian=True, control_duration=30.0, idle_timeout=30.0)
    with TestClient(create_app(config=cfg)) as c:
        yield c


@pytest.fixture
def patient_client(fast_config):
    """Долгий ход: для проверок, где рука должна успеть доехать."""
    cfg = fast_config.replace(control_duration=30.0, idle_timeout=30.0)
    with TestClient(create_app(config=cfg)) as c:
        yield c


def token() -> str:
    return str(uuid.uuid4())


def wait_for(ws, kind: str, limit: int = 400):
    """Прочитать сообщения до первого с типом ``kind``."""
    for _ in range(limit):
        msg = ws.receive_json()
        if msg["t"] == kind:
            return msg
    raise AssertionError(f"сообщение {kind!r} так и не пришло")


def take_control(ws, tok: str):
    ws.send_json({"t": "hello", "token": tok})
    ws.send_json({"t": "enqueue"})
    return wait_for(ws, "granted")


# --------------------------------------------------------------------------- #
# hello / базовая гигиена
# --------------------------------------------------------------------------- #

def test_hello_answers_with_queue(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"t": "hello", "token": token()})
        msg = wait_for(ws, "queue")
        assert set(msg) == {"t", "position", "ahead", "eta_sec", "queue_length", "you_control"}
        assert msg["you_control"] is False


def test_first_message_must_be_hello(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"t": "enqueue"})
        msg = wait_for(ws, "error")
        assert msg["code"] == "bad_message"
        # соединение живо
        ws.send_json({"t": "hello", "token": token()})
        assert wait_for(ws, "queue")


def test_hello_without_token_is_bad_message(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"t": "hello"})
        assert wait_for(ws, "error")["code"] == "bad_message"


def test_broken_json_is_bad_message_and_keeps_connection(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_text("{not json")
        assert wait_for(ws, "error")["code"] == "bad_message"
        ws.send_json({"t": "hello", "token": token()})
        assert wait_for(ws, "queue")


def test_unknown_message_type_is_ignored(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"t": "hello", "token": token()})
        wait_for(ws, "queue")
        ws.send_json({"t": "teleport", "x": 1})
        ws.send_json({"t": "ping"})
        assert wait_for(ws, "pong")          # соединение живо, ошибки нет


def test_ping_returns_pong_with_timestamp(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"t": "hello", "token": token()})
        ws.send_json({"t": "ping"})
        msg = wait_for(ws, "pong")
        assert isinstance(msg["ts"], float)


def test_observers_receive_state_broadcast(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"t": "hello", "token": token()})
        msg = wait_for(ws, "state")
        assert set(msg) == {"t", "joints", "ee", "robot", "ts"}
        assert set(msg["ee"]) == {"x", "y", "z"}
        assert msg["robot"] in {"idle", "moving", "error", "estop"}
        assert isinstance(msg["ts"], float)


# --------------------------------------------------------------------------- #
# очередь
# --------------------------------------------------------------------------- #

def test_enqueue_grants_control_to_first_client(client):
    with client.websocket_connect("/ws") as ws:
        granted = take_control(ws, token())
        # Один на стенде — ход без срока (см. Config.control_duration и
        # SessionManager._start_countdown_if_needed).
        assert granted["duration_sec"] is None
        assert granted["expires_at"] is None
        queue = wait_for(ws, "queue")
        assert queue["position"] == 0
        assert queue["you_control"] is True


def test_second_client_waits_in_queue(client):
    with client.websocket_connect("/ws") as first:
        take_control(first, token())
        with client.websocket_connect("/ws") as second:
            second.send_json({"t": "hello", "token": token()})
            second.send_json({"t": "enqueue"})
            msg = wait_for(second, "queue")
            while msg["position"] == -1:
                msg = wait_for(second, "queue")
            assert msg["position"] == 1
            assert msg["you_control"] is False
            assert msg["eta_sec"] >= 0


def test_queue_full_is_reported(fast_config):
    cfg = fast_config.replace(max_queue=2, control_duration=30.0)
    with TestClient(create_app(config=cfg)) as client:
        with client.websocket_connect("/ws") as a, client.websocket_connect("/ws") as b:
            take_control(a, token())
            b.send_json({"t": "hello", "token": token()})
            b.send_json({"t": "enqueue"})
            wait_for(b, "queue")
            with client.websocket_connect("/ws") as c:
                c.send_json({"t": "hello", "token": token()})
                c.send_json({"t": "enqueue"})
                assert wait_for(c, "error")["code"] == "queue_full"


def test_cooldown_after_own_turn(client):
    with client.websocket_connect("/ws") as ws:
        take_control(ws, token())
        ws.send_json({"t": "release"})
        assert wait_for(ws, "revoked")["reason"] == "release"
        ws.send_json({"t": "enqueue"})
        assert wait_for(ws, "error")["code"] == "cooldown"


def test_release_by_observer_is_not_controller(client):
    """release — только от оператора (docs/api.md §3)."""
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"t": "hello", "token": token()})
        wait_for(ws, "queue")
        ws.send_json({"t": "release"})
        assert wait_for(ws, "error")["code"] == "not_controller"


def test_release_by_queued_client_is_not_controller(client):
    with client.websocket_connect("/ws") as owner, client.websocket_connect("/ws") as waiter:
        take_control(owner, token())
        waiter.send_json({"t": "hello", "token": token()})
        waiter.send_json({"t": "enqueue"})
        wait_for(waiter, "queue")
        waiter.send_json({"t": "release"})
        assert wait_for(waiter, "error")["code"] == "not_controller"


def test_leave_removes_client_from_queue(client):
    with client.websocket_connect("/ws") as owner, client.websocket_connect("/ws") as waiter:
        take_control(owner, token())
        waiter.send_json({"t": "hello", "token": token()})
        waiter.send_json({"t": "enqueue"})
        msg = wait_for(waiter, "queue")
        while msg["position"] == -1:
            msg = wait_for(waiter, "queue")
        assert msg["position"] == 1

        waiter.send_json({"t": "leave"})
        msg = wait_for(waiter, "queue")
        while msg["position"] != -1:
            msg = wait_for(waiter, "queue")
        assert msg["queue_length"] == 0


def test_leave_by_observer_is_not_queued(client):
    """not_queued узаконен ровно за сообщением leave."""
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"t": "hello", "token": token()})
        wait_for(ws, "queue")
        ws.send_json({"t": "leave"})
        assert wait_for(ws, "error")["code"] == "not_queued"


def test_leave_by_controller_is_not_queued(client):
    with client.websocket_connect("/ws") as ws:
        take_control(ws, token())
        ws.send_json({"t": "leave"})
        assert wait_for(ws, "error")["code"] == "not_queued"


def test_turn_passes_to_the_next_client(client):
    with client.websocket_connect("/ws") as a, client.websocket_connect("/ws") as b:
        take_control(a, token())
        b.send_json({"t": "hello", "token": token()})
        b.send_json({"t": "enqueue"})
        wait_for(b, "queue")

        a.send_json({"t": "release"})
        granted = wait_for(b, "granted", limit=800)
        # b получает ход последним в очереди — ждать больше некому, срока нет.
        assert granted["duration_sec"] is None


def test_turn_expires_by_timeout(client):
    with client.websocket_connect("/ws") as ws:
        take_control(ws, token())
        # ход длится 0.6 c; команд не шлём, но idle (0.4) наступит раньше
        assert wait_for(ws, "revoked", limit=800)["reason"] in {"idle", "timeout"}


def test_idle_timeout_revokes_turn(client):
    with client.websocket_connect("/ws") as ws:
        take_control(ws, token())
        msg = wait_for(ws, "revoked", limit=800)
        assert msg["reason"] == "idle"


def test_ping_does_not_prevent_idle_timeout(client):
    """Пингуем непрерывно всё время хода — ход всё равно уходит по idle."""
    import time as _time

    with client.websocket_connect("/ws") as ws:
        take_control(ws, token())
        deadline = _time.time() + 5.0
        while _time.time() < deadline:
            ws.send_json({"t": "ping"})
            _time.sleep(0.05)
            for _ in range(40):
                msg = ws.receive_json()
                if msg["t"] == "revoked":
                    assert msg["reason"] == "idle"
                    return
                if msg["t"] == "pong":
                    break
        raise AssertionError("ping спас от idle-таймаута, а не должен был")


# --------------------------------------------------------------------------- #
# команды
# --------------------------------------------------------------------------- #

def test_command_from_observer_is_rejected_without_closing(client):
    with client.websocket_connect("/ws") as owner, client.websocket_connect("/ws") as watcher:
        take_control(owner, token())
        watcher.send_json({"t": "hello", "token": token()})
        wait_for(watcher, "queue")
        watcher.send_json({"t": "set_joint", "joint": "shoulder_pan", "position": 0.5})
        assert wait_for(watcher, "error")["code"] == "not_controller"
        # соединение живо
        watcher.send_json({"t": "ping"})
        assert wait_for(watcher, "pong")


def test_set_joint_moves_the_arm(client):
    with client.websocket_connect("/ws") as ws:
        take_control(ws, token())
        before = wait_for(ws, "state")["joints"]["shoulder_pan"]
        for _ in range(15):
            ws.send_json({"t": "set_joint", "joint": "shoulder_pan", "position": 1.0})
        after = None
        for _ in range(60):
            after = wait_for(ws, "state")["joints"]["shoulder_pan"]
            if after > before + 0.01:
                break
        assert after > before + 0.01


def test_set_joints_accepts_a_map(client):
    with client.websocket_connect("/ws") as ws:
        take_control(ws, token())
        ws.send_json({"t": "set_joints", "positions": {"shoulder_pan": 0.3, "elbow_flex": -0.3}})
        ws.send_json({"t": "ping"})
        assert wait_for(ws, "pong")           # ошибки не пришло


def test_set_joint_out_of_range_names_the_joint(client):
    """UI обязан знать, какой слайдер подсвечивать (docs/api.md §3)."""
    with client.websocket_connect("/ws") as ws:
        take_control(ws, token())
        ws.send_json({"t": "set_joint", "joint": "shoulder_pan", "position": 99.0})
        msg = wait_for(ws, "error")
        assert msg["code"] == "out_of_range"
        assert msg["joint"] == "shoulder_pan"


def test_set_joints_out_of_range_names_the_offending_joint(client):
    with client.websocket_connect("/ws") as ws:
        take_control(ws, token())
        ws.send_json(
            {"t": "set_joints", "positions": {"shoulder_pan": 0.1, "elbow_flex": 99.0}}
        )
        msg = wait_for(ws, "error")
        assert msg["code"] == "out_of_range"
        assert msg["joint"] == "elbow_flex"


def test_set_joint_with_unknown_joint_is_reported(client):
    with client.websocket_connect("/ws") as ws:
        take_control(ws, token())
        ws.send_json({"t": "set_joint", "joint": "nose", "position": 0.1})
        msg = wait_for(ws, "error")
        assert msg["code"] == "out_of_range"
        assert msg["joint"] == "nose"


def test_gripper_takes_unit_interval(client):
    with client.websocket_connect("/ws") as ws:
        take_control(ws, token())
        ws.send_json({"t": "gripper", "value": 1.0})
        ws.send_json({"t": "ping"})
        assert wait_for(ws, "pong")


def test_state_reports_gripper_in_unit_interval(client):
    """Захват на проводе всюду 0..1 — включая обратный поток state."""
    with client.websocket_connect("/ws") as ws:
        take_control(ws, token())
        for _ in range(30):
            value = wait_for(ws, "state")["joints"]["gripper"]
            assert 0.0 <= value <= 1.0


def test_gripper_travels_the_full_unit_interval(patient_client):
    client = patient_client
    with client.websocket_connect("/ws") as ws:
        take_control(ws, token())
        start = wait_for(ws, "state")["joints"]["gripper"]
        for _ in range(20):
            ws.send_json({"t": "gripper", "value": 1.0})
        value = start
        for _ in range(200):
            value = wait_for(ws, "state")["joints"]["gripper"]
            if value > 0.9:
                break
        assert value > 0.9, "схват должен открыться до 1.0 в долях"


def test_set_joint_gripper_is_a_unit_value(patient_client):
    """set_joint с joint="gripper" принимает доли, а не радианы."""
    client = patient_client
    with client.websocket_connect("/ws") as ws:
        take_control(ws, token())
        for _ in range(20):
            ws.send_json({"t": "set_joint", "joint": "gripper", "position": 1.0})
        value = 0.0
        for _ in range(200):
            value = wait_for(ws, "state")["joints"]["gripper"]
            if value > 0.9:
                break
        assert value > 0.9

        # значение вне 0..1 адресно ругается и клэмпится
        ws.send_json({"t": "set_joint", "joint": "gripper", "position": 5.0})
        msg = wait_for(ws, "error")
        assert msg["code"] == "out_of_range"
        assert msg["joint"] == "gripper"


def test_jog_ee_moves_end_effector(cartesian_client):
    client = cartesian_client
    with client.websocket_connect("/ws") as ws:
        take_control(ws, token())
        before = wait_for(ws, "state")["ee"]
        for _ in range(10):
            ws.send_json({"t": "jog_ee", "axis": "z", "delta": -0.01})
        after = before
        for _ in range(60):
            after = wait_for(ws, "state")["ee"]
            if abs(after["z"] - before["z"]) > 0.005:
                break
        assert abs(after["z"] - before["z"]) > 0.005


def test_jog_ee_with_bad_axis(cartesian_client):
    client = cartesian_client
    with client.websocket_connect("/ws") as ws:
        take_control(ws, token())
        ws.send_json({"t": "jog_ee", "axis": "q", "delta": 0.01})
        assert wait_for(ws, "error")["code"] == "bad_message"


def test_jog_ee_beyond_workspace_reports_out_of_range_with_axis(cartesian_client):
    client = cartesian_client
    """Границы зоны проверяются раньше IK (docs/api.md §3)."""
    with client.websocket_connect("/ws") as ws:
        take_control(ws, token())
        ws.send_json({"t": "jog_ee", "axis": "x", "delta": 5.0})
        msg = wait_for(ws, "error")
        assert msg["code"] == "out_of_range"
        assert msg["axis"] == "x"


def test_jog_ee_inside_workspace_but_unreachable_reports_ik_failed(fast_config):
    """ik_failed приходит именно для точки ВНУТРИ объявленной зоны.

    Потолок зоны поднят до 0.80 нарочно, чтобы подрезки не случилось: цель
    z=0.606 остаётся внутри коробки и при этом дальше физического вылета руки
    (0.469 м). Если бы точка подрезалась границей, контракт требовал бы
    out_of_range, и тест проверял бы не то свойство.
    """
    # feature_cartesian обязателен: без него jog_ee отбивается тем же
    # ik_failed, и тест проходил бы, ничего не проверяя.
    tall = fast_config.replace(workspace_z=(0.08, 0.80), feature_cartesian=True)
    with TestClient(create_app(config=tall)) as client:
        with client.websocket_connect("/ws") as ws:
            take_control(ws, token())
            ws.send_json({"t": "jog_ee", "axis": "z", "delta": 0.40})
            msg = wait_for(ws, "error")
            assert msg["code"] == "ik_failed"


def test_preset_home_is_accepted(client):
    with client.websocket_connect("/ws") as ws:
        take_control(ws, token())
        ws.send_json({"t": "preset", "name": "home"})
        ws.send_json({"t": "ping"})
        assert wait_for(ws, "pong")


def test_unknown_preset_is_bad_message(client):
    with client.websocket_connect("/ws") as ws:
        take_control(ws, token())
        ws.send_json({"t": "preset", "name": "breakdance"})
        assert wait_for(ws, "error")["code"] == "bad_message"


def test_rate_limit_kicks_in(fast_config):
    cfg = fast_config.replace(max_msg_per_sec=10, control_duration=30.0, idle_timeout=30.0)
    with TestClient(create_app(config=cfg)) as client:
        with client.websocket_connect("/ws") as ws:
            take_control(ws, token())
            for _ in range(40):
                ws.send_json({"t": "set_joint", "joint": "shoulder_pan", "position": 0.2})
            assert wait_for(ws, "error")["code"] == "rate_limited"


# --------------------------------------------------------------------------- #
# estop
# --------------------------------------------------------------------------- #

def test_estop_revokes_turn_and_blocks_commands(client):
    with client.websocket_connect("/ws") as ws:
        take_control(ws, token())
        client.post("/api/admin/estop", headers={"X-Admin-Token": ADMIN})
        assert wait_for(ws, "revoked")["reason"] == "estop"

        ws.send_json({"t": "set_joint", "joint": "shoulder_pan", "position": 0.1})
        assert wait_for(ws, "error")["code"] == "estop"

        state = wait_for(ws, "state")
        assert state["robot"] == "estop"


def test_admin_kick_revokes_with_admin_reason(client):
    with client.websocket_connect("/ws") as ws:
        take_control(ws, token())
        client.post("/api/admin/kick", headers={"X-Admin-Token": ADMIN})
        assert wait_for(ws, "revoked")["reason"] == "admin"


# --------------------------------------------------------------------------- #
# переподключение
# --------------------------------------------------------------------------- #

def test_reconnect_within_grace_keeps_control(client):
    tok = token()
    with client.websocket_connect("/ws") as ws:
        granted = take_control(ws, tok)
        expires_at = granted["expires_at"]

    with client.websocket_connect("/ws") as ws2:
        ws2.send_json({"t": "hello", "token": tok})
        again = wait_for(ws2, "granted")
        assert again["expires_at"] == pytest.approx(expires_at, abs=1e-6)


def test_reconnect_after_grace_loses_control(fast_config):
    cfg = fast_config.replace(reconnect_grace=0.15, control_duration=30.0, idle_timeout=30.0)
    tok = token()
    with TestClient(create_app(config=cfg)) as client:
        with client.websocket_connect("/ws") as ws:
            take_control(ws, tok)

        import time as _time
        _time.sleep(0.5)

        with client.websocket_connect("/ws") as ws2:
            ws2.send_json({"t": "hello", "token": tok})
            msg = wait_for(ws2, "queue")
            assert msg["you_control"] is False


# --------------------------------------------------------------------------- #
# watchdog: живость соединения, а не поток команд
# --------------------------------------------------------------------------- #

@pytest.fixture
def watchdog_config(fast_config):
    """Долгий ход, короткое окно watchdog, медленная рука.

    max_vel 0.5 рад/с и цель 0.6 рад — ехать 1.2 с, то есть заведомо дольше
    окна watchdog (0.4 с). Значит видно, доезжает цель сама или замирает.
    """
    return fast_config.replace(
        control_duration=30.0,
        idle_timeout=30.0,
        watchdog_timeout=0.4,
        max_vel_rad_s=0.5,
    )


def drain_joint(ws, joint: str, seconds: float, pinger=None):
    """Читать state `seconds` секунд, попутно (опционально) пингуя."""
    import time as _time

    deadline = _time.time() + seconds
    last = None
    while _time.time() < deadline:
        msg = ws.receive_json()
        if msg["t"] == "state":
            last = msg["joints"][joint]
        if pinger is not None:
            pinger()
    return last


def test_single_target_completes_while_client_only_pings(watchdog_config):
    """Главное новое правило: цель доезжает сама, повторять её не нужно.

    Клиент шлёт ОДИН set_joint и дальше только ping — рука обязана приехать
    именно туда, куда он показал.
    """
    import time as _time

    with TestClient(create_app(config=watchdog_config)) as client:
        with client.websocket_connect("/ws") as ws:
            take_control(ws, token())
            ws.send_json({"t": "set_joint", "joint": "shoulder_pan", "position": 0.6})

            last = None
            deadline = _time.time() + 4.0
            next_ping = _time.time()
            while _time.time() < deadline:
                msg = ws.receive_json()
                if msg["t"] == "state":
                    last = msg["joints"]["shoulder_pan"]
                    if last > 0.58:
                        break
                if _time.time() >= next_ping:
                    ws.send_json({"t": "ping"})
                    next_ping = _time.time() + 0.1

            assert last is not None
            assert last > 0.58, f"цель не доехала, застряла на {last}"


def test_movement_freezes_when_the_client_goes_silent(watchdog_config):
    """Замолчавший клиент (свёрнутая вкладка) останавливает руку."""
    with TestClient(create_app(config=watchdog_config)) as client:
        with client.websocket_connect("/ws") as ws:
            take_control(ws, token())
            ws.send_json({"t": "set_joint", "joint": "shoulder_pan", "position": 1.5})

            # ни одного сообщения от клиента — только слушаем
            frozen = drain_joint(ws, "shoulder_pan", 2.5)
            assert frozen is not None
            assert frozen < 1.0, f"движение должно было замереть, а доехало до {frozen}"
            assert frozen > 0.05, "рука должна была успеть тронуться"

            # ещё немного тишины — позиция не меняется
            again = drain_joint(ws, "shoulder_pan", 1.0)
            assert again == pytest.approx(frozen, abs=0.02)


def test_ping_alone_does_not_resume_after_freeze(watchdog_config):
    """Замирание необратимо: ping доказывает живость вкладки, но не намерение.

    Сработавший watchdog сбрасывает цель в текущую позицию. Возобновить движение
    может только новая команда — иначе телефон, вышедший из блокировки через
    минуту, погнал бы руку к цели, о которой человек уже забыл.
    """
    import time as _time

    with TestClient(create_app(config=watchdog_config)) as client:
        with client.websocket_connect("/ws") as ws:
            take_control(ws, token())
            ws.send_json({"t": "set_joint", "joint": "shoulder_pan", "position": 1.5})
            frozen = drain_joint(ws, "shoulder_pan", 2.0)
            assert frozen < 1.0, "watchdog обязан был остановить движение на полпути"

            # клиент ожил и исправно пингует, но ничего не командует
            deadline = _time.time() + 1.5
            last = frozen
            next_ping = _time.time()
            while _time.time() < deadline:
                msg = ws.receive_json()
                if msg["t"] == "state":
                    last = msg["joints"]["shoulder_pan"]
                if _time.time() >= next_ping:
                    ws.send_json({"t": "ping"})
                    next_ping = _time.time() + 0.1
            assert last == pytest.approx(frozen, abs=0.02), (
                "один только ping не должен возобновлять движение"
            )

            # а вот новая команда — должна
            ws.send_json({"t": "set_joint", "joint": "shoulder_pan", "position": 1.5})
            deadline = _time.time() + 2.0
            moved = last
            next_ping = _time.time()
            while _time.time() < deadline:
                msg = ws.receive_json()
                if msg["t"] == "state":
                    moved = msg["joints"]["shoulder_pan"]
                    if moved > frozen + 0.2:
                        break
                if _time.time() >= next_ping:
                    ws.send_json({"t": "ping"})
                    next_ping = _time.time() + 0.1
            assert moved > frozen + 0.2, "после новой команды движение обязано продолжиться"



def test_handover_home_completes_without_any_client(watchdog_config):
    """Серверное движение (уход в home) доезжает: сторожить некого."""
    cfg = watchdog_config.replace(control_duration=0.5, idle_timeout=0.5)
    with TestClient(create_app(config=cfg)) as client:
        with client.websocket_connect("/ws") as ws:
            take_control(ws, token())
            # уводим руку далеко от home и молчим до потери хода
            ws.send_json({"t": "set_joint", "joint": "shoulder_pan", "position": 1.5})
            assert wait_for(ws, "revoked", limit=800)["reason"] in {"idle", "timeout"}

            # никто не управляет; рука обязана доехать до home (shoulder_pan=0)
            last = drain_joint(ws, "shoulder_pan", 3.0)
            assert abs(last) < 0.05, f"рука не вернулась в home, застряла на {last}"


# --------------------------------------------------------------------------- #
# второе подключение по тому же токену
# --------------------------------------------------------------------------- #

def test_second_connection_evicts_the_first_and_keeps_control(client):
    """Вторая вкладка вытесняет первую, но ход остаётся за токеном."""
    from starlette.websockets import WebSocketDisconnect

    tok = token()
    first = client.websocket_connect("/ws")
    first.__enter__()
    try:
        granted = take_control(first, tok)

        with client.websocket_connect("/ws") as second:
            second.send_json({"t": "hello", "token": tok})
            again = wait_for(second, "granted")
            assert again["expires_at"] == pytest.approx(granted["expires_at"], abs=1e-6)

            # старое соединение закрыто сервером
            with pytest.raises(WebSocketDisconnect):
                for _ in range(200):
                    first.receive_json()

            # ход по-прежнему у токена: команда принимается
            second.send_json({"t": "set_joint", "joint": "shoulder_pan", "position": 0.2})
            second.send_json({"t": "ping"})
            assert wait_for(second, "pong")
    finally:
        with contextlib.suppress(Exception):
            first.__exit__(None, None, None)


def test_second_connection_keeps_queue_position(client):
    from starlette.websockets import WebSocketDisconnect

    tok = token()
    with client.websocket_connect("/ws") as owner:
        take_control(owner, token())

        waiter = client.websocket_connect("/ws")
        waiter.__enter__()
        try:
            waiter.send_json({"t": "hello", "token": tok})
            waiter.send_json({"t": "enqueue"})
            msg = wait_for(waiter, "queue")
            while msg["position"] == -1:
                msg = wait_for(waiter, "queue")
            assert msg["position"] == 1

            with client.websocket_connect("/ws") as second:
                second.send_json({"t": "hello", "token": tok})
                msg = wait_for(second, "queue")
                assert msg["position"] == 1, "место в очереди должно сохраниться"

                with pytest.raises(WebSocketDisconnect):
                    for _ in range(200):
                        waiter.receive_json()
        finally:
            with contextlib.suppress(Exception):
                waiter.__exit__(None, None, None)


def test_reconnect_after_page_reload_restores_the_timer(client):
    """Повторный granted нужен, чтобы клиент восстановил таймер хода."""
    tok = token()
    with client.websocket_connect("/ws") as ws:
        granted = take_control(ws, tok)

    # «перезагрузка страницы»: новое соединение с тем же токеном
    with client.websocket_connect("/ws") as ws2:
        ws2.send_json({"t": "hello", "token": tok})
        again = wait_for(ws2, "granted")
        assert again["expires_at"] == pytest.approx(granted["expires_at"], abs=1e-6)
        assert again["duration_sec"] == granted["duration_sec"]
        queue = wait_for(ws2, "queue")
        assert queue["you_control"] is True


def test_displaced_connection_closes_with_a_private_code(client):
    """Вытеснённое соединение закрывается кодом 4409, а не 1000.

    Это стык двух сторон, и обе поодиночке рассуждали логично. Шлюз выбирал
    1000 («обычное закрытие, не ошибка — не переподключайся»), фронтенд завёл
    частный 4409 («вытеснили, переподключаться нельзя»). Но 1000 неотличим от
    закрытия при уходе со страницы, и клиент с переподключением возвращается:
    вытесняет вкладку, которая вытеснила его, та вытесняет его снова, и две
    вкладки долбят шлюз по кругу.

    Значение обязано совпадать с ``CLOSE_DISPLACED`` в
    ``frontend/src/lib/protocol.ts``. Меняете здесь — меняйте и там.
    """
    from so101_gateway.ws import WS_EVICTED_CODE

    assert WS_EVICTED_CODE == 4409
    assert 4000 <= WS_EVICTED_CODE <= 4999, (
        "код обязан быть из частного диапазона, иначе клиент не отличит "
        "вытеснение от обычного закрытия"
    )

    tok = token()
    with client.websocket_connect("/ws") as first:
        first.send_json({"t": "hello", "token": tok})
        wait_for(first, "queue")

        with client.websocket_connect("/ws") as second:
            second.send_json({"t": "hello", "token": tok})
            wait_for(second, "queue")

            with pytest.raises(WebSocketDisconnect) as excinfo:
                for _ in range(400):
                    first.receive_json()
            assert excinfo.value.code == WS_EVICTED_CODE
