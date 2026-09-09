"""Тесты ограничителя, через который проходит ЛЮБАЯ команда."""

from __future__ import annotations

import pytest

from so101_gateway.safety.limits import JointLimits
from so101_gateway.safety.limiter import (
    CommandLimiter,
    CommandResult,
    RateLimiter,
    WorkspaceBox,
)
from so101_gateway.session.manager import ErrorCode


@pytest.fixture
def limits() -> JointLimits:
    return JointLimits.fallback()


@pytest.fixture
def limiter(limits, config, clock) -> CommandLimiter:
    return CommandLimiter(limits=limits, config=config, clock=clock)


@pytest.fixture
def armed(limiter) -> CommandLimiter:
    """Лимитер с ходом у живого оператора — watchdog взведён."""
    limiter.arm_watchdog()
    return limiter


def run_ticks(limiter, clock, seconds: float, rate_hz: float = 50.0, keep_alive: bool = False):
    """Прокрутить лимитер `seconds` секунд, вернуть трек командной точки.

    ``keep_alive`` имитирует живого клиента: любое сообщение (в том числе
    ``ping``) кормит watchdog.
    """
    dt = 1.0 / rate_hz
    track = [dict(limiter.q_cmd)]
    for _ in range(int(round(seconds * rate_hz))):
        clock.advance(dt)
        if keep_alive:
            limiter.note_activity()
        track.append(dict(limiter.tick()))
    return track


def park_low(limiter, clock, limits, joint="wrist_roll"):
    """Отогнать сустав к нижнему пределу, чтобы ход до верхнего был длинным.

    Полный размах wrist_roll — 5.58 рад, при 1 рад/с это 5.58 с: заведомо
    дольше окна watchdog (3 с), а значит проверка честная.
    """
    lo, hi = limits.bounds(joint)
    limiter.set_pose_rad({joint: lo})
    run_ticks(limiter, clock, 12.0, keep_alive=True)
    limiter.note_activity()
    assert limiter.q_cmd[joint] == pytest.approx(lo, abs=1e-6)
    return lo, hi


# --------------------------------------------------------------------------- #
# клэмп по пределам суставов
# --------------------------------------------------------------------------- #

def test_target_above_limit_is_clamped_and_reported(limiter, limits):
    _, hi = limits.bounds("shoulder_pan")
    result = limiter.set_joints({"shoulder_pan": hi + 10.0})
    assert result.ok, "команда всё равно исполняется, но подрезанной"
    assert result.error == ErrorCode.OUT_OF_RANGE
    assert limiter.target["shoulder_pan"] == pytest.approx(hi)


def test_target_below_limit_is_clamped_and_reported(limiter, limits):
    lo, _ = limits.bounds("elbow_flex")
    result = limiter.set_joints({"elbow_flex": lo - 10.0})
    assert result.ok
    assert result.error == ErrorCode.OUT_OF_RANGE
    assert limiter.target["elbow_flex"] == pytest.approx(lo)


def test_out_of_range_names_the_offending_joint(limiter, limits):
    """UI обязан знать, какой слайдер подсвечивать (docs/api.md §3)."""
    _, hi = limits.bounds("elbow_flex")
    result = limiter.set_joints({"shoulder_pan": 0.1, "elbow_flex": hi + 5.0})
    assert result.error == ErrorCode.OUT_OF_RANGE
    assert result.joint == "elbow_flex"
    assert result.axis is None
    assert result.extra == {"joint": "elbow_flex"}


def test_target_inside_limits_passes_without_error(limiter):
    result = limiter.set_joints({"elbow_flex": 0.5})
    assert result.ok and result.error is None
    assert result.extra == {}
    assert limiter.target["elbow_flex"] == pytest.approx(0.5)


def test_unknown_joint_is_rejected_and_named(limiter):
    result = limiter.set_joints({"nose_wiggle": 0.5})
    assert not result.ok
    assert result.error == ErrorCode.OUT_OF_RANGE
    assert result.joint == "nose_wiggle"
    assert "nose_wiggle" not in limiter.target


def test_non_numeric_target_is_rejected(limiter):
    result = limiter.set_joints({"elbow_flex": "далеко"})
    assert not result.ok
    assert result.error == ErrorCode.BAD_MESSAGE


def test_nan_target_is_rejected(limiter):
    result = limiter.set_joints({"elbow_flex": float("nan")})
    assert not result.ok
    assert result.error == ErrorCode.BAD_MESSAGE


# --------------------------------------------------------------------------- #
# захват: на проводе всюду 0..1
# --------------------------------------------------------------------------- #

