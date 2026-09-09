"""Тесты парсера пределов суставов.

Парсер обязан быть проверяем независимо от того, положил ли уже соседний агент
сгенерированный URDF в deploy/ — поэтому основной тест идёт по синтетическому
URDF-фрагменту во временном файле.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from so101_gateway.safety.limits import (
    FALLBACK_LIMITS,
    JOINT_ORDER,
    JointLimits,
    load_joint_limits,
    parse_urdf_limits,
)

SYNTHETIC_URDF = """<?xml version="1.0"?>
<robot name="so101_follower">
  <link name="base_link"/>
  <joint name="base_fixed" type="fixed">
    <parent link="world"/>
    <child link="base_link"/>
  </joint>
  <joint name="shoulder_pan" type="revolute">
    <axis xyz="0 0 1"/>
    <limit effort="10" velocity="10" lower="-1.5" upper="1.5"/>
  </joint>
  <joint name="shoulder_lift" type="revolute">
    <limit effort="10" velocity="10" lower="-1.25" upper="1.75"/>
  </joint>
  <joint name="elbow_flex" type="revolute">
    <limit effort="10" velocity="10" lower="-1.0" upper="1.0"/>
  </joint>
  <joint name="wrist_flex" type="revolute">
    <limit effort="10" velocity="10" lower="-0.9" upper="0.9"/>
  </joint>
  <joint name="wrist_roll" type="revolute">
    <limit effort="10" velocity="10" lower="-2.0" upper="2.5"/>
  </joint>
  <joint name="gripper" type="revolute">
    <limit effort="10" velocity="10" lower="-0.1" upper="1.6"/>
  </joint>
  <joint name="camera_mount" type="fixed">
    <parent link="wrist_link"/>
    <child link="camera_link"/>
  </joint>
</robot>
"""


def _write(tmp_path, text, name="so101.urdf"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_synthetic_urdf_reads_lower_and_upper(tmp_path):
    limits = parse_urdf_limits(_write(tmp_path, SYNTHETIC_URDF))

    assert limits.names == list(JOINT_ORDER)
    assert limits.bounds("shoulder_pan") == pytest.approx((-1.5, 1.5))
    assert limits.bounds("shoulder_lift") == pytest.approx((-1.25, 1.75))
    assert limits.bounds("wrist_roll") == pytest.approx((-2.0, 2.5))
    assert limits.bounds("gripper") == pytest.approx((-0.1, 1.6))
    assert limits.source == "urdf"


def test_parse_keeps_canonical_joint_order_regardless_of_file_order(tmp_path):
    shuffled = SYNTHETIC_URDF.replace(
        '<joint name="shoulder_pan"', '<joint name="ZZZ_shoulder_pan"'
    ).replace(
        '<joint name="gripper"', '<joint name="shoulder_pan"'
    ).replace(
        '<joint name="ZZZ_shoulder_pan"', '<joint name="gripper"'
    )
    limits = parse_urdf_limits(_write(tmp_path, shuffled))
    assert limits.names == list(JOINT_ORDER)


def test_parse_ignores_fixed_joints_and_unknown_joints(tmp_path):
    limits = parse_urdf_limits(_write(tmp_path, SYNTHETIC_URDF))
    assert "base_fixed" not in limits.names
    assert "camera_mount" not in limits.names


def test_parse_ignores_joint_references_inside_transmissions(tmp_path):
    """Регрессия: в апстримном URDF есть <transmission><joint name="gripper">.

    Такая вложенная ссылка не имеет ни type, ни <limit>; если обходить дерево
    рекурсивно, парсер падает на настоящем файле.
    """
    with_transmission = SYNTHETIC_URDF.replace(
        "</robot>",
        """  <transmission name="gripper_trans">
    <type>transmission_interface/SimpleTransmission</type>
    <joint name="gripper">
      <hardwareInterface>hardware_interface/PositionJointInterface</hardwareInterface>
    </joint>
    <actuator name="motor6"><mechanicalReduction>1</mechanicalReduction></actuator>
  </transmission>
