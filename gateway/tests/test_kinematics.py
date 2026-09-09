"""Тесты кинематики, собранной из URDF.

Главный тест здесь — ``test_zero_pose_matches_the_measured_tf``: он сверяет FK
в нулевой позе с величиной, ЗАМЕРЕННОЙ на живой системе через TF
``base_link → gripper_frame_link``. Ровно на этом месте модель симулятора
однажды разошлась с настоящей рукой на треть метра (плоская трёхзвенная модель
давала x=0.039 / z=0.377 против настоящих x=0.391 / z=0.226), и тест стоит
здесь, чтобы это не повторилось.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

import numpy as np
import pytest

from so101_gateway.config import default_urdf_path
from so101_gateway.kinematics import (
    BASE_LINK,
    EE_LINK,
    ZERO_POSE_EE,
    ZERO_POSE_TOL,
    Kinematics,
    _fallback_chain,
    load_kinematics,
    parse_urdf_chain,
)
from so101_gateway.safety.limits import JOINT_ORDER, JointLimits

#: Игрушечная плоская рука: два звена по метру, начало на высоте 1 м.
#: FK считается на бумаге, поэтому парсер проверяется независимо от SO-101.
TOY_URDF = """<?xml version="1.0"?>
<robot name="toy">
  <link name="base_link"/>
  <link name="l1"/>
  <link name="l2"/>
  <link name="gripper_frame_link"/>
  <joint name="j1" type="revolute">
    <origin xyz="0 0 1" rpy="0 0 0"/>
    <parent link="base_link"/>
    <child link="l1"/>
    <axis xyz="0 0 1"/>
    <limit effort="1" velocity="1" lower="-1.5" upper="1.5"/>
  </joint>
  <joint name="j2" type="revolute">
    <origin xyz="1 0 0" rpy="0 0 0"/>
    <parent link="l1"/>
    <child link="l2"/>
    <axis xyz="0 0 1"/>
    <limit effort="1" velocity="1" lower="-1.5" upper="1.5"/>
  </joint>
  <joint name="tip" type="fixed">
    <origin xyz="1 0 0" rpy="0 0 0"/>
    <parent link="l2"/>
    <child link="gripper_frame_link"/>
  </joint>
