"""Тесты чисто-питоновского бэкенда (без ROS)."""

from __future__ import annotations

import math

import time

import pytest

from so101_gateway.backends.base import RobotBackend
from so101_gateway.backends.sim import HOME_POSE, SimBackend
from so101_gateway.safety.limits import JOINT_ORDER, JointLimits
from so101_gateway.session.manager import ErrorCode


@pytest.fixture
def limits() -> JointLimits:
    return JointLimits.fallback()


@pytest.fixture
def backend(limits, config, clock) -> SimBackend:
    return SimBackend(limits=limits, config=config, clock=clock)


def settle(backend, clock, seconds: float = 3.0, rate_hz: float = 50.0):
    for _ in range(int(seconds * rate_hz)):
        clock.advance(1.0 / rate_hz)
        backend.get_state()


# --------------------------------------------------------------------------- #
# протокол
# --------------------------------------------------------------------------- #

def test_sim_backend_satisfies_protocol(backend):
    assert isinstance(backend, RobotBackend)


def test_is_connected(backend):
    assert backend.is_connected() is True


def test_state_shape(backend):
    state = backend.get_state()
    assert set(state["joints"]) == set(JOINT_ORDER)
    assert set(state["ee"]) == {"x", "y", "z"}
    assert all(isinstance(v, float) for v in state["joints"].values())
    assert all(isinstance(v, float) for v in state["ee"].values())
    assert state["connected"] is True


def test_starts_at_home(backend):
    state = backend.get_state()
    for name, value in HOME_POSE.items():
        assert state["joints"][name] == pytest.approx(value, abs=1e-9)


# --------------------------------------------------------------------------- #
# интегратор
# --------------------------------------------------------------------------- #

def test_position_converges_to_command(backend, clock):
    backend.send_joint_command({"shoulder_pan": 1.0})
    settle(backend, clock)
    assert backend.get_state()["joints"]["shoulder_pan"] == pytest.approx(1.0, abs=1e-3)


def test_position_lags_behind_command(backend, clock):
    """Первый порядок: позиция едет к команде, а не прыгает в неё."""
    backend.send_joint_command({"shoulder_pan": 1.0})
    clock.advance(0.005)
    value = backend.get_state()["joints"]["shoulder_pan"]
    assert 0.0 < value < 1.0


def test_position_approaches_monotonically(backend, clock):
    backend.send_joint_command({"elbow_flex": -1.0})
    previous = backend.get_state()["joints"]["elbow_flex"]
    for _ in range(50):
        clock.advance(0.02)
        current = backend.get_state()["joints"]["elbow_flex"]
        assert current <= previous + 1e-12
        previous = current
    assert previous == pytest.approx(-1.0, abs=1e-3)


def test_command_is_clamped_to_joint_limits(backend, clock, limits):
    _, hi = limits.bounds("shoulder_pan")
    backend.send_joint_command({"shoulder_pan": hi + 10.0})
    settle(backend, clock)
    assert backend.get_state()["joints"]["shoulder_pan"] == pytest.approx(hi, abs=1e-3)


def test_go_home_returns_arm_to_home_pose(backend, clock):
    backend.send_joint_command({"shoulder_pan": 1.5, "elbow_flex": -1.5})
    settle(backend, clock)
    assert backend.get_state()["joints"]["shoulder_pan"] == pytest.approx(1.5, abs=1e-3)

    backend.go_home()
    settle(backend, clock)
    for name, value in HOME_POSE.items():
        assert backend.get_state()["joints"][name] == pytest.approx(value, abs=1e-3)


# --------------------------------------------------------------------------- #
# кинематика
# --------------------------------------------------------------------------- #

def test_ee_changes_when_joints_move(backend, clock):
    before = backend.get_state()["ee"]
    backend.send_joint_command({"shoulder_pan": 0.8})
    settle(backend, clock)
    after = backend.get_state()["ee"]
    assert after != before
    assert abs(after["y"] - before["y"]) > 0.01


