"""Пределы суставов SO-101.

Источник истины — URDF апстрима, который сборка кладёт в
``deploy/so101_follower.generated.urdf``. Пока файла нет (или он битый),
используется захардкоженный фолбэк со значениями, списанными с
``upstream/so101_description`` — с явным предупреждением в лог.

Никаких внешних зависимостей: только xml.etree из стандартной библиотеки.
"""

from __future__ import annotations

import logging
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple

log = logging.getLogger(__name__)

__all__ = [
    "GRIPPER",
    "JOINT_ORDER",
    "JOINT_LABELS",
    "FALLBACK_LIMITS",
    "JointLimits",
    "parse_urdf_limits",
    "load_joint_limits",
]

#: Сустав схвата: единственный, который на проводе нормирован в 0..1.
GRIPPER = "gripper"

#: Канонический порядок суставов. Совпадает с порядком в /api/config.
JOINT_ORDER: Sequence[str] = (
    "shoulder_pan",
    "shoulder_lift",
    "elbow_flex",
    "wrist_flex",
    "wrist_roll",
    "gripper",
)

#: Человекочитаемые подписи для фронтенда (см. docs/api.md, GET /api/config).
JOINT_LABELS: Mapping[str, str] = {
    "shoulder_pan": "Основание",
    "shoulder_lift": "Плечо",
    "elbow_flex": "Локоть",
    "wrist_flex": "Кисть",
    "wrist_roll": "Поворот кисти",
    "gripper": "Захват",
}

#: Фолбэк. Значения взяты из upstream/so101_description/urdf/so101_arm_common.xacro
#: и .../end_effectors/so101_ee_*.xacro — то есть из того же источника, из которого
#: генерируется deploy/so101_follower.generated.urdf.
FALLBACK_LIMITS: Mapping[str, Tuple[float, float]] = {
    "shoulder_pan": (-1.91986, 1.91986),
    "shoulder_lift": (-1.74533, 1.74533),
    "elbow_flex": (-1.69, 1.69),
    "wrist_flex": (-1.65806, 1.65806),
    "wrist_roll": (-2.74385, 2.84121),
    "gripper": (-0.174533, 1.74533),
}


@dataclass(frozen=True)
class JointLimits:
    """Пределы суставов + откуда они взялись (``urdf`` | ``fallback``)."""

    limits: Mapping[str, Tuple[float, float]]
    source: str = "fallback"

    @classmethod
    def fallback(cls) -> "JointLimits":
        return cls(limits=dict(FALLBACK_LIMITS), source="fallback")

    @property
    def names(self) -> List[str]:
        return [n for n in JOINT_ORDER if n in self.limits]

    def has(self, joint: str) -> bool:
        return joint in self.limits

    def bounds(self, joint: str) -> Tuple[float, float]:
        return self.limits[joint]

    def clamp(self, joint: str, value: float) -> float:
        lo, hi = self.limits[joint]
        return lo if value < lo else hi if value > hi else value

    def in_range(self, joint: str, value: float) -> bool:
        lo, hi = self.limits[joint]
        return lo <= value <= hi

    def mid(self, joint: str) -> float:
        lo, hi = self.limits[joint]
        return (lo + hi) / 2.0

    def zeros(self) -> Dict[str, float]:
        """Нулевая поза, подрезанная по пределам (нуль всегда внутри диапазона)."""
        return {name: self.clamp(name, 0.0) for name in self.names}

    def gripper_from_unit(self, value: float) -> float:
        """0..1 с провода → радианы сустава.

        Контракт задаёт значение схвата в долях (0 — закрыт, 1 — открыт),
        а URDF — в радианах. Отображение линейное, вход подрезается.
        """
        lo, hi = self.limits[GRIPPER]
        v = 0.0 if value < 0.0 else 1.0 if value > 1.0 else value
        return lo + (hi - lo) * v

    def gripper_to_unit(self, radians: float) -> float:
        """Радианы сустава → 0..1 для отправки клиенту."""
        lo, hi = self.limits[GRIPPER]
        if hi == lo:  # pragma: no cover - вырожденный URDF
            return 0.0
        return (radians - lo) / (hi - lo)

    def as_config_payload(self) -> List[Dict[str, object]]:
        """Секция ``joints`` для GET /api/config.

        Захват — единственное исключение из «пределы из URDF»: на проводе он
        всюду выражен в долях 0..1 и помечен ``normalized``. Радианы остаются
        внутри шлюза (docs/api.md §2).
        """
        out: List[Dict[str, object]] = []
        for name in self.names:
            if name == GRIPPER:
                out.append(
                    {
                        "name": name,
                        "min": 0.0,
                        "max": 1.0,
                        "label": JOINT_LABELS.get(name, name),
                        "normalized": True,
                    }
                )
                continue
            lo, hi = self.limits[name]
            out.append(
                {
                    "name": name,
                    "min": round(lo, 5),
                    "max": round(hi, 5),
                    "label": JOINT_LABELS.get(name, name),
                }
            )
        return out