</robot>
"""

TOY_LIMITS = JointLimits(limits={"j1": (-1.5, 1.5), "j2": (-1.5, 1.5)}, source="urdf")


def _write(tmp_path, text, name="robot.urdf"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def limits() -> JointLimits:
    return JointLimits.fallback()


@pytest.fixture
def kin(limits) -> Kinematics:
    """Модель SO-101 из захардкоженного снимка цепи.

    Снимок обязан совпадать со сгенерированным URDF (см. тест ниже), поэтому
    тесты поведения не зависят от того, лежит ли уже файл в ``deploy/``.
    """
    return Kinematics(_fallback_chain(), limits, source="fallback")


def generated_urdf() -> Path:
    return Path(default_urdf_path())


# --------------------------------------------------------------------------- #
# контрольная точка: нулевая поза
# --------------------------------------------------------------------------- #

def test_zero_pose_matches_the_measured_tf(kin):
    """В нулевой позе схват там же, где его показывает TF настоящей руки.

    x=+0.391, y=0.000, z=+0.226 — замер на живой системе. Это не пожелание и не
    результат подгонки: если FK перестала это воспроизводить, геометрия
    разошлась с URDF, и ни рабочая зона, ни вкладка «Точка» больше не значат
    того же, что на стенде.
    """
    x, y, z = kin.forward_kinematics({})
    assert x == pytest.approx(ZERO_POSE_EE[0], abs=ZERO_POSE_TOL)
    assert y == pytest.approx(ZERO_POSE_EE[1], abs=ZERO_POSE_TOL)
    assert z == pytest.approx(ZERO_POSE_EE[2], abs=ZERO_POSE_TOL)
    # и явными числами, чтобы тест читался без похода в константы
    assert (x, y, z) == pytest.approx((0.391, 0.000, 0.226), abs=0.005)


def test_zero_pose_from_the_generated_urdf_matches_the_measured_tf(limits):
    """То же самое, но по настоящему файлу, а не по снимку в коде."""
    path = generated_urdf()
    if not path.exists():                     # сборка ещё не клала файл
        pytest.skip("deploy/so101_follower.generated.urdf недоступен")
    kin = Kinematics(parse_urdf_chain(path), limits)
    assert kin.forward_kinematics({}) == pytest.approx((0.391, 0.000, 0.226), abs=0.005)


def test_fallback_chain_is_a_faithful_snapshot_of_the_generated_urdf(limits):
    """Фолбэк — снимок того же URDF, а не «примерно похожая» геометрия."""
    path = generated_urdf()
    if not path.exists():
        pytest.skip("deploy/so101_follower.generated.urdf недоступен")
    from_file = Kinematics(parse_urdf_chain(path), limits)
    snapshot = Kinematics(_fallback_chain(), limits)

    assert [j.name for j in from_file.chain] == [j.name for j in snapshot.chain]
    for pose in ({}, {"shoulder_lift": -0.9, "elbow_flex": 0.9},
                 {"shoulder_pan": 1.2, "wrist_flex": -0.7, "wrist_roll": 2.0}):
        assert from_file.forward_kinematics(pose) == pytest.approx(
            snapshot.forward_kinematics(pose), abs=1e-6
        )


# --------------------------------------------------------------------------- #
# разбор URDF
# --------------------------------------------------------------------------- #

def test_chain_runs_from_base_to_the_gripper_frame(kin):
    chain = kin.chain
    assert chain[0].parent == BASE_LINK
    assert chain[-1].child == EE_LINK
    # цепь связная: child каждого сустава — parent следующего
    for previous, current in zip(chain, chain[1:]):
        assert previous.child == current.parent


def test_gripper_is_not_part_of_the_chain(kin):
    """Захват ведёт в отдельную ветку и положение схвата не меняет."""
    assert "gripper" not in kin.joint_names
    assert kin.joint_names == [
        "shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll",
    ]
    assert all(name in JOINT_ORDER for name in kin.joint_names)
    closed = kin.forward_kinematics({"gripper": 0.0})
    opened = kin.forward_kinematics({"gripper": 1.7})
    assert closed == pytest.approx(opened, abs=1e-12)


def test_parse_reads_origin_axis_and_fixed_joints(tmp_path):
    chain = parse_urdf_chain(_write(tmp_path, TOY_URDF))
    kin = Kinematics(chain, TOY_LIMITS)

    assert [j.name for j in chain] == ["j1", "j2", "tip"]
    assert kin.joint_names == ["j1", "j2"]          # fixed в подвижные не попал
    assert kin.forward_kinematics({}) == pytest.approx((2.0, 0.0, 1.0))
    assert kin.forward_kinematics({"j1": math.pi / 2}) == pytest.approx((0.0, 2.0, 1.0), abs=1e-12)
    assert kin.forward_kinematics({"j2": math.pi / 2}) == pytest.approx((1.0, 1.0, 1.0), abs=1e-12)


def test_parse_supports_prismatic_joints(tmp_path):
    urdf = TOY_URDF.replace('<joint name="j2" type="revolute">', '<joint name="j2" type="prismatic">')
    kin = Kinematics(parse_urdf_chain(_write(tmp_path, urdf)), TOY_LIMITS)
    # ось j2 — z, значит выдвижение поднимает схват
    assert kin.forward_kinematics({"j2": 0.5}) == pytest.approx((2.0, 0.0, 1.5))


def test_parse_raises_when_the_chain_does_not_reach_the_base(tmp_path):
    orphan = TOY_URDF.replace('<parent link="base_link"/>', '<parent link="nowhere"/>')
    with pytest.raises(ValueError) as exc:
        parse_urdf_chain(_write(tmp_path, orphan))
    assert "base_link" in str(exc.value)


def test_parse_raises_on_unsupported_joint_type(tmp_path):
    weird = TOY_URDF.replace('<joint name="j2" type="revolute">', '<joint name="j2" type="floating">')
    with pytest.raises(ValueError):
        parse_urdf_chain(_write(tmp_path, weird))


def test_parse_raises_on_malformed_xml(tmp_path):
    with pytest.raises(ValueError):
        parse_urdf_chain(_write(tmp_path, "<robot><joint></robot>"))


def test_parse_raises_on_zero_axis(tmp_path):
    broken = TOY_URDF.replace('<axis xyz="0 0 1"/>', '<axis xyz="0 0 0"/>')
    with pytest.raises(ValueError):
        parse_urdf_chain(_write(tmp_path, broken))


# --------------------------------------------------------------------------- #
# загрузка
# --------------------------------------------------------------------------- #

def test_load_falls_back_with_warning_when_file_missing(tmp_path, caplog, limits):
    with caplog.at_level(logging.WARNING):
        kin = load_kinematics(tmp_path / "nope.urdf", limits)
    assert kin.source == "fallback"
    # фолбэк обязан быть не хуже настоящего файла в контрольной точке
    assert kin.forward_kinematics({}) == pytest.approx((0.391, 0.000, 0.226), abs=0.005)
    assert caplog.records


def test_load_falls_back_with_warning_when_file_broken(tmp_path, caplog, limits):
    path = _write(tmp_path, "not xml at all")
    with caplog.at_level(logging.WARNING):
        kin = load_kinematics(path, limits)
    assert kin.source == "fallback"
    assert caplog.records


def test_load_without_path_falls_back(caplog, limits):
    with caplog.at_level(logging.WARNING):
        kin = load_kinematics(None, limits)
    assert kin.source == "fallback"
    assert caplog.records


# --------------------------------------------------------------------------- #
# свойства прямой задачи
# --------------------------------------------------------------------------- #

def test_pan_rotates_the_arm_about_a_vertical_axis(kin):
    """``shoulder_pan`` — единственный вертикальный сустав: z не меняется.

    Ось проходит НЕ через начало ``base_link``, а через origin первого сустава
    (x≈0.039), поэтому «окружность вокруг нуля» здесь не свойство, а артефакт
    старой модели. Проверяем то, что действительно обязано выполняться:
    высота сохраняется, расстояние до оси сохраняется, знак ``y`` следует за
    знаком угла.

    Допуск 1e-5, а не машинный: в URDF π записано как 3.14159, из-за чего ось
    ``shoulder_pan`` отклонена от вертикали примерно на 2.6e-6 рад, и высота
    схвата при повороте гуляет на ~1 мкм. Это свойство самого файла, а не
    модели — округлять его здесь мы не вправе.
    """
    base = kin.forward_kinematics({})
    positive = kin.forward_kinematics({"shoulder_pan": 0.7})
    negative = kin.forward_kinematics({"shoulder_pan": -0.7})

    assert positive[2] == pytest.approx(base[2], abs=1e-5)
    assert negative[2] == pytest.approx(base[2], abs=1e-5)
    # знак: origin сустава несёт rpy (π, 0, −π), поэтому ПОЛОЖИТЕЛЬНЫЙ
    # shoulder_pan уводит схват в СТОРОНУ −y
    assert positive[1] < -0.1 < 0.0 < negative[1]

    # расстояние до оси вращения сохраняется
    pivot = kin.chain[0].origin[:3, 3]
    expected = math.hypot(base[0] - pivot[0], base[1] - pivot[1])
    for point in (positive, negative):
        assert math.hypot(point[0] - pivot[0], point[1] - pivot[1]) == pytest.approx(
            expected, abs=1e-5
        )
    # а расстояние до НАЧАЛА base_link — нет: ось смещена вперёд на 39 мм
    assert math.hypot(positive[0], positive[1]) != pytest.approx(
        math.hypot(*base[:2]), abs=1e-3
    )


def test_batched_fk_agrees_with_the_single_pose_one(kin, limits):
    """Пучковая FK — та же геометрия, что и обычная.

    На ней стоит якобиан численной IK, и буферы матриц она переиспользует между
    вызовами. Тест ловит и расхождение формул, и загрязнение буфера остатками
    предыдущего вызова, поэтому один и тот же пучок считается дважды.
    """
    rng = np.random.default_rng(1234)
    Q = np.array(
        [[rng.uniform(*limits.bounds(n)) for n in kin.joint_names] for _ in range(9)]
    )
    first = kin._fk_batch(Q)
    second = kin._fk_batch(Q)                       # тот же буфер, второй проход

    assert first == pytest.approx(second, abs=1e-15)
    for row, values in zip(first, Q):
        pose = dict(zip(kin.joint_names, values))
        assert tuple(row) == pytest.approx(kin.forward_kinematics(pose), abs=1e-12)

    # другой размер пучка — другой буфер, результат тот же
    assert kin._fk_batch(Q[:3]) == pytest.approx(first[:3], abs=1e-15)


def test_forward_kinematics_ignores_unknown_joint_names(kin):
    assert kin.forward_kinematics({"нет такого": 1.0}) == pytest.approx(
        kin.forward_kinematics({}), abs=1e-12
    )


# --------------------------------------------------------------------------- #
# обратная задача
# --------------------------------------------------------------------------- #

def test_ik_is_the_inverse_of_fk_for_random_reachable_poses(kin, limits):
    """Достижимая по построению точка обязана решаться."""
    rng = np.random.default_rng(20240901)
    for _ in range(40):
        pose = {n: float(rng.uniform(*limits.bounds(n))) for n in kin.joint_names}
        point = kin.forward_kinematics(pose)
        solved = kin.inverse_kinematics(point, seed=pose)
        assert solved is not None, point
        assert kin.forward_kinematics(solved) == pytest.approx(point, abs=1e-5)


def test_ik_solves_without_a_seed(kin):
    """Без подсказки — тоже: перезапуски перебирают конфигурации локтя."""
    for point in ((0.25, 0.0, 0.25), (0.20, 0.15, 0.30), (0.30, -0.10, 0.15)):
        solved = kin.inverse_kinematics(point)
        assert solved is not None, point
        assert kin.forward_kinematics(solved) == pytest.approx(point, abs=1e-5)


def test_ik_result_stays_within_joint_limits(kin, limits):
    """Клэмп на каждом шаге спуска: решение физически исполнимо."""
    for point in ((0.35, 0.0, 0.10), (0.15, 0.25, 0.35), (0.20, -0.20, 0.10)):
        solved = kin.inverse_kinematics(point)
        assert solved is not None, point
        for name, value in solved.items():
            lo, hi = limits.bounds(name)
            assert lo <= value <= hi, (name, value)


def test_ik_returns_none_beyond_the_reach_of_the_chain(kin):
    """Заведомо далёкая точка отвергается, а не «примерно достигается»."""
    assert kin.inverse_kinematics((2.0, 0.0, 0.2)) is None
    assert kin.inverse_kinematics((0.0, 0.0, 1.5)) is None


def test_ik_returns_none_for_an_unreachable_point_near_the_base(kin):
    """Дыра рабочей зоны у основания: сложиться настолько пределы не дают.

    Перебор по сетке 13⁴ в пределах суставов не подходит к этой точке ближе
    чем на 16 мм — то есть это настоящая недостижимость, а не слабость решателя.
    """
    assert kin.inverse_kinematics((0.1226, 0.0, 0.2058)) is None


def test_ik_rejects_non_finite_targets(kin):
    assert kin.inverse_kinematics((float("nan"), 0.0, 0.2)) is None
    assert kin.inverse_kinematics((float("inf"), 0.0, 0.2)) is None


def test_ik_returns_the_solution_nearest_to_the_seed(kin, limits):
    """Seed выбирает ветку решения: джог не должен «щёлкать» позой."""
    pose = {"shoulder_pan": 0.0, "shoulder_lift": -0.9, "elbow_flex": 0.9,
            "wrist_flex": 0.0, "wrist_roll": 0.0}
    point = kin.forward_kinematics(pose)
    nearby = (point[0], point[1], point[2] - 0.01)

    solved = kin.inverse_kinematics(nearby, seed=pose)
    assert solved is not None
    for name, value in solved.items():
        assert abs(value - pose[name]) < 0.5, name


def test_ik_never_reports_success_without_reaching_the_target(kin):
    """Ответ ``None`` честен: любое НЕ-``None`` решение проверено прямой задачей."""
    rng = np.random.default_rng(7)
    checked = 0
    for _ in range(60):
        point = (
            float(rng.uniform(-0.5, 0.5)),
            float(rng.uniform(-0.5, 0.5)),
            float(rng.uniform(-0.3, 0.6)),
        )
        solved = kin.inverse_kinematics(point)
        if solved is None:
            continue
        checked += 1
        assert kin.forward_kinematics(solved) == pytest.approx(point, abs=1e-5)
    assert checked > 0, "выборка не содержала ни одной достижимой точки"
