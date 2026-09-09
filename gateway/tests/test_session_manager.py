"""Тесты ядра очереди.

Всё время — через инъецированные часы. Ни одного time.sleep: логика переходов
проверяется мгновенно и детерминированно.
"""

from __future__ import annotations

import pytest

from so101_gateway.session.manager import (
    ErrorCode,
    GoHome,
    Granted,
    QueueChanged,
    Revoked,
    SessionManager,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def make(config, clock, **overrides) -> SessionManager:
    return SessionManager(config=config.replace(**overrides), clock=clock)


def advance(mgr: SessionManager, clock, dt: float):
    """Сдвинуть часы и прокрутить один тик."""
    clock.advance(dt)
    return mgr.tick()


def join_and_enqueue(mgr: SessionManager, token: str):
    mgr.hello(token)
    return mgr.enqueue(token)


def of_type(events, cls):
    return [e for e in events if isinstance(e, cls)]


# --------------------------------------------------------------------------- #
# базовая раздача хода
# --------------------------------------------------------------------------- #

def test_first_client_in_empty_queue_gets_control_immediately(config, clock):
    mgr = make(config, clock)
    ok, err, events = join_and_enqueue(mgr, "a")

    assert ok and err is None
    assert mgr.controller == "a"
    granted = of_type(events, Granted)
    assert len(granted) == 1
    assert granted[0].token == "a"
    # Посетитель на стенде один — торопить его некого, срока нет.
    assert granted[0].duration_sec is None
    assert granted[0].expires_at is None
    assert mgr.control_expires_at is None


def test_observer_does_not_control_until_it_enqueues(config, clock):
    mgr = make(config, clock)
    mgr.hello("a")
    assert mgr.controller is None
    assert not mgr.is_controller("a")
    view = mgr.queue_view("a")
    assert view["position"] == -1
    assert view["you_control"] is False


def test_twenty_clients_exactly_one_controls(config, clock):
    mgr = make(config, clock)
    for i in range(20):
        ok, err, _ = join_and_enqueue(mgr, f"c{i}")
        assert ok, err

    controllers = [t for t in mgr.tokens if mgr.is_controller(t)]
    assert controllers == ["c0"]
    assert mgr.queue_length == 19  # 20 участников: один управляет, 19 ждут


def test_twenty_first_client_gets_queue_full(config, clock):
    mgr = make(config, clock)
    for i in range(20):
        ok, _, _ = join_and_enqueue(mgr, f"c{i}")
        assert ok
    ok, err, events = join_and_enqueue(mgr, "late")
    assert not ok
    assert err == ErrorCode.QUEUE_FULL
    assert events == []
    assert mgr.queue_length == 19


def test_enqueue_is_idempotent(config, clock):
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    join_and_enqueue(mgr, "b")
    before = mgr.queue_view("b")
    ok, err, _ = mgr.enqueue("b")
    assert ok and err is None
    assert mgr.queue_view("b") == before


def test_enqueue_by_unknown_token_is_rejected(config, clock):
    mgr = make(config, clock)
    ok, err, _ = mgr.enqueue("ghost")
    assert not ok
    assert err == ErrorCode.BAD_MESSAGE


# --------------------------------------------------------------------------- #
# длительность хода
# --------------------------------------------------------------------------- #

def test_turn_expires_after_control_duration(config, clock):
    """Отсчёт идёт, пока в очереди кто-то ждёт."""
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    join_and_enqueue(mgr, "b")     # без ожидающего срок бы не запустился
    started = clock()

    # активный оператор: шлём команды, чтобы не сработал idle-таймаут
    while clock() + 5.0 < started + config.control_duration:
        clock.advance(5.0)
        mgr.note_activity("a", is_command=True)
        assert mgr.tick() == []
        assert mgr.controller == "a"

    assert mgr.controller == "a"
    events = advance(mgr, clock, (started + config.control_duration) - clock() + 0.1)
    revoked = of_type(events, Revoked)
    assert [(r.token, r.reason) for r in revoked] == [("a", "timeout")]
    assert mgr.controller is None


def test_turn_cannot_be_extended_by_commands(config, clock):
    """Непрерывный поток команд не продлевает ход ни на секунду."""
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    join_and_enqueue(mgr, "b")     # отсчёт запускается наличием ожидающего
    started = clock()

    for _ in range(1000):
        clock.advance(1.0)
        mgr.note_activity("a", is_command=True)
        mgr.tick()
        if mgr.controller is None:
            break

    assert mgr.controller is None
    elapsed = clock() - started
    assert elapsed == pytest.approx(config.control_duration, abs=1.0)


def test_second_client_gets_turn_after_handover(config, clock):
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    join_and_enqueue(mgr, "b")

    events = advance(mgr, clock, config.control_duration)
    assert of_type(events, Revoked)
    assert of_type(events, GoHome), "передача хода начинается с ухода в home"
    assert mgr.controller is None
    assert mgr.robot_mode == "moving"

    # пока идёт HANDOVER_HOME — хода нет ни у кого
    advance(mgr, clock, config.handover_home - 0.1)
    assert mgr.controller is None
    assert mgr.robot_mode == "moving"

    events = advance(mgr, clock, 0.2)
    granted = of_type(events, Granted)
    assert [g.token for g in granted] == ["b"]
    assert mgr.controller == "b"
    assert mgr.robot_mode == "idle"


def test_commands_are_not_accepted_during_handover(config, clock):
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    join_and_enqueue(mgr, "b")
    advance(mgr, clock, config.control_duration)

    allowed, err = mgr.can_command("b")
    assert not allowed
    assert err == ErrorCode.NOT_CONTROLLER
    allowed, err = mgr.can_command("a")
    assert not allowed


def test_release_gives_turn_away_early(config, clock):
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    join_and_enqueue(mgr, "b")

    events = mgr.release("a")
    assert [(r.token, r.reason) for r in of_type(events, Revoked)] == [("a", "release")]
    assert mgr.controller is None

    advance(mgr, clock, config.handover_home)
    assert mgr.controller == "b"


def test_leave_removes_queued_client_from_queue(config, clock):
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    join_and_enqueue(mgr, "b")
    events = mgr.leave("b")
    assert of_type(events, QueueChanged)
    assert mgr.queue_length == 0
    assert mgr.queue_view("b")["position"] == -1


def test_release_by_queued_client_is_a_noop(config, clock):
    """release — только для оператора; очередник обязан слать leave."""
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    join_and_enqueue(mgr, "b")
    assert mgr.release("b") == []
    assert mgr.is_queued("b")
    assert mgr.controller == "a"


def test_release_by_plain_observer_is_a_noop(config, clock):
    """Наблюдателю отдавать нечего — менеджер молчит, отказ формирует WS-слой."""
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    mgr.hello("watcher")
    assert mgr.release("watcher") == []
    assert mgr.controller == "a"
    assert not mgr.is_queued("watcher")


def test_leave_by_controller_is_a_noop(config, clock):
    """leave — только для очереди; оператор обязан слать release."""
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    assert mgr.leave("a") == []
    assert mgr.controller == "a"


def test_leave_by_plain_observer_is_a_noop(config, clock):
    mgr = make(config, clock)
    mgr.hello("watcher")
    assert mgr.leave("watcher") == []


def test_leave_shifts_positions_of_those_behind(config, clock):
    mgr = make(config, clock)
    for name in ("a", "b", "c", "d"):
        join_and_enqueue(mgr, name)
    assert mgr.queue_view("d")["position"] == 3
    mgr.leave("b")
    assert mgr.queue_view("c")["position"] == 1
    assert mgr.queue_view("d")["position"] == 2
    assert mgr.queue_length == 2


# --------------------------------------------------------------------------- #
# бездействие
# --------------------------------------------------------------------------- #

def test_idle_timeout_revokes_turn(config, clock):
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    advance(mgr, clock, config.idle_timeout - 0.1)
    assert mgr.controller == "a"

    events = advance(mgr, clock, 0.2)
    assert [(r.token, r.reason) for r in of_type(events, Revoked)] == [("a", "idle")]
    assert mgr.controller is None


def test_command_resets_idle_timer(config, clock):
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    for _ in range(4):
        clock.advance(config.idle_timeout - 1.0)
        mgr.note_activity("a", is_command=True)
        mgr.tick()
        assert mgr.controller == "a"


def test_ping_does_not_reset_idle_timer(config, clock):
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    # пингуем часто, но команд не шлём
    for _ in range(10):
        clock.advance(config.idle_timeout / 10.0 + 0.01)
        mgr.note_activity("a", is_command=False)
        events = mgr.tick()
        if of_type(events, Revoked):
            break
    assert mgr.controller is None, "ping не должен спасать от idle-таймаута"
    assert clock() <= 1_000_000.0 + config.idle_timeout + 1.0


def test_idle_timer_starts_fresh_for_new_controller(config, clock):
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    join_and_enqueue(mgr, "b")
    advance(mgr, clock, config.idle_timeout)          # a теряет ход
    advance(mgr, clock, config.handover_home)         # b получает ход
    assert mgr.controller == "b"
    advance(mgr, clock, config.idle_timeout - 0.1)
    assert mgr.controller == "b"


# --------------------------------------------------------------------------- #
# обрыв связи
# --------------------------------------------------------------------------- #

def test_controller_keeps_turn_while_within_grace(config, clock):
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    expires_at = mgr.control_expires_at

    mgr.disconnect("a")
    assert mgr.controller == "a", "ход не отбирается сразу"

    advance(mgr, clock, config.reconnect_grace - 0.5)
    assert mgr.controller == "a"

    events = mgr.hello("a")
    assert mgr.controller == "a"
    granted = of_type(events, Granted)
    assert len(granted) == 1
    assert granted[0].expires_at == pytest.approx(expires_at), "возврат не продлевает ход"


def test_controller_loses_turn_after_grace(config, clock):
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    mgr.disconnect("a")

    events = advance(mgr, clock, config.reconnect_grace + 0.1)
    assert [(r.token, r.reason) for r in of_type(events, Revoked)] == [("a", "disconnect")]
    assert mgr.controller is None

    mgr.hello("a")
    assert mgr.controller is None, "после grace ход не возвращается"


def test_queued_client_is_dropped_immediately_on_disconnect(config, clock):
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    join_and_enqueue(mgr, "b")
    join_and_enqueue(mgr, "c")
    assert mgr.queue_length == 2

    mgr.disconnect("b")
    assert mgr.queue_length == 1
    assert mgr.queue_view("c")["position"] == 1


def test_observer_is_dropped_immediately_on_disconnect(config, clock):
    mgr = make(config, clock)
    mgr.hello("w")
    assert "w" in mgr.tokens
    mgr.disconnect("w")
    assert "w" not in mgr.tokens


def test_turn_passes_to_next_after_grace_expiry(config, clock):
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    join_and_enqueue(mgr, "b")
    mgr.disconnect("a")
    advance(mgr, clock, config.reconnect_grace + 0.1)
    advance(mgr, clock, config.handover_home)
    assert mgr.controller == "b"


# --------------------------------------------------------------------------- #
# cooldown
# --------------------------------------------------------------------------- #

def test_cooldown_blocks_immediate_re_enqueue(config, clock):
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    mgr.release("a")

    ok, err, _ = mgr.enqueue("a")
    assert not ok
    assert err == ErrorCode.COOLDOWN


def test_cooldown_expires(config, clock):
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    mgr.release("a")
    advance(mgr, clock, config.cooldown + 0.1)

    ok, err, _ = mgr.enqueue("a")
    assert ok, err


def test_cooldown_remaining_is_reported(config, clock):
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    mgr.release("a")
    advance(mgr, clock, 10.0)
    assert mgr.cooldown_remaining("a") == pytest.approx(config.cooldown - 10.0, abs=1e-6)


def test_estop_victim_gets_no_cooldown(config, clock):
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    mgr.estop()
    mgr.release_estop()
    ok, err, _ = mgr.enqueue("a")
    assert ok, f"после estop ход отобрали не по вине оператора: {err}"


def test_admin_kick_applies_cooldown(config, clock):
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    mgr.kick()
    ok, err, _ = mgr.enqueue("a")
    assert not ok
    assert err == ErrorCode.COOLDOWN


# --------------------------------------------------------------------------- #
# estop
# --------------------------------------------------------------------------- #

def test_estop_revokes_turn_immediately(config, clock):
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    events = mgr.estop()
    assert [(r.token, r.reason) for r in of_type(events, Revoked)] == [("a", "estop")]
    assert mgr.controller is None
    assert mgr.robot_mode == "estop"


def test_estop_freezes_queue(config, clock):
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    join_and_enqueue(mgr, "b")
    join_and_enqueue(mgr, "c")
    mgr.estop()

    for _ in range(20):
        advance(mgr, clock, 10.0)
    assert mgr.controller is None, "во время estop ход не выдаётся"
    assert mgr.queue_length == 2
    assert mgr.queue_view("b")["position"] == 1
    assert mgr.queue_view("c")["position"] == 2


def test_estop_is_only_lifted_explicitly(config, clock):
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    join_and_enqueue(mgr, "b")
    mgr.estop()
    advance(mgr, clock, 1000.0)
    assert mgr.robot_mode == "estop"

    events = mgr.release_estop()
    assert of_type(events, GoHome)
    assert mgr.robot_mode == "moving"
    advance(mgr, clock, config.handover_home)
    assert mgr.controller == "b"
    assert mgr.robot_mode == "idle"


def test_commands_are_rejected_during_estop(config, clock):
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    mgr.estop()
    allowed, err = mgr.can_command("a")
    assert not allowed
    assert err == ErrorCode.ESTOP


def test_estop_is_idempotent(config, clock):
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    mgr.estop()
    assert mgr.estop() == []
    assert mgr.robot_mode == "estop"


# --------------------------------------------------------------------------- #
# admin kick
# --------------------------------------------------------------------------- #

def test_admin_kick_passes_turn_to_next(config, clock):
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    join_and_enqueue(mgr, "b")
    events = mgr.kick()
    assert [(r.token, r.reason) for r in of_type(events, Revoked)] == [("a", "admin")]
    advance(mgr, clock, config.handover_home)
    assert mgr.controller == "b"


def test_admin_kick_without_controller_is_noop(config, clock):
    mgr = make(config, clock)
    assert mgr.kick() == []


# --------------------------------------------------------------------------- #
# queue view / eta
# --------------------------------------------------------------------------- #

def test_queue_view_for_controller(config, clock):
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    view = mgr.queue_view("a")
    assert view == {
        "position": 0,
        "ahead": 0,
        "eta_sec": 0,
        "queue_length": 0,
        "you_control": True,
    }


def test_eta_uses_control_duration_plus_handover(config, clock):
    mgr = make(config, clock)
    for name in ("a", "b", "c", "d"):
        join_and_enqueue(mgr, name)

    slot = config.control_duration + config.handover_home
    assert mgr.queue_view("b") == {
        "position": 1,
        "ahead": 1,
        "eta_sec": int(1 * slot),
        "queue_length": 3,
        "you_control": False,
    }
    assert mgr.queue_view("c")["eta_sec"] == int(2 * slot)
    assert mgr.queue_view("d")["eta_sec"] == int(3 * slot)


def test_eta_drops_when_nobody_controls(config, clock):
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    join_and_enqueue(mgr, "b")
    mgr.release("a")  # хода нет ни у кого, идёт handover
    view = mgr.queue_view("b")
    assert view["position"] == 1
    assert view["ahead"] == 0
    assert view["eta_sec"] == 0


def test_queue_positions_shift_after_turn_change(config, clock):
    mgr = make(config, clock)
    for name in ("a", "b", "c"):
        join_and_enqueue(mgr, name)
    assert mgr.queue_view("c")["position"] == 2

    mgr.release("a")
    advance(mgr, clock, config.handover_home)
    assert mgr.controller == "b"
    assert mgr.queue_view("c")["position"] == 1


def test_public_status_payload(config, clock):
    mgr = make(config, clock)
    for name in ("a", "b", "c", "d"):
        join_and_enqueue(mgr, name)
    status = mgr.status()
    assert status["robot"] == "idle"
    assert status["occupied"] is True
    assert status["queue_length"] == 3
    assert status["estimated_wait_sec"] == int(4 * (config.control_duration + config.handover_home))


# --------------------------------------------------------------------------- #
# смешанные сценарии
# --------------------------------------------------------------------------- #

def test_full_rotation_of_three_clients(config, clock):
    mgr = make(config, clock)
    for name in ("a", "b", "c"):
        join_and_enqueue(mgr, name)

    order = []
    for _ in range(3):
        order.append(mgr.controller)
        advance(mgr, clock, config.control_duration)
        advance(mgr, clock, config.handover_home)
    assert order == ["a", "b", "c"]


def test_queue_changed_event_emitted_on_every_membership_change(config, clock):
    mgr = make(config, clock)
    _, _, ev1 = join_and_enqueue(mgr, "a")
    assert any(isinstance(e, QueueChanged) for e in ev1)
    _, _, ev2 = join_and_enqueue(mgr, "b")
    assert any(isinstance(e, QueueChanged) for e in ev2)
    ev3 = mgr.disconnect("b")
    assert any(isinstance(e, QueueChanged) for e in ev3)


def test_admin_snapshot_exposes_tokens_and_timers(config, clock):
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    join_and_enqueue(mgr, "b")
    snap = mgr.admin_snapshot()
    assert snap["controller"]["token"] == "a"
    assert snap["controller"]["expires_at"] == pytest.approx(mgr.control_expires_at)
    assert [c["token"] for c in snap["queue"]] == ["b"]
    assert snap["estop"] is False
    assert "now" in snap


# --------------------------------------------------------------------------- #
# отсчёт хода начинается только когда есть кому ждать
# --------------------------------------------------------------------------- #

def test_lone_visitor_keeps_control_indefinitely(config, clock):
    """Один на стенде — играет сколько хочет.

    Ограничение хода существует ради очереди. Пока очереди нет, выгонять
    человека через 90 секунд незачем: он уйдёт с ощущением, что ему не дали
    попробовать, а робот будет стоять пустой.
    """
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")

    assert mgr.control_expires_at is None
    # Держим ход впятеро дольше, чем длится обычный ход.
    for _ in range(5):
        clock.advance(config.control_duration)
        mgr.note_activity("a", is_command=True)
        assert of_type(mgr.tick(), Revoked) == []
        assert mgr.controller == "a"


def test_countdown_starts_when_someone_joins_the_queue(config, clock):
    """Отсчёт запускается в момент появления ожидающего, а не раньше."""
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    clock.advance(600.0)                      # десять минут в одиночестве
    mgr.note_activity("a", is_command=True)
    assert mgr.control_expires_at is None

    joined_at = clock()
    _, _, events = join_and_enqueue(mgr, "b")

    # Полный ход С ЭТОЙ секунды, а не остаток от простоя в одиночестве.
    assert mgr.control_expires_at == pytest.approx(joined_at + config.control_duration)
    granted = of_type(events, Granted)
    assert [g.token for g in granted] == ["a"]
    assert granted[0].expires_at == pytest.approx(joined_at + config.control_duration)
    assert granted[0].duration_sec == pytest.approx(config.control_duration)


def test_countdown_is_not_restarted_by_further_joiners(config, clock):
    """Третий подошедший не сдвигает уже идущий отсчёт."""
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    join_and_enqueue(mgr, "b")
    deadline = mgr.control_expires_at
    assert deadline is not None

    clock.advance(config.control_duration / 3)
    _, _, events = join_and_enqueue(mgr, "c")

    assert mgr.control_expires_at == pytest.approx(deadline)
    assert of_type(events, Granted) == []      # оператору сообщать нечего


def test_turn_without_waiters_ends_only_by_idle_or_release(config, clock):
    """Без очереди ход всё равно отбирается за бездействие.

    Иначе ушедший от стенда посетитель держал бы робота вечно: вкладка жива,
    ping идёт, а человека нет.
    """
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    assert mgr.control_expires_at is None

    events = advance(mgr, clock, config.idle_timeout + 0.1)
    assert [(r.token, r.reason) for r in of_type(events, Revoked)] == [("a", "idle")]


def test_next_operator_gets_deadline_when_queue_still_has_people(config, clock):
    """Ход с очередью — со сроком; последний в очереди — без срока."""
    mgr = make(config, clock)
    join_and_enqueue(mgr, "a")
    join_and_enqueue(mgr, "b")
    join_and_enqueue(mgr, "c")

    events = mgr.release("a")
    events.extend(advance(mgr, clock, config.handover_home + 0.1))
    granted = of_type(events, Granted)
    assert granted and granted[-1].token == "b"
    assert granted[-1].expires_at is not None      # за b стоит ещё c

    events = mgr.release("b")
    events.extend(advance(mgr, clock, config.handover_home + 0.1))
    granted = of_type(events, Granted)
    assert granted and granted[-1].token == "c"
    assert granted[-1].expires_at is None          # за c уже никого