def test_ee_keeps_its_height_when_only_pan_rotates(backend, clock):
    """``shoulder_pan`` — вертикальный сустав: высота схвата не меняется.

    Раньше тест требовал сохранения расстояния до НАЧАЛА ``base_link``. По URDF
    ось поворота проходит через origin первого сустава (x≈0.039), а не через
    начало координат, так что окружность вокруг нуля была свойством прежней
    самодельной модели. Сохраняется расстояние до оси — его и проверяем.
    """
    pivot_x = 0.0388353            # origin shoulder_pan по URDF
    before = backend.get_state()["ee"]
    radius_before = math.hypot(before["x"] - pivot_x, before["y"])
    backend.send_joint_command({"shoulder_pan": 0.5})
    settle(backend, clock)
    after = backend.get_state()["ee"]

    assert math.hypot(after["x"] - pivot_x, after["y"]) == pytest.approx(
        radius_before, abs=1e-3
    )
    assert after["z"] == pytest.approx(before["z"], abs=1e-3)
    assert math.hypot(after["x"], after["y"]) != pytest.approx(
        math.hypot(before["x"], before["y"]), abs=1e-3
    ), "ось поворота смещена от начала base_link — расстояние до нуля не сохраняется"


def test_home_pose_is_inside_the_workspace(backend, config):
    ee = backend.get_state()["ee"]
    assert config.workspace_x[0] <= ee["x"] <= config.workspace_x[1]
    assert config.workspace_y[0] <= ee["y"] <= config.workspace_y[1]
    assert config.workspace_z[0] <= ee["z"] <= config.workspace_z[1]


def test_workspace_contains_the_home_pose_with_margin(backend, config):
    """Умолчания зоны обязаны обслуживать ОБА бэкенда.

    Прежняя коробка (x 0.05…0.35) не содержала даже домашней позы настоящей
    руки: стоило переключиться с симулятора на стенд, и схват оказывался за
    границей объявленной зоны ещё до первой команды.

    Проверяем именно домашнюю позу, а не нулевую: в нулях рука вытянута в
    струну (x=0.391 при вылете 0.47), якобиан там вырожден, и держать такую
    позу внутри рабочей зоны незачем — см. тест ниже.
    """
    x, y, z = backend.forward_kinematics(HOME_POSE)
    assert config.workspace_x[0] + 0.02 <= x <= config.workspace_x[1] - 0.02
    assert config.workspace_y[0] + 0.02 <= y <= config.workspace_y[1] - 0.02
    assert config.workspace_z[0] + 0.02 <= z <= config.workspace_z[1] - 0.02


def test_fully_extended_zero_pose_is_deliberately_outside(backend, config):
    """Нулевая поза намеренно вне зоны, и это не упущение.

    x=0.391 при максимальном вылете 0.469 — рука почти в струну. Если такая
    точка вдруг окажется внутри объявленной зоны, значит зону расширили до
    окрестности вырождения, где IK перестаёт сходиться, а рука дёргается.
    """
    x, _, _ = backend.forward_kinematics({name: 0.0 for name in JOINT_ORDER})
    assert x > config.workspace_x[1], (
        "зона доросла до вытянутой в струну позы — проверьте сходимость IK у границы"
    )


def test_workspace_is_a_safe_subset_of_the_full_reach(backend, config):
    """И наоборот: зона — безопасная коробка для демо, а не полный вылет.

    Замеренный вылет схвата: x −0.33…+0.47, y ±0.40, z −0.20…+0.49. Зона обязана
    лежать внутри него с запасом, не заходить за спину (x>0) и не спускаться в
    стол (z>0). Углы коробки при этом заведомо недостижимы — вписать
    прямоугольник в сферу нельзя, и ровно для этого в контракте есть
    ``ik_failed``; проверяем не углы, а центр.
    """
    assert 0.0 < config.workspace_x[0] < config.workspace_x[1] <= 0.47
    assert -0.40 <= config.workspace_y[0] < 0.0 < config.workspace_y[1] <= 0.40
    assert 0.0 < config.workspace_z[0] < config.workspace_z[1] <= 0.49

    center = tuple(
        (lo + hi) / 2.0
        for lo, hi in (config.workspace_x, config.workspace_y, config.workspace_z)
    )
    solved = backend.inverse_kinematics(center)
    assert solved is not None, f"центр рабочей зоны {center} недостижим"


