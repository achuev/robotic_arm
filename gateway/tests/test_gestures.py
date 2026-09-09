"""Жесты: сами данные и проигрыватель.

Жест запускает случайный прохожий одной кнопкой, поэтому проверяется не
«красиво ли получилось» (это глазами), а то, что позы физически допустимы
и что жест не может ни застрять, ни перехватить управление у человека.
"""

from __future__ import annotations

import pytest

from so101_gateway.app import GatewayService
from so101_gateway.backends.sim import SimBackend
from so101_gateway.gestures import GESTURES, gesture_names
from so101_gateway.safety.limits import JOINT_ORDER, load_joint_limits
from so101_gateway.session.manager import ErrorCode


@pytest.fixture
def limits():
    return load_joint_limits(None)


@pytest.fixture
def service(config, clock, limits):
    backend = SimBackend(limits=limits, config=config, clock=clock)
    return GatewayService(config=config, backend=backend, limits=limits, clock=clock)


# --------------------------------------------------------------------------- #
# данные
# --------------------------------------------------------------------------- #

def test_gesture_set_is_what_the_ui_offers():
    assert gesture_names() == ["bow", "nod", "shake", "wave"]


@pytest.mark.parametrize("name", sorted(GESTURES))
def test_every_waypoint_mentions_only_real_joints(name):
    for pose, _hold in GESTURES[name]:
        unknown = set(pose) - set(JOINT_ORDER)
        assert not unknown, f"{name}: неизвестные суставы {unknown}"


@pytest.mark.parametrize("name", sorted(GESTURES))
def test_first_waypoint_is_complete(name):
    """Первая точка задаёт позу целиком.

    Последующие могут быть частичными — они накладываются на предыдущую. Но
    если неполной окажется ПЕРВАЯ, жест начнётся от той позы, в которой рука
    случайно оказалась, и выглядеть будет каждый раз по-разному.
    """
    first, _ = GESTURES[name][0]
    assert set(first) == set(JOINT_ORDER), f"{name}: в первой точке не все суставы"


@pytest.mark.parametrize("name", sorted(GESTURES))
def test_every_pose_is_inside_joint_limits(name, limits):
    """Ни одна точка не выходит за пределы из URDF.

    Клэмп в лимитере всё равно подрежет, но подрезанный жест выглядит иначе,
    чем задумано, и молча: рука просто не доходит до края.
    """
    for i, (pose, _hold) in enumerate(GESTURES[name]):
        for joint, value in pose.items():
            clamped = limits.clamp(joint, value)
            assert clamped == pytest.approx(value), (
                f"{name}, точка {i}: {joint}={value} подрезается до {clamped}"
            )


@pytest.mark.parametrize("name", sorted(GESTURES))
def test_gesture_has_more_than_one_waypoint(name):
    """Жест из одной точки — это пресет, а не движение.

    Ровно этим и был старый «wave»: рука однократно переставлялась в поднятую
    позу и замирала.
    """
    assert len(GESTURES[name]) >= 2


def test_wave_actually_swings_both_ways():
    """У махания основание уходит и влево, и вправо.

    Иначе получается не мах, а увод руки в сторону.
    """
    pans = [pose["shoulder_pan"] for pose, _ in GESTURES["wave"] if "shoulder_pan" in pose]
    assert max(pans) > 0.2 and min(pans) < -0.2


# --------------------------------------------------------------------------- #
# проигрыватель
# --------------------------------------------------------------------------- #

def _run_ticks(service, clock, n: int, dt: float = 0.02) -> None:
    """Прогнать n тактов командного цикла (без asyncio)."""
    for _ in range(n):
        clock.advance(dt)
        service._advance_gesture()
        service.limiter.tick()


def test_preset_wave_starts_a_gesture(service):
    assert service._apply_preset("wave").ok
    assert service._gesture is not None
    # Первая точка применена сразу, а не на следующем такте.
    assert service.limiter.target["elbow_flex"] == pytest.approx(-1.35)


def test_gesture_runs_through_all_waypoints_and_ends(service, clock):
    assert service._apply_preset("wave").ok
    _run_ticks(service, clock, 4000)          # заведомо больше, чем нужно
    assert service._gesture is None, "жест не завершился"


def test_gesture_visits_both_extremes(service, clock):
    """Рука действительно махнула в обе стороны, а не съехала в одну."""
    assert service._apply_preset("wave").ok
    seen = []
    for _ in range(4000):
        clock.advance(0.02)
        service._advance_gesture()
        service.limiter.tick()
        seen.append(service.limiter.q_cmd["shoulder_pan"])
        if service._gesture is None:
            break
    assert max(seen) > 0.3
    assert min(seen) < -0.3


