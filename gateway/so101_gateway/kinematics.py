"""Кинематика SO-101, выведенная ИЗ URDF.

Источник истины — тот же файл, из которого ``safety/limits.py`` читает пределы
суставов: ``deploy/so101_follower.generated.urdf`` (путь в ``Config.urdf_path``).
Здесь из него берутся ``<origin>`` (xyz + rpy) и ``<axis>`` каждого сустава на
пути от ``base_link`` до ``gripper_frame_link``, и FK собирается честным
перемножением однородных преобразований — ровно так же, как это делает
``robot_state_publisher``, чей TF читает ROS-бэкенд.

Смысл модуля: у симулятора и у стенда должна быть ОДНА геометрия, совпадающая
по построению, а не по совпадению. Контрольная точка — нулевая поза: замеренный
на живой системе TF ``base_link → gripper_frame_link`` даёт
x=+0.391, y=0.000, z=+0.226 (см. ``ZERO_POSE_EE``).

Разбор XML — как в ``safety/limits.py``: только ``xml.etree`` из стандартной
библиотеки. numpy нужен уже для самой кинематики (матрицы, якобиан, DLS).
"""

from __future__ import annotations

import logging
import math
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from .safety.limits import JointLimits

log = logging.getLogger(__name__)

__all__ = [
    "BASE_LINK",
    "EE_LINK",
    "ZERO_POSE_EE",
    "FALLBACK_CHAIN",
    "ChainJoint",
    "Kinematics",
    "parse_urdf_chain",
    "load_kinematics",
]

Point = Tuple[float, float, float]

#: Начало и конец кинематической цепи. ``gripper_frame_link`` — это тот самый
#: фрейм, который ROS-бэкенд запрашивает у TF, поэтому «схват» здесь и там
#: означает одну и ту же точку.
BASE_LINK = "base_link"
EE_LINK = "gripper_frame_link"

#: Положение схвата в нулевой позе, замеренное на живой системе через TF.
#: Это контрольная точка всей модели: если FK её не воспроизводит, геометрия
#: разошлась с URDF и её нельзя использовать ни для «Точки», ни для рабочей зоны.
ZERO_POSE_EE: Point = (0.391, 0.000, 0.226)

#: Допуск сверки с ``ZERO_POSE_EE`` (метры).
ZERO_POSE_TOL = 0.005

# --- параметры численной IK -------------------------------------------------- #

#: Сходимость по позиции: 1 мкм. Физического смысла такая точность не имеет
#: (шаг джога — 1 см), но вблизи решения DLS сходится почти квадратично, и
#: запас в четыре порядка стоит ~5% времени. Зато «IK обратна FK» проверяется
#: как настоящее свойство, а не с точностью «плюс-минус миллиметр».
IK_POSITION_TOL = 1e-6
#: Потолок итераций. Несходимость — это ответ ``None``, а не «примерно доехали».
IK_MAX_ITERATIONS = 80
#: Столько итераций подряд без улучшения — спуск встал (уткнулся в пределы или
#: подошёл к ближайшей достижимой точке). Дальше крутить бессмысленно: недостижимые
#: точки должны отвергаться быстро, иначе джог упирается в них по полсекунды.
IK_STALL_ITERATIONS = 8
#: Что считать улучшением: невязка должна упасть хотя бы на эту долю за итерацию.
IK_MIN_PROGRESS = 1e-3
#: Демпфирование λ метода затухающих наименьших квадратов. Гасит вырождение
#: якобиана: у 5-звенной руки вблизи полного вылета (и у ``wrist_roll``, который
#: почти не двигает точку) столбцы якобиана коллинеарны/нулевые.
IK_DAMPING = 0.03
#: Максимальный шаг сустава за итерацию, рад. Держит линеаризацию честной.
IK_MAX_STEP = 0.25
#: Шаг центральной конечной разности для якобиана, рад.
IK_FD_STEP = 1e-6