def test_fk_in_the_zero_pose_matches_the_real_arm(backend):
    """Контрольная точка модели: замер TF на живой системе.

    Обе модели — симулятор и ROS-бэкенд, читающий TF, — обязаны в нулевой позе
    давать x=+0.391, y=0.000, z=+0.226. Прежняя самодельная геометрия давала
    x=+0.039 / z=+0.377 (рука «смотрела вверх» вместо «вытянута вперёд»);
    этот тест стоит здесь, чтобы к такому расхождению не вернуться.
    """
    zeros = {name: 0.0 for name in JOINT_ORDER}
    assert backend.forward_kinematics(zeros) == pytest.approx((0.391, 0.0, 0.226), abs=0.005)


# IK численная, сходимость объявлена по норме 1e-6 м, поэтому сверка round-trip
# идёт с запасом на порядок — а не с «машинной» точностью аналитики.
IK_ROUND_TRIP_TOL = 1e-5


def test_ik_is_the_inverse_of_fk(backend):
    point = backend.forward_kinematics(HOME_POSE)
    solved = backend.inverse_kinematics(point)
    assert solved is not None
    assert backend.forward_kinematics(solved) == pytest.approx(point, abs=IK_ROUND_TRIP_TOL)


def test_ik_round_trip_for_several_reachable_points(backend):
    for point in [(0.15, 0.0, 0.25), (0.2, 0.1, 0.15), (0.12, -0.08, 0.3)]:
        solved = backend.inverse_kinematics(point)
        assert solved is not None, point
        assert backend.forward_kinematics(solved) == pytest.approx(
            point, abs=IK_ROUND_TRIP_TOL
        )


def test_ik_fails_for_unreachable_point(backend):
    # угол коробки: вылет руки 0.47 м, до этой точки 0.66 м
    assert backend.inverse_kinematics((0.42, 0.30, 0.45)) is None
    assert backend.inverse_kinematics((2.0, 0.0, 0.2)) is None


# --------------------------------------------------------------------------- #
# jog_ee
# --------------------------------------------------------------------------- #

def test_jog_ee_shifts_the_end_effector_by_delta(backend):
    start = backend.forward_kinematics(HOME_POSE)
    result = backend.jog_ee("z", -0.05, from_joints=HOME_POSE)
    assert result.ok, result.error
    reached = backend.forward_kinematics(result.positions)
    assert reached[2] == pytest.approx(start[2] - 0.05, abs=IK_ROUND_TRIP_TOL)
    assert reached[0] == pytest.approx(start[0], abs=IK_ROUND_TRIP_TOL)
    assert reached[1] == pytest.approx(start[1], abs=IK_ROUND_TRIP_TOL)


def test_jog_ee_works_for_every_axis(backend):
    for axis, index in (("x", 0), ("y", 1), ("z", 2)):
        start = backend.forward_kinematics(HOME_POSE)
        result = backend.jog_ee(axis, 0.01, from_joints=HOME_POSE)
        assert result.ok, (axis, result.error)
        reached = backend.forward_kinematics(result.positions)
        assert reached[index] == pytest.approx(start[index] + 0.01, abs=IK_ROUND_TRIP_TOL)


def test_jog_ee_outside_workspace_is_clamped_and_reported(backend, config):
    result = backend.jog_ee("z", 5.0, from_joints=HOME_POSE)
    assert result.error == ErrorCode.OUT_OF_RANGE
    assert result.axis == "z", "UI должен знать, какую ось подсветить"
    assert result.point[2] == pytest.approx(config.workspace_z[1])