def test_gripper_message_maps_unit_value_into_joint_range(limiter, limits):
    lo, hi = limits.bounds("gripper")
    result = limiter.set_gripper_unit(1.0)
    assert result.ok and result.error is None
    assert limiter.target["gripper"] == pytest.approx(hi)

    limiter.set_gripper_unit(0.0)
    assert limiter.target["gripper"] == pytest.approx(lo)

    limiter.set_gripper_unit(0.5)
    assert limiter.target["gripper"] == pytest.approx((lo + hi) / 2)


def test_set_joint_gripper_is_also_a_unit_value(limiter, limits):
    """Контракт: set_joint с joint="gripper" принимает 0..1, а не радианы."""
    lo, hi = limits.bounds("gripper")
    result = limiter.set_joint("gripper", 1.0)
    assert result.ok and result.error is None
    assert limiter.target["gripper"] == pytest.approx(hi)

    limiter.set_joint("gripper", 0.0)
    assert limiter.target["gripper"] == pytest.approx(lo)


def test_set_joints_treats_gripper_as_unit_value(limiter, limits):
    lo, hi = limits.bounds("gripper")
    limiter.set_joints({"gripper": 1.0, "elbow_flex": 0.2})
    assert limiter.target["gripper"] == pytest.approx(hi)
    assert limiter.target["elbow_flex"] == pytest.approx(0.2)


def test_gripper_outside_unit_interval_is_clamped_and_named(limiter, limits):
    _, hi = limits.bounds("gripper")
    result = limiter.set_joint("gripper", 5.0)
    assert result.ok
    assert result.error == ErrorCode.OUT_OF_RANGE
    assert result.joint == "gripper"
    assert limiter.target["gripper"] == pytest.approx(hi)


def test_gripper_message_outside_unit_interval_is_named(limiter):
    result = limiter.set_gripper_unit(-2.0)
    assert result.error == ErrorCode.OUT_OF_RANGE
    assert result.joint == "gripper"


def test_internal_poses_stay_in_radians(limiter, limits):
    """Пресеты и home задаются в радианах — нормировка только на проводе."""
    lo, hi = limits.bounds("gripper")
    limiter.set_pose_rad({"gripper": hi})
    assert limiter.target["gripper"] == pytest.approx(hi)
    limiter.set_pose_rad({"gripper": 0.8})
    assert limiter.target["gripper"] == pytest.approx(0.8)


# --------------------------------------------------------------------------- #
# ограничение скорости — главный тест
# --------------------------------------------------------------------------- #

def test_full_range_jump_never_exceeds_max_velocity(armed, limits, clock, config):
    """Скачок цели на весь диапазон не даёт скорости выше MAX_VEL_RAD_S."""
    limiter = armed
    lo, hi = limits.bounds("shoulder_pan")
    limiter.set_joints({"shoulder_pan": lo})
    run_ticks(limiter, clock, 12.0, keep_alive=True)
    assert limiter.q_cmd["shoulder_pan"] == pytest.approx(lo, abs=1e-6)

    limiter.set_joints({"shoulder_pan": hi})     # прыжок через весь диапазон
    dt = 1.0 / config.cmd_rate_hz
    max_step = config.max_vel_rad_s * dt
    prev = limiter.q_cmd["shoulder_pan"]
    steps = 0
    while limiter.q_cmd["shoulder_pan"] < hi - 1e-9 and steps < 10_000:
        clock.advance(dt)
        limiter.note_activity()          # живой клиент шлёт ping
        limiter.tick()
        cur = limiter.q_cmd["shoulder_pan"]
        assert cur - prev <= max_step + 1e-9, "превышена максимальная скорость сустава"
        prev = cur
        steps += 1

    assert limiter.q_cmd["shoulder_pan"] == pytest.approx(hi, abs=1e-6)
    assert steps == pytest.approx((hi - lo) / max_step, rel=0.05)


def test_velocity_limit_applies_to_every_joint_simultaneously(armed, limits, clock, config):
    limiter = armed
    limiter.set_pose_rad({name: limits.bounds(name)[1] for name in limits.names})
    dt = 1.0 / config.cmd_rate_hz
    max_step = config.max_vel_rad_s * dt

    prev = dict(limiter.q_cmd)
    for _ in range(200):
        clock.advance(dt)
        limiter.note_activity()
        cur = dict(limiter.tick())
        for name in limits.names:
            assert abs(cur[name] - prev[name]) <= max_step + 1e-9
        prev = cur


def test_command_point_reaches_target_and_stops(limiter, clock):
    limiter.set_joints({"wrist_roll": 0.5})
    run_ticks(limiter, clock, 1.0)
    assert limiter.q_cmd["wrist_roll"] == pytest.approx(0.5, abs=1e-6)
    run_ticks(limiter, clock, 1.0)
    assert limiter.q_cmd["wrist_roll"] == pytest.approx(0.5, abs=1e-6)