#: Стартовые позы для перезапуска IK, если из основного seed решение не нашлось.
#: Пятизвенная рука в одну и ту же точку приходит несколькими конфигурациями, и
#: спуск из «локтем вниз» не найдёт решения «локтем вверх».
IK_RESTART_SEEDS: Sequence[Mapping[str, float]] = (
    {},                                                              # все нули
    {"shoulder_lift": -0.9, "elbow_flex": 0.9},                      # локоть вверх
    {"shoulder_lift": -1.4, "elbow_flex": 1.4, "wrist_flex": -0.5},  # сложена
    {"shoulder_lift": 0.6, "elbow_flex": -0.6, "wrist_flex": 0.6},   # локоть вниз
    {"shoulder_lift": -0.4, "elbow_flex": 1.2, "wrist_flex": 0.8},
)


# --------------------------------------------------------------------------- #
# однородные преобразования
# --------------------------------------------------------------------------- #

def _rpy_matrix(roll: float, pitch: float, yaw: float) -> np.ndarray:
    """Матрица поворота URDF-соглашения: R = Rz(yaw)·Ry(pitch)·Rx(roll)."""
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return np.array(
        [
            [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
            [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
            [-sp, cp * sr, cp * cr],
        ],
        dtype=float,
    )


def _origin_matrix(xyz: Sequence[float], rpy: Sequence[float]) -> np.ndarray:
    """4x4 из ``<origin xyz=... rpy=.../>``."""
    T = np.eye(4)
    T[:3, :3] = _rpy_matrix(*rpy)
    T[:3, 3] = xyz
    return T


def _skew(axis: np.ndarray) -> np.ndarray:
    """Кососимметричная матрица [a]×: ``[a]× v == a × v``."""
    x, y, z = axis
    return np.array([[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]], dtype=float)


# --------------------------------------------------------------------------- #
# звено цепи
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ChainJoint:
    """Один сустав URDF: постоянный ``origin`` плюс движение вдоль/вокруг оси."""

    name: str
    parent: str
    child: str
    #: Тип URDF: ``fixed`` | ``revolute`` | ``continuous`` | ``prismatic``.
    kind: str
    #: Преобразование parent → сустав (из ``<origin>``), 4x4.
    origin: np.ndarray
    #: Единичная ось движения в системе сустава.
    axis: np.ndarray

    def __post_init__(self) -> None:
        # Родриг: R(θ) = I + sinθ·[a]× + (1−cosθ)·[a]×². Обе постоянные матрицы
        # считаются один раз — иначе каждый вызов FK строил бы 3x3 заново, а
        # численная IK зовёт FK тысячами.
        K = _skew(self.axis)
        object.__setattr__(self, "skew1", K)
        object.__setattr__(self, "skew2", K @ K)

    @property
    def movable(self) -> bool:
        return self.kind != "fixed"

    def transform(self, value: float) -> np.ndarray:
        """Полное преобразование parent → child при значении сустава ``value``."""
        if self.kind == "fixed":
            return self.origin
        T = np.eye(4)
        if self.kind == "prismatic":
            T[:3, 3] = self.axis * value
        else:
            T[:3, :3] += math.sin(value) * self.skew1 + (1.0 - math.cos(value)) * self.skew2
        return self.origin @ T


#: Фолбэк-цепь: снимок ``<origin>``/``<axis>`` из
#: ``deploy/so101_follower.generated.urdf`` (base_link → gripper_frame_link).
#: Нужен по той же причине, что и ``FALLBACK_LIMITS`` в safety/limits.py: шлюз
#: обязан подниматься на macOS даже без сгенерированного URDF. Расхождение с
#: реальной сборкой в этом случае возможно, поэтому загрузка кричит WARNING.
#: Формат: (name, parent, child, kind, xyz, rpy, axis).
FALLBACK_CHAIN: Sequence[
    Tuple[str, str, str, str, Tuple[float, float, float], Tuple[float, float, float], Tuple[float, float, float]]
] = (
    (
        "shoulder_pan", "base_link", "shoulder_link", "revolute",
        (0.0388353, -8.97657e-09, 0.0624), (3.14159, 4.18253e-17, -3.14159), (0.0, 0.0, 1.0),
    ),
    (
        "shoulder_lift", "shoulder_link", "upper_arm_link", "revolute",
        (-0.0303992, -0.0182778, -0.0542), (-1.5708, -1.5708, 0.0), (0.0, 0.0, 1.0),
    ),
    (
        "elbow_flex", "upper_arm_link", "lower_arm_link", "revolute",
        (-0.11257, -0.028, 1.73763e-16), (-3.63608e-16, 8.74301e-16, 1.5708), (0.0, 0.0, 1.0),
    ),
    (
        "wrist_flex", "lower_arm_link", "wrist_link", "revolute",
        (-0.1349, 0.0052, 3.62355e-17), (4.02456e-15, 8.67362e-16, -1.5708), (0.0, 0.0, 1.0),
    ),
    (
        "wrist_roll", "wrist_link", "gripper_link", "revolute",
        (5.55112e-17, -0.0611, 0.0181), (1.5708, 0.0486795, 3.14159), (0.0, 0.0, 1.0),
    ),
    (
        "gripper_frame_joint", "gripper_link", "gripper_frame_link", "fixed",
        (-0.0079, -0.000218121, -0.0981274), (0.0, 3.14159, 0.0), (0.0, 0.0, 1.0),
    ),
)


def _fallback_chain() -> List[ChainJoint]:
    return [
        ChainJoint(
            name=name,
            parent=parent,
            child=child,
            kind=kind,
            origin=_origin_matrix(xyz, rpy),
            axis=np.array(axis, dtype=float),
        )
        for name, parent, child, kind, xyz, rpy, axis in FALLBACK_CHAIN
    ]


# --------------------------------------------------------------------------- #
# разбор URDF
# --------------------------------------------------------------------------- #

def _floats(raw: Optional[str], default: Sequence[float], what: str) -> List[float]:
    if raw is None:
        return list(default)
    parts = raw.split()
    if len(parts) != 3:
        raise ValueError(f"{what}: ожидались три числа, получено {raw!r}")
    try:
        return [float(p) for p in parts]
    except ValueError as exc:
        raise ValueError(f"{what}: нечисловое значение в {raw!r}") from exc


def _read_joint(element: ET.Element, path: Path) -> ChainJoint:
    name = element.get("name") or "<без имени>"
    kind = element.get("type") or ""
    if kind not in {"fixed", "revolute", "continuous", "prismatic"}:
        raise ValueError(f"сустав {name} в {path}: неподдерживаемый тип {kind!r}")

    parent = element.find("parent")
    child = element.find("child")
    if parent is None or child is None:
        raise ValueError(f"у сустава {name} в {path} нет parent/child")
    parent_link, child_link = parent.get("link"), child.get("link")
    if not parent_link or not child_link:
        raise ValueError(f"у сустава {name} в {path} пустой parent/child")

    origin = element.find("origin")
    xyz = _floats(origin.get("xyz") if origin is not None else None, (0.0, 0.0, 0.0), f"origin xyz сустава {name}")
    rpy = _floats(origin.get("rpy") if origin is not None else None, (0.0, 0.0, 0.0), f"origin rpy сустава {name}")

    axis_el = element.find("axis")
    axis = np.array(
        _floats(axis_el.get("xyz") if axis_el is not None else None, (1.0, 0.0, 0.0), f"axis сустава {name}"),
        dtype=float,
    )
    if kind != "fixed":
        norm = float(np.linalg.norm(axis))
        if norm < 1e-12:
            raise ValueError(f"у сустава {name} в {path} нулевая ось вращения")
        axis = axis / norm

    return ChainJoint(
        name=name,
        parent=parent_link,
        child=child_link,
        kind=kind,
        origin=_origin_matrix(xyz, rpy),
        axis=axis,
    )


def parse_urdf_chain(
    path: os.PathLike | str,
    base_link: str = BASE_LINK,
    ee_link: str = EE_LINK,
) -> List[ChainJoint]:
    """Цепь суставов ``base_link → ee_link`` в порядке от базы к схвату.

    Бросает ``ValueError`` на битом XML, неизвестных типах суставов или если
    цепь не собирается — вызывающий сам решает, падать или уходить в фолбэк.
    """
    path = Path(path)
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        raise ValueError(f"не разобрать URDF {path}: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"не прочитать URDF {path}: {exc}") from exc

    root = tree.getroot()
    # ТОЛЬКО прямые потомки <robot>: вложенные <joint> внутри <transmission>
    # и <gazebo> — это ссылки по имени, у них нет ни origin, ни axis.
    by_child: Dict[str, ChainJoint] = {}
    for element in root.findall("joint"):
        joint = _read_joint(element, path)
        if joint.child in by_child:
            raise ValueError(f"у звена {joint.child} в {path} больше одного родителя")
        by_child[joint.child] = joint

    # идём от схвата вверх по родителям — в дереве URDF путь единственный
    chain: List[ChainJoint] = []
    link = ee_link
    while link != base_link:
        joint = by_child.get(link)
        if joint is None:
            raise ValueError(
                f"в {path} нет пути {base_link} → {ee_link}: звено {link} ни к чему не крепится"
            )
        chain.append(joint)
        link = joint.parent
        if len(chain) > len(by_child):  # pragma: no cover - защита от цикла
            raise ValueError(f"в {path} цикл в цепи суставов около {link}")

    if not chain:
        raise ValueError(f"в {path} цепь {base_link} → {ee_link} пуста")
    chain.reverse()
    return chain


# --------------------------------------------------------------------------- #
# модель
# --------------------------------------------------------------------------- #

class Kinematics:
    """FK перемножением преобразований URDF + численная IK по позиции.

    Ориентация схвата НЕ задаётся: у руки пять степеней свободы (плюс захват,
    который в цепи до схвата не участвует вовсе), и требовать шесть уравнений
    от пяти неизвестных значило бы объявлять недостижимым почти всё. Решается
    только позиция: три уравнения, пять неизвестных.
    """

    def __init__(
        self,
        chain: Sequence[ChainJoint],
        limits: JointLimits,
        source: str = "urdf",
    ) -> None:
        self._chain = list(chain)
        self._limits = limits
        self.source = source
        #: Подвижные суставы цепи в порядке от базы к схвату. ``gripper`` сюда
        #: не попадает: он ведёт в отдельную ветку (подвижную губку), а не к
        #: ``gripper_frame_link``, и на положение схвата не влияет.
        self._movable: List[str] = [j.name for j in self._chain if j.movable]
        self._solvable: List[str] = [n for n in self._movable if limits.has(n)]
        #: Для каждого сустава цепи — его столбец в векторе неизвестных IK
        #: (``None`` у неподвижных и у тех, чьих пределов нет: они стоят в нуле).
        index = {name: i for i, name in enumerate(self._solvable)}
        self._columns: List[Optional[int]] = [index.get(j.name) for j in self._chain]
        #: Переиспользуемые буферы ``_fk_batch``, ключ — размер пучка.
        self._buffers: Dict[int, List[np.ndarray]] = {}
        #: Верхняя оценка вылета: положение схвата — сумма повёрнутых сдвигов
        #: origin-ов, поэтому ‖p‖ ≤ Σ‖tᵢ‖ при любых углах. Точка дальше этого
        #: радиуса недостижима заведомо, и гонять по ней спуск незачем.
        self._max_reach = float(sum(np.linalg.norm(j.origin[:3, 3]) for j in self._chain))

    # ------------------------------------------------------------------ #
    # свойства цепи
    # ------------------------------------------------------------------ #

    @property
    def chain(self) -> List[ChainJoint]:
        return list(self._chain)

    @property
    def joint_names(self) -> List[str]:
        """Подвижные суставы цепи base_link → gripper_frame_link."""
        return list(self._movable)

    # ------------------------------------------------------------------ #
    # прямая задача
    # ------------------------------------------------------------------ #

    def transform(self, joints: Mapping[str, float]) -> np.ndarray:
        """Однородное преобразование ``base_link → gripper_frame_link``."""
        T = np.eye(4)
        for joint in self._chain:
            value = float(joints.get(joint.name, 0.0)) if joint.movable else 0.0
            T = T @ joint.transform(value)
        return T

    def forward_kinematics(self, joints: Mapping[str, float]) -> Point:
        """Углы суставов → положение схвата в системе ``base_link``."""
        p = self.transform(joints)[:3, 3]
        return (float(p[0]), float(p[1]), float(p[2]))

    # ------------------------------------------------------------------ #
    # обратная задача
    # ------------------------------------------------------------------ #

    def _fk_batch(self, Q: np.ndarray) -> np.ndarray:
        """Положения схвата для пучка поз: ``(m, n)`` → ``(m, 3)``.

        Считать 2n+1 возмущённых поз по одной — значит платить накладные расходы
        numpy 2n+1 раз вместо одного; на этом численная IK и стоит.

        Подвижные суставы, которых нет в пределах (а значит, и в наборе
        неизвестных IK), держатся в нуле — ровно как в ``transform``.

        Буферы под матрицы переиспользуются между вызовами, поэтому модель
        рассчитана на работу из одного потока. Так она и живёт: у симулятора
        свой экземпляр, и зовут его только из цикла событий шлюза.
        """
        m = Q.shape[0]
        buffers = self._buffers.get(m)
        if buffers is None:
            # каждому подвижному суставу — своя (m,4,4), инициализированная
            # единицей: вращательный сустав переписывает только блок поворота,
            # призматический — только сдвиг, поэтому остальное остаётся верным
            buffers = [np.tile(np.eye(4), (m, 1, 1)) for j in self._chain if j.movable]
            self._buffers[m] = buffers

        eye3 = np.eye(3)
        T: Optional[np.ndarray] = None
        slot = 0
        for joint, column in zip(self._chain, self._columns):
            if not joint.movable:
                T = joint.origin if T is None else T @ joint.origin
                continue
            values = Q[:, column] if column is not None else np.zeros(m)
            block = buffers[slot]
            slot += 1
            if joint.kind == "prismatic":
                block[:, :3, 3] = values[:, None] * joint.axis
            else:
                block[:, :3, :3] = (
                    eye3
                    + np.sin(values)[:, None, None] * joint.skew1
                    + (1.0 - np.cos(values))[:, None, None] * joint.skew2
                )
            step = joint.origin @ block
            T = step if T is None else T @ step
        if T is None or T.ndim == 2:
            # вырожденная цепь без подвижных суставов: одна и та же точка на все
            # строки пучка (сюда SO-101 не попадает, но крутить пучок не на чем)
            point = np.zeros(3) if T is None else T[:3, 3]
            return np.broadcast_to(point, (m, 3))
        return T[:, :3, 3]

    def _probe(self, q: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """Позиция схвата в ``q`` и 3×n якобиан там же — за один пучок FK.

        Строки пучка: сама поза, затем пары ``q ± h·eᵢ``. Якобиан — центральная
        конечная разность по этим парам; аналитики здесь нет намеренно, FK
        остаётся единственным источником геометрии.
        """
        n = q.size
        Q = np.repeat(q[None, :], 2 * n + 1, axis=0)
        rows = np.arange(n)
        Q[1 + 2 * rows, rows] += IK_FD_STEP
        Q[2 + 2 * rows, rows] -= IK_FD_STEP
        P = self._fk_batch(Q)
        J = ((P[1::2] - P[2::2]) / (2.0 * IK_FD_STEP)).T
        return P[0], J

    def _fk_vector(self, q: np.ndarray) -> np.ndarray:
        return np.asarray(self.transform(dict(zip(self._solvable, q)))[:3, 3], dtype=float)

    def _clamp(self, q: np.ndarray) -> np.ndarray:
        return np.array(
            [self._limits.clamp(name, float(v)) for name, v in zip(self._solvable, q)],
            dtype=float,
        )

    def _descend(self, target: np.ndarray, seed: np.ndarray) -> Optional[np.ndarray]:
        """Затухающие наименьшие квадраты из одной стартовой позы.

        Δq = Jᵀ(JJᵀ + λ²I)⁻¹ e — то же, что решение задачи наименьших квадратов
        с регуляризацией: вблизи вырождения шаг не улетает в бесконечность, а
        затухает. Каждый шаг подрезается по пределам суставов, поэтому спуск
        никогда не проходит через физически невозможные позы.
        """
        q = self._clamp(seed)
        eye = np.eye(3)
        best = math.inf
        stalled = 0
        for _ in range(IK_MAX_ITERATIONS):
            position, J = self._probe(q)
            error = target - position
            distance = float(np.linalg.norm(error))
            if distance <= IK_POSITION_TOL:
                return q
            # прогресс считается ОТНОСИТЕЛЬНЫМ: сходящийся DLS режет невязку
            # заметно, а «ползёт на 1e-12 за шаг» — это уже упор в предел или в
            # ближайшую достижимую точку, и крутить дальше незачем
            if distance < best * (1.0 - IK_MIN_PROGRESS):
                best = distance
                stalled = 0
            else:
                stalled += 1
                if stalled >= IK_STALL_ITERATIONS:
                    break
            try:
                dq = J.T @ np.linalg.solve(J @ J.T + (IK_DAMPING ** 2) * eye, error)
            except np.linalg.LinAlgError:  # pragma: no cover - λ>0 не даёт вырождения
                return None
            biggest = float(np.max(np.abs(dq)))
            if biggest > IK_MAX_STEP:
                dq *= IK_MAX_STEP / biggest
            nq = self._clamp(q + dq)
            if float(np.max(np.abs(nq - q))) < 1e-12:
                # упёрлись в пределы или в плато: дальше этот старт не пойдёт
                break
            q = nq
        # честная финальная проверка: цель засчитывается только по факту FK
        if float(np.linalg.norm(target - self._fk_vector(q))) <= IK_POSITION_TOL:
            return q
        return None

    def inverse_kinematics(
        self,
        point: Point,
        seed: Optional[Mapping[str, float]] = None,
    ) -> Optional[Dict[str, float]]:
        """Положение схвата → углы суставов, либо ``None``, если не дотянуться.

        ``seed`` — стартовая поза спуска: если передать текущую позу руки,
        решение окажется ближайшим к ней и джог не будет «щёлкать» позой. При
        неудаче перебираются ``IK_RESTART_SEEDS`` — другие конфигурации локтя.

        ``None`` означает именно «не сошлось за отведённые итерации», а не
        «доехали примерно»: результат проверяется прямой задачей.
        """
        if not self._solvable:
            return None                # решать нечем: подвижных суставов нет
        values = [float(v) for v in point]
        if not all(math.isfinite(v) for v in values):
            return None
        target = np.array(values, dtype=float)
        if float(np.linalg.norm(target)) > self._max_reach:
            return None  # заведомо дальше суммарной длины цепи

        starts: List[Mapping[str, float]] = []
        if seed is not None:
            starts.append(seed)
        starts.extend(IK_RESTART_SEEDS)

        for start in starts:
            q0 = np.array(
                [float(start.get(name, 0.0)) for name in self._solvable],
                dtype=float,
            )
            solution = self._descend(target, q0)
            if solution is not None:
                return {name: float(v) for name, v in zip(self._solvable, solution)}
        return None


# --------------------------------------------------------------------------- #
# загрузка
# --------------------------------------------------------------------------- #

def load_kinematics(path: os.PathLike | str | None, limits: JointLimits) -> Kinematics:
    """Собрать модель из URDF, при любой неудаче — фолбэк-цепь + WARNING."""
    if path is None:
        log.warning(
            "Путь к URDF не задан, кинематика собрана из захардкоженного снимка "
            "цепи (fallback). Проверьте URDF_PATH."
        )
        return Kinematics(_fallback_chain(), limits, source="fallback")

    path = Path(path)
    if not path.exists():
        log.warning(
            "URDF %s не найден — кинематика собрана из захардкоженного снимка "
            "цепи (fallback). Геометрия может разойтись с реальной сборкой.",
            path,
        )
        return Kinematics(_fallback_chain(), limits, source="fallback")

    try:
        chain = parse_urdf_chain(path)
    except ValueError as exc:
        log.warning(
            "URDF %s не разобран (%s) — кинематика собрана из захардкоженного "
            "снимка цепи (fallback).",
            path,
            exc,
        )
        return Kinematics(_fallback_chain(), limits, source="fallback")

    log.info("Кинематика собрана из URDF %s", path)
    return Kinematics(chain, limits, source="urdf")