def test_jog_ee_outside_workspace_wins_over_ik_failure(backend, config):
    """Порядок из контракта: границы зоны проверяются раньше IK.

    Точка за зоной, да ещё и недостижимая после подрезки, — это всё равно
    out_of_range, а не ik_failed. Ось для этого теперь −x: по URDF-геометрии
    ближняя граница зоны (x=0.10) лежит в «дыре» у основания, куда рука не
    складывается из-за пределов локтя и кисти. По +x подрезанная точка стала
    достижимой, и там движение честно исполняется (см. тест ниже).
    """
    # Зона намеренно вынесена туда, куда рука не дотягивается совсем: так
    # проверяется именно ПОРЯДОК проверок, а не везение с геометрией. Любая
    # подрезанная точка здесь недостижима, и всё равно обязан прийти
    # out_of_range, а не ik_failed.
    far = config.replace(
        workspace_x=(1.00, 1.20), workspace_y=(-0.20, 0.20), workspace_z=(0.08, 0.36)
    )
    unreachable_zone = SimBackend(limits=backend._limits, config=far, clock=time.monotonic)

    result = unreachable_zone.jog_ee("x", -5.0, from_joints=HOME_POSE)
    assert result.point[0] == pytest.approx(far.workspace_x[0])
    assert not result.ok
    assert result.error == ErrorCode.OUT_OF_RANGE, (
        "границы зоны обязаны проверяться раньше IK"
    )
    assert result.axis == "x"
    assert result.positions is None


def test_jog_ee_outside_workspace_still_moves_when_the_clamped_point_is_reachable(
    backend, config
):
    """Подрезка в зону движение НЕ отменяет: ok=True плюс адресный out_of_range."""
    result = backend.jog_ee("x", 5.0, from_joints=HOME_POSE)
    assert result.ok
    assert result.error == ErrorCode.OUT_OF_RANGE
    assert result.axis == "x"
    assert result.point[0] == pytest.approx(config.workspace_x[1])
    assert backend.forward_kinematics(result.positions) == pytest.approx(
        result.point, abs=IK_ROUND_TRIP_TOL
    )


def test_jog_ee_inside_workspace_but_unreachable_reports_ik_failed(backend, config):
    """ik_failed остаётся для точек ВНУТРИ объявленной рабочей зоны.

    Зона здесь расширена по z нарочно, чтобы подрезки НЕ произошло: цель
    (0.293, 0, 0.606) лежит внутри объявленной коробки и при этом дальше
    физического вылета руки (0.469 м). Именно такой случай контракт и относит
    к ik_failed.
    """
    tall = config.replace(workspace_z=(0.08, 0.80))
    backend = SimBackend(limits=backend._limits, config=tall, clock=time.monotonic)

    result = backend.jog_ee("z", 0.40, from_joints=HOME_POSE)
    assert tall.workspace_x[0] <= result.point[0] <= tall.workspace_x[1]
    assert tall.workspace_z[0] <= result.point[2] <= tall.workspace_z[1], (
        "точка обязана остаться внутри зоны, иначе тест проверяет не то"
    )
    assert not result.ok
    assert result.error == ErrorCode.IK_FAILED
    assert result.positions is None


def test_jog_ee_rejects_unknown_axis(backend):
    result = backend.jog_ee("w", 0.01, from_joints=HOME_POSE)
    assert not result.ok
    assert result.error == ErrorCode.BAD_MESSAGE


def test_jog_ee_rejects_non_numeric_delta(backend):
    result = backend.jog_ee("x", "чуть-чуть", from_joints=HOME_POSE)
    assert not result.ok
    assert result.error == ErrorCode.BAD_MESSAGE


def test_jog_ee_keeps_solution_within_joint_limits(backend, limits):
    result = backend.jog_ee("x", 0.05, from_joints=HOME_POSE)
    assert result.ok
    for name, value in result.positions.items():
        lo, hi = limits.bounds(name)
        assert lo <= value <= hi


def test_jog_ee_defaults_to_measured_pose(backend, clock):
    backend.send_joint_command({"shoulder_pan": 0.3})
    settle(backend, clock)
    state = backend.get_state()
    result = backend.jog_ee("z", -0.02)
    assert result.ok, result.error
    assert result.point[2] == pytest.approx(state["ee"]["z"] - 0.02, abs=1e-6)


# --------------------------------------------------------------------------- #
# пресеты
# --------------------------------------------------------------------------- #

def test_preset_home_is_known(backend):
    assert backend.preset("home") == pytest.approx(HOME_POSE)


def test_preset_wave_is_known_and_within_limits(backend, limits):
    pose = backend.preset("wave")
    assert pose is not None
    assert set(pose) == set(JOINT_ORDER)
    for name, value in pose.items():
        lo, hi = limits.bounds(name)
        assert lo <= value <= hi


def test_unknown_preset_returns_none(backend):
    assert backend.preset("breakdance") is None