</robot>""",
    )
    limits = parse_urdf_limits(_write(tmp_path, with_transmission))
    assert limits.bounds("gripper") == pytest.approx((-0.1, 1.6))


def test_parse_reads_the_real_upstream_urdf():
    """Парсер обязан читать настоящий URDF апстрима, а не только синтетику."""
    upstream = (
        Path(__file__).resolve().parents[2]
        / "upstream/so101_description/urdf/legacy/so101_follower.urdf"
    )
    if not upstream.exists():          # сабмодуль может быть не подтянут
        pytest.skip("сабмодуль upstream недоступен")
    limits = parse_urdf_limits(upstream)
    assert limits.source == "urdf"
    assert limits.names == list(JOINT_ORDER)
    # фолбэк списан именно с этого файла — значения обязаны совпасть
    for name in JOINT_ORDER:
        assert limits.bounds(name) == pytest.approx(FALLBACK_LIMITS[name])


def test_parse_raises_when_a_required_joint_is_missing(tmp_path):
    broken = SYNTHETIC_URDF.replace('<joint name="gripper" type="revolute">', '<joint name="gripper_x" type="revolute">')
    with pytest.raises(ValueError) as exc:
        parse_urdf_limits(_write(tmp_path, broken))
    assert "gripper" in str(exc.value)


def test_parse_raises_when_limit_tag_absent(tmp_path):
    broken = SYNTHETIC_URDF.replace(
        '<limit effort="10" velocity="10" lower="-1.0" upper="1.0"/>', ""
    )
    with pytest.raises(ValueError):
        parse_urdf_limits(_write(tmp_path, broken))


def test_parse_raises_on_malformed_xml(tmp_path):
    with pytest.raises(ValueError):
        parse_urdf_limits(_write(tmp_path, "<robot><joint></robot>"))


def test_load_falls_back_with_warning_when_file_missing(tmp_path, caplog):
    missing = tmp_path / "nope.urdf"
    with caplog.at_level(logging.WARNING):
        limits = load_joint_limits(missing)
    assert limits.source == "fallback"
    assert limits.bounds("shoulder_pan") == pytest.approx(FALLBACK_LIMITS["shoulder_pan"])
    assert any("fallback" in r.message.lower() or "URDF" in r.message for r in caplog.records)


def test_load_falls_back_with_warning_when_file_broken(tmp_path, caplog):
    path = _write(tmp_path, "not xml at all")
    with caplog.at_level(logging.WARNING):
        limits = load_joint_limits(path)
    assert limits.source == "fallback"


def test_load_uses_urdf_when_present(tmp_path):
    limits = load_joint_limits(_write(tmp_path, SYNTHETIC_URDF))
    assert limits.source == "urdf"
    assert limits.bounds("elbow_flex") == pytest.approx((-1.0, 1.0))


def test_clamp_respects_bounds():
    limits = JointLimits.fallback()
    lo, hi = limits.bounds("shoulder_pan")
    assert limits.clamp("shoulder_pan", hi + 5.0) == pytest.approx(hi)
    assert limits.clamp("shoulder_pan", lo - 5.0) == pytest.approx(lo)
    assert limits.clamp("shoulder_pan", 0.1) == pytest.approx(0.1)


def test_in_range_reports_violations():
    limits = JointLimits.fallback()
    lo, hi = limits.bounds("elbow_flex")
    assert limits.in_range("elbow_flex", 0.0)
    assert not limits.in_range("elbow_flex", hi + 0.01)
    assert not limits.in_range("elbow_flex", lo - 0.01)


def test_unknown_joint_is_rejected():
    limits = JointLimits.fallback()
    assert not limits.has("nose_wiggle")
    with pytest.raises(KeyError):
        limits.bounds("nose_wiggle")


def test_fallback_matches_upstream_urdf_values():
    """Значения фолбэка списаны с апстримного URDF, а не выдуманы."""
    limits = JointLimits.fallback()
    assert limits.bounds("shoulder_pan") == pytest.approx((-1.91986, 1.91986))
    assert limits.bounds("elbow_flex") == pytest.approx((-1.69, 1.69))
    assert limits.bounds("wrist_roll") == pytest.approx((-2.74385, 2.84121))


def test_config_payload_reports_gripper_as_normalised_unit_interval():
    """Контракт: захват всюду на проводе — доли 0..1 с пометкой normalized."""
    payload = JointLimits.fallback().as_config_payload()
    by_name = {entry["name"]: entry for entry in payload}

    gripper = by_name["gripper"]
    assert gripper["min"] == 0.0
    assert gripper["max"] == 1.0
    assert gripper["normalized"] is True

    # остальные суставы — радианы из URDF и без пометки
    pan = by_name["shoulder_pan"]
    assert pan["min"] == pytest.approx(-1.91986)
    assert "normalized" not in pan


def test_gripper_normalisation_maps_unit_interval_to_joint_range():
    """WS-сообщение gripper приходит в 0..1 и должно лечь в радианы сустава."""
    limits = JointLimits.fallback()
    lo, hi = limits.bounds("gripper")
    assert limits.gripper_from_unit(0.0) == pytest.approx(lo)
    assert limits.gripper_from_unit(1.0) == pytest.approx(hi)
    assert limits.gripper_from_unit(0.5) == pytest.approx((lo + hi) / 2)
    # выход за 0..1 подрезается
    assert limits.gripper_from_unit(5.0) == pytest.approx(hi)
    assert limits.gripper_from_unit(-5.0) == pytest.approx(lo)