def test_slow_tick_does_not_allow_a_bigger_leap_than_velocity_allows(armed, limits, clock, config):
    """Если тик задержался, шаг всё равно ограничен MAX_VEL * dt, не больше."""
    limiter = armed
    _, hi = limits.bounds("shoulder_pan")
    limiter.set_joints({"shoulder_pan": hi})
    start = limiter.q_cmd["shoulder_pan"]
    clock.advance(0.5)                     # тик «проспал» полсекунды
    limiter.note_activity()
    limiter.tick()
    assert limiter.q_cmd["shoulder_pan"] - start <= config.max_vel_rad_s * 0.5 + 1e-9


def test_stalled_loop_cannot_teleport_the_command_point(limiter, limits, clock, config):
    """Даже с разоружённым watchdog зависший цикл не даёт гигантский шаг."""
    _, hi = limits.bounds("shoulder_pan")
    limiter.set_joints({"shoulder_pan": hi})
    start = limiter.q_cmd["shoulder_pan"]
    clock.advance(60.0)                    # процесс подвис на минуту
    limiter.tick()
    step = limiter.q_cmd["shoulder_pan"] - start
    assert 0.0 < step <= config.max_vel_rad_s * CommandLimiter.MAX_TICK_DT + 1e-9


# --------------------------------------------------------------------------- #
# watchdog: живость соединения, а не поток команд
# --------------------------------------------------------------------------- #

def test_accepted_target_completes_without_repeating_it(armed, limits, clock, config):
    """Главное новое правило: цель доезжает сама, повторять её не нужно.

    Клиент шлёт ОДИН set_joint, дальше только ping раз в 0.5 с. Ехать дольше,
    чем окно watchdog, — и рука всё равно доезжает до конца.
    """
    limiter = armed
    lo, hi = park_low(limiter, clock, limits)
    assert (hi - lo) > config.max_vel_rad_s * config.watchdog_timeout, "ехать дольше окна"
    limiter.set_joints({"wrist_roll": hi})

    dt = 1.0 / config.cmd_rate_hz
    for step in range(int(10.0 * config.cmd_rate_hz)):
        clock.advance(dt)
        if step % int(0.5 * config.cmd_rate_hz) == 0:
            limiter.note_activity()        # ping раз в полсекунды
        limiter.tick()

    assert limiter.q_cmd["wrist_roll"] == pytest.approx(hi, abs=1e-6)
    assert not limiter.watchdog_tripped


def test_watchdog_trips_when_the_client_goes_silent(armed, limits, clock, config):
    limiter = armed
    lo, hi = park_low(limiter, clock, limits)
    limiter.set_joints({"wrist_roll": hi})

    run_ticks(limiter, clock, 1.0, keep_alive=True)
    moving_at = limiter.q_cmd["wrist_roll"]
    assert moving_at > lo + 0.5
    assert not limiter.watchdog_tripped

    run_ticks(limiter, clock, 6.0)         # клиент замолчал совсем
    frozen_at = limiter.q_cmd["wrist_roll"]
    assert limiter.watchdog_tripped
    assert frozen_at < hi, "движение должно было замереть"

    run_ticks(limiter, clock, 5.0)
    assert limiter.q_cmd["wrist_roll"] == pytest.approx(frozen_at, abs=1e-9)


def test_watchdog_freeze_happens_within_the_timeout(armed, limits, clock, config):
    limiter = armed
    _, hi = park_low(limiter, clock, limits)
    start = limiter.q_cmd["wrist_roll"]
    limiter.set_joints({"wrist_roll": hi})
    run_ticks(limiter, clock, 10.0)
    travelled = limiter.q_cmd["wrist_roll"] - start
    assert travelled <= config.max_vel_rad_s * config.watchdog_timeout + 1e-6


def test_ping_alone_keeps_the_watchdog_fed(armed, limits, clock, config):
    """ping кормит watchdog (но не спасает от idle — это другой таймер)."""
    limiter = armed
    _, hi = park_low(limiter, clock, limits)
    limiter.set_joints({"wrist_roll": hi})
    run_ticks(limiter, clock, 8.0, keep_alive=True)
    assert not limiter.watchdog_tripped
    assert limiter.q_cmd["wrist_roll"] == pytest.approx(hi, abs=1e-6)


def test_activity_releases_a_tripped_watchdog(armed, limits, clock):
    limiter = armed
    _, hi = park_low(limiter, clock, limits)
    limiter.set_joints({"wrist_roll": hi})
    run_ticks(limiter, clock, 6.0)
    assert limiter.watchdog_tripped
    frozen_at = limiter.q_cmd["wrist_roll"]

    limiter.set_joints({"wrist_roll": hi})
    assert not limiter.watchdog_tripped
    run_ticks(limiter, clock, 1.0, keep_alive=True)
    assert limiter.q_cmd["wrist_roll"] > frozen_at