def test_partial_waypoints_keep_the_rest_of_the_pose(service, clock):
    """Частичная точка не роняет остальные суставы в ноль.

    У «кивка» двигается только кисть; плечо и локоть обязаны остаться там,
    куда их поставила первая точка.
    """
    assert service._apply_preset("nod").ok
    lift = service.limiter.target["shoulder_lift"]
    _run_ticks(service, clock, 400)
    assert service.limiter.target["shoulder_lift"] == pytest.approx(lift)


def test_manual_command_cancels_the_gesture(service, clock):
    """Человек, взявшийся за слайдер, получает управление немедленно."""
    assert service._apply_preset("wave").ok
    _run_ticks(service, clock, 20)
    assert service._gesture is not None

    service._cancel_gesture()
    service.limiter.set_joint("shoulder_pan", 0.0)
    _run_ticks(service, clock, 200)
    assert service._gesture is None
    assert service.limiter.target["shoulder_pan"] == pytest.approx(0.0)


def test_home_is_not_a_gesture_and_cancels_one(service, clock):
    assert service._apply_preset("wave").ok
    assert service._apply_preset("home").ok
    assert service._gesture is None


def test_unknown_preset_is_rejected(service):
    result = service._apply_preset("нет-такого")
    assert not result.ok and result.error == ErrorCode.BAD_MESSAGE


def test_stuck_waypoint_does_not_hang_the_gesture(service, clock, monkeypatch):
    """Недостижимая точка не подвешивает жест навсегда.

    Если сустав упёрся, командная точка до цели не дойдёт никогда. Сторожевой
    срок обязан протолкнуть жест дальше, иначе рука останется в промежуточной
    позе, а следующий посетитель получит её оттуда.
    """
    assert service._apply_preset("wave").ok
    # Замораживаем движение: командная точка не двигается вообще.
    monkeypatch.setattr(service.limiter, "tick", lambda: dict(service.limiter.q_cmd))
    for _ in range(2000):
        clock.advance(0.02)
        service._advance_gesture()
        if service._gesture is None:
            break
    assert service._gesture is None


# --------------------------------------------------------------------------- #
# геометрия: рука не должна задевать стол
# --------------------------------------------------------------------------- #

#: Запас до столешницы, метры. Схват — не точка, и `gripper_frame_link`
#: сидит не на самом кончике губок.
TABLE_CLEARANCE = 0.03


@pytest.fixture
def fk(limits):
    from so101_gateway.config import default_urdf_path
    from so101_gateway.kinematics import load_kinematics

    return load_kinematics(default_urdf_path(), limits)


def _full_poses(gesture):
    """Точки жеста, развёрнутые в полные позы (частичные накладываются)."""
    pose: dict = {}
    for partial, _hold in gesture:
        pose.update(partial)
        yield dict(pose)


@pytest.mark.parametrize("name", sorted(GESTURES))
def test_gesture_never_puts_the_gripper_into_the_table(name, fk):
    """Ни одна точка жеста не опускает схват ниже столешницы.

    Основание стоит на столе, то есть z = 0 — это и есть стол. Проверять
    надо не только сами точки, но и путь между ними: ограничитель ведёт все
    суставы одновременно, и промежуточная поза бывает ниже обеих соседних.
    Ровно так «самая компактная» поза парковки ныряла на 5 см под стол.
    """
    poses = list(_full_poses(GESTURES[name]))
    for i in range(len(poses) - 1):
        a, b = poses[i], poses[i + 1]
        for step in range(21):
            t = step / 20
            mid = {j: a[j] + (b[j] - a[j]) * t for j in a}
            z = fk.forward_kinematics(mid)[2]
            assert z >= TABLE_CLEARANCE, (
                f"{name}: между точками {i} и {i+1} схват опускается до z={z:.3f}"
            )


def test_park_pose_is_low_and_reachable_from_home_without_hitting_the_table(fk):
    """Парковка низкая, и путь к ней из home не задевает стол."""
    from so101_gateway.backends.sim import HOME_POSE
    from so101_gateway.gestures import PARK_POSE

    park_z = fk.forward_kinematics(PARK_POSE)[2]
    assert park_z < 0.10, "парковка должна быть низкой, иначе она не «мягкая»"
    assert park_z >= TABLE_CLEARANCE

    for step in range(41):
        t = step / 40
        mid = {j: HOME_POSE[j] + (PARK_POSE[j] - HOME_POSE[j]) * t for j in HOME_POSE}
        z = fk.forward_kinematics(mid)[2]
        assert z >= TABLE_CLEARANCE, f"путь в парковку опускается до z={z:.3f}"