def _required(names: Iterable[str], found: Mapping[str, Tuple[float, float]]) -> None:
    missing = [n for n in names if n not in found]
    if missing:
        raise ValueError(f"в URDF нет пределов для суставов: {', '.join(missing)}")


def parse_urdf_limits(path: os.PathLike | str) -> JointLimits:
    """Прочитать lower/upper из URDF.

    Бросает ``ValueError`` на битом XML, отсутствующем ``<limit>`` или неполном
    наборе суставов — вызывающий сам решает, падать или уходить в фолбэк.
    """
    path = Path(path)
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        raise ValueError(f"не разобрать URDF {path}: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"не прочитать URDF {path}: {exc}") from exc

    root = tree.getroot()
    found: Dict[str, Tuple[float, float]] = {}
    # ТОЛЬКО прямые потомки <robot>: вложенные <joint> внутри <transmission>
    # (и <gazebo>) — это ссылки по имени, у них нет ни type, ни <limit>.
    for joint in root.findall("joint"):
        name = joint.get("name")
        if name not in JOINT_ORDER:
            continue
        if joint.get("type") in {"fixed", "continuous"}:
            # continuous не имеет пределов по определению; такого в SO-101 нет,
            # но пусть лучше явно упадёт на проверке полноты набора.
            continue
        limit = joint.find("limit")
        if limit is None:
            raise ValueError(f"у сустава {name} в {path} нет тега <limit>")
        lower, upper = limit.get("lower"), limit.get("upper")
        if lower is None or upper is None:
            raise ValueError(f"у сустава {name} в {path} нет lower/upper")
        try:
            lo, hi = float(lower), float(upper)
        except ValueError as exc:
            raise ValueError(f"нечисловые пределы у сустава {name} в {path}") from exc
        if lo > hi:
            raise ValueError(f"у сустава {name} lower > upper")
        found[name] = (lo, hi)

    _required(JOINT_ORDER, found)
    return JointLimits(limits={n: found[n] for n in JOINT_ORDER}, source="urdf")


def load_joint_limits(path: os.PathLike | str | None) -> JointLimits:
    """Прочитать пределы из URDF, при любой неудаче — фолбэк + WARNING."""
    if path is None:
        log.warning(
            "Путь к URDF не задан, используются захардкоженные пределы суставов "
            "(fallback). Проверьте URDF_PATH."
        )
        return JointLimits.fallback()

    path = Path(path)
    if not path.exists():
        log.warning(
            "URDF %s не найден — используются захардкоженные пределы суставов "
            "(fallback, значения из upstream/so101_description). Пределы могут "
            "разойтись с реальной сборкой руки.",
            path,
        )
        return JointLimits.fallback()

    try:
        limits = parse_urdf_limits(path)
    except ValueError as exc:
        log.warning(
            "URDF %s не разобран (%s) — используются захардкоженные пределы "
            "суставов (fallback).",
            path,
            exc,
        )
        return JointLimits.fallback()

    log.info("Пределы суставов прочитаны из URDF %s", path)
    return limits