def test_watchdog_is_disarmed_when_nobody_controls(limiter, limits, clock, config):
    """Серверные движения (home, пресет) доезжают: сторожить некого."""
    lo, hi = park_low(limiter, clock, limits)
    assert (hi - lo) > config.max_vel_rad_s * config.watchdog_timeout
    limiter.set_pose_rad({"wrist_roll": hi})
    run_ticks(limiter, clock, 10.0)        # ни одного сообщения от клиента
    assert not limiter.watchdog_tripped
    assert limiter.q_cmd["wrist_roll"] == pytest.approx(hi, abs=1e-6)


def test_disarm_stops_an_already_tripped_watchdog(armed, limits, clock):
    limiter = armed
    _, hi = park_low(limiter, clock, limits)
    limiter.set_joints({"wrist_roll": hi})
    run_ticks(limiter, clock, 6.0)
    assert limiter.watchdog_tripped

    limiter.disarm_watchdog()              # ход отобрали, рука уходит в home
    assert not limiter.watchdog_tripped
    limiter.set_pose_rad({"wrist_roll": hi})
    run_ticks(limiter, clock, 10.0)
    assert limiter.q_cmd["wrist_roll"] == pytest.approx(hi, abs=1e-6)


def test_arming_resets_the_silence_timer(limiter, limits, clock):
    """Новый оператор получает полное окно, а не остаток от предыдущего."""
    clock.advance(100.0)                   # долгая тишина до выдачи хода
    limiter.arm_watchdog()
    assert not limiter.watchdog_tripped


def test_hold_freezes_target_at_command_point(limiter, limits, clock):
    _, hi = limits.bounds("shoulder_pan")
    limiter.set_joints({"shoulder_pan": hi})
    run_ticks(limiter, clock, 0.5)
    limiter.hold()
    at = limiter.q_cmd["shoulder_pan"]
    assert limiter.target["shoulder_pan"] == pytest.approx(at)
    run_ticks(limiter, clock, 2.0)
    assert limiter.q_cmd["shoulder_pan"] == pytest.approx(at)


# --------------------------------------------------------------------------- #
# rate limit
# --------------------------------------------------------------------------- #

def test_rate_limiter_allows_up_to_the_budget(clock):
    rl = RateLimiter(max_per_sec=30, clock=clock)
    assert all(rl.allow() for _ in range(30))
    assert not rl.allow()


def test_rate_limiter_recovers_after_window(clock):
    rl = RateLimiter(max_per_sec=30, clock=clock)
    for _ in range(30):
        rl.allow()
    assert not rl.allow()
    clock.advance(1.01)
    assert rl.allow()


def test_rate_limiter_is_a_sliding_window(clock):
    rl = RateLimiter(max_per_sec=10, clock=clock)
    for _ in range(10):
        clock.advance(0.05)
        assert rl.allow()
    assert not rl.allow()
    clock.advance(0.55)
    assert rl.allow()


def test_rate_limiter_is_per_session(clock):
    a = RateLimiter(max_per_sec=5, clock=clock)
    b = RateLimiter(max_per_sec=5, clock=clock)
    for _ in range(5):
        a.allow()
    assert not a.allow()
    assert b.allow(), "лимит одного клиента не должен задевать другого"


# --------------------------------------------------------------------------- #
# рабочая зона
# --------------------------------------------------------------------------- #

def test_workspace_contains(config):
    box = WorkspaceBox.from_config(config)
    assert box.contains(0.2, 0.0, 0.2)
    assert not box.contains(0.5, 0.0, 0.2)
    assert not box.contains(0.2, 0.0, 0.5)
    assert not box.contains(0.2, -0.4, 0.2)


def test_workspace_clamp_reports_violation(config):
    box = WorkspaceBox.from_config(config)
    point, clamped = box.clamp(0.9, -0.9, 0.9)
    assert clamped
    assert point == pytest.approx((config.workspace_x[1], config.workspace_y[0], config.workspace_z[1]))

    point, clamped = box.clamp(0.2, 0.0, 0.2)
    assert not clamped
    assert point == pytest.approx((0.2, 0.0, 0.2))


def test_workspace_payload_matches_contract(config):
    box = WorkspaceBox.from_config(config)
    assert box.as_payload() == {
        "x": [config.workspace_x[0], config.workspace_x[1]],
        "y": [config.workspace_y[0], config.workspace_y[1]],
        "z": [config.workspace_z[0], config.workspace_z[1]],
    }


# --------------------------------------------------------------------------- #
# CommandResult
# --------------------------------------------------------------------------- #

def test_command_result_extra_is_empty_without_address():
    assert CommandResult(ok=True).extra == {}
    assert CommandResult(ok=False, error="x").extra == {}


def test_command_result_extra_prefers_joint_then_axis():
    assert CommandResult(ok=False, error="x", joint="elbow_flex").extra == {"joint": "elbow_flex"}
    assert CommandResult(ok=False, error="x", axis="z").extra == {"axis": "z"}
