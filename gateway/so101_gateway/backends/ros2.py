"""ROS 2 бэкенд: единственный файл пакета, которому позволено знать про rclpy.

Устройство
----------
Нода живёт в фоновом потоке с собственным исполнителем; FastAPI дёргает методы
из своего потока, поэтому всё разделяемое состояние — под одним замком.

Главное архитектурное правило проекта: у топика команд ровно ОДИН издатель — этот
бэкенд. IK-нода апстрима запускается с ``cmd_topic:=/web/ik_cmd`` и в топик руки
не пишет; её решение подхватывается здесь и возвращается наверх как обычные цели
суставов, то есть проходит через тот же клэмп и ограничитель скорости, что и
ручное управление. Небезопасная команда не может попасть к железу в обход.

Декартов джоггинг идёт через ``/servo_target``, а не через сервис ``go_to_pose``:
сервис исполняет всю траекторию сам и блокирует вызывающего на секунды, что для
джоггинга непригодно.

Положение схвата берётся ИСКЛЮЧИТЕЛЬНО из TF (``robot_state_publisher`` уже поднят
в bringup). Приблизительная модель из ``SimBackend`` здесь не используется намеренно:
её геометрия расходится с URDF (в нулевой позе она даёт x=0.039, z=0.377 против
настоящих x=0.391, z=0.226), и две разные кинематики в одном контуре обратной связи
уводят руку — проверено, схват ехал вниз вместо вверх.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Dict, Mapping, Optional, Tuple

import rclpy
import rclpy.signals
import tf2_ros
from geometry_msgs.msg import PoseStamped
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from sensor_msgs.msg import JointState
from std_msgs.msg import Float64MultiArray

from ..config import Config
from ..safety.limiter import WorkspaceBox
from ..safety.limits import JOINT_ORDER, JointLimits
from ..session.manager import ErrorCode
from .base import JogResult, RobotState
from .sim import HOME_POSE, PRESETS

__all__ = ["Ros2Backend"]

log = logging.getLogger(__name__)

#: Свежесть joint_states, после которой связь считается потерянной.
STALE_AFTER_SEC = 1.0
#: Звено схвата в TF. Положение берём отсюда, а не из собственной модели:
#: IK решает robokin по настоящему URDF, и вторая, приблизительная модель в том
#: же контуре обратной связи уводит руку — проверено, схват ехал вниз вместо
#: вверх. Источник кинематики должен быть один.
DEFAULT_EE_FRAME = "gripper_frame_link"
DEFAULT_BASE_FRAME = "base_link"
#: Сколько ждём решение IK на /web/ik_cmd после посылки /servo_target.
IK_WAIT_SEC = 0.25


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


class _GatewayNode(Node):
    """Тонкая нода: только ввод-вывод, вся логика снаружи."""

    def __init__(self, owner: "Ros2Backend") -> None:
        super().__init__("so101_web_gateway")
        self._owner = owner

        # joint_states публикуется BEST_EFFORT — подписка обязана совпадать,
        # иначе QoS не сматчится и сообщения не придут вообще.
        sensor_qos = QoSProfile(
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            durability=QoSDurabilityPolicy.VOLATILE,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=10,
        )

        self.create_subscription(
            JointState, _env("JOINT_STATES_TOPIC", "/follower/joint_states"),
            self._on_joint_states, sensor_qos,
        )
        self.create_subscription(
            Float64MultiArray, _env("IK_CMD_TOPIC", "/web/ik_cmd"),
            self._on_ik_cmd, 10,
        )
        self._cmd_pub = self.create_publisher(
            Float64MultiArray, _env("CMD_TOPIC", "/follower/forward_controller/commands"), 10,
        )
        self._servo_pub = self.create_publisher(
            PoseStamped, _env("SERVO_TARGET_TOPIC", "/servo_target"), 10,
        )
        self._base_frame = _env("BASE_FRAME", DEFAULT_BASE_FRAME)
        self._ee_frame = _env("EE_FRAME", DEFAULT_EE_FRAME)

        # TF публикует robot_state_publisher, уже поднятый в bringup.
        self._tf_buffer = tf2_ros.Buffer()
        self._tf_listener = tf2_ros.TransformListener(self._tf_buffer, self)

    def lookup_ee(self) -> Optional[Tuple[float, float, float]]:
        """Положение схвата из TF или ``None``, если преобразование не готово."""
        pose = self.lookup_ee_pose()
        return None if pose is None else pose[0]

    def lookup_ee_pose(self):
        """Положение И ориентация схвата из TF."""
        try:
            tf = self._tf_buffer.lookup_transform(
                self._base_frame, self._ee_frame, rclpy.time.Time()
            ).transform
            t, r = tf.translation, tf.rotation
            return (
                (float(t.x), float(t.y), float(t.z)),
                (float(r.x), float(r.y), float(r.z), float(r.w)),
            )
        except Exception:  # noqa: BLE001 — TF ещё не собрался, это норма
            return None

    # --- вход ---------------------------------------------------------- #

    def _on_joint_states(self, msg: JointState) -> None:
        # ЛОВУШКА: порядок в joint_states алфавитный и НЕ совпадает с порядком
        # команд. Сопоставление по индексу совпало бы только на нулях, то есть
        # прошло бы все простые тесты, а на стенде плечо поехало бы вместо
        # локтя. См. docs/ros-integration.md.
        self._owner._ingest_joint_states(dict(zip(msg.name, msg.position)))

    def _on_ik_cmd(self, msg: Float64MultiArray) -> None:
        data = list(msg.data)
        if len(data) < len(JOINT_ORDER):
            return
        self._owner._ingest_ik_solution(
            {name: float(data[i]) for i, name in enumerate(JOINT_ORDER)}
        )

    # --- выход --------------------------------------------------------- #

    def publish_command(self, positions: Mapping[str, float]) -> None:
        msg = Float64MultiArray()
        msg.data = [float(positions[name]) for name in JOINT_ORDER]
        self._cmd_pub.publish(msg)

    def publish_servo_target(self, x: float, y: float, z: float, quat=None) -> None:
        """Цель для IK-ноды.

        Ориентацию берём ТЕКУЩУЮ, а не единичную. У руки пять степеней свободы,
        и решатель не может одновременно попасть в произвольную позицию и в
        произвольную ориентацию — он размазывает невязку по обеим задачам. С
        единичным кватернионом это выражалось грубо: подъём схвата на 16 см по z
        утаскивал его на 15 см по x. Прося ту ориентацию, в которой рука уже
        находится, мы оставляем решателю только позиционную задачу.
        """
        msg = PoseStamped()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = self._base_frame
        msg.pose.position.x = float(x)
        msg.pose.position.y = float(y)
        msg.pose.position.z = float(z)
        if quat is None:
            msg.pose.orientation.w = 1.0
        else:
            qx, qy, qz, qw = quat
            msg.pose.orientation.x = qx
            msg.pose.orientation.y = qy
            msg.pose.orientation.z = qz
            msg.pose.orientation.w = qw
        self._servo_pub.publish(msg)

    def has_ik_services(self) -> bool:
        """Поднята ли IK-нода. От этого зависит features.cartesian."""
        names = {n for n, _ in self.get_service_names_and_types()}
        return "/go_to_pose" in names or "/go_to_joints" in names


class Ros2Backend:
    """Бэкенд поверх ros2_control. Протокол — как у SimBackend."""

    def __init__(
        self, *, limits: JointLimits, config: Config, clock=time.monotonic
    ) -> None:
        self._limits = limits
        self._cfg = config
        self._clock = clock
        self._workspace = WorkspaceBox.from_config(config)
        self._lock = threading.Lock()

        self._joints: Dict[str, float] = {n: 0.0 for n in limits.names}
        self._last_seen: Optional[float] = None
        self._ik_solution: Optional[Dict[str, float]] = None
        self._ik_stamp: float = -1e9
        self._last_ee: Optional[Tuple[float, float, float]] = None

        if not rclpy.ok():
            # СИГНАЛЫ НЕ ОТДАЁМ rclpy. По умолчанию он ставит свой обработчик
            # SIGINT/SIGTERM и по нему гасит контекст. При остановке стенда это
            # происходит РАНЬШЕ, чем uvicorn доходит до нашего shutdown, и
            # мягкая посадка падает на первой же публикации:
            #
            #   RCLError: Failed to publish: publisher's context is invalid
            #
            # Внешне это выглядело как «рука дёрнулась на треть секунды и
            # встала». Сигналы обрабатывает uvicorn, порядок остановки —
            # наш: сначала увести руку, потом гасить ROS.
            rclpy.init(args=None, signal_handler_options=rclpy.signals.SignalHandlerOptions.NO)
        self._node = _GatewayNode(self)
        self._executor = SingleThreadedExecutor()
        self._executor.add_node(self._node)
        self._thread = threading.Thread(
            target=self._spin, name="so101-ros-spin", daemon=True
        )
        self._thread.start()
        log.info("ROS-бэкенд поднят, ждём joint_states")

    def _spin(self) -> None:
        try:
            self._executor.spin()
        except Exception:  # noqa: BLE001 — поток не должен ронять процесс
            log.exception("исполнитель ROS остановлен")

    def close(self) -> None:
        self._executor.shutdown()
        self._node.destroy_node()

    # --- приём из ноды ------------------------------------------------- #

    def _ingest_joint_states(self, by_name: Mapping[str, float]) -> None:
        with self._lock:
            for name in self._limits.names:
                if name in by_name:
                    self._joints[name] = float(by_name[name])
            self._last_seen = self._clock()

    def _ingest_ik_solution(self, positions: Mapping[str, float]) -> None:
        with self._lock:
            self._ik_solution = dict(positions)
            self._ik_stamp = self._clock()

    # --- протокол RobotBackend ----------------------------------------- #

    def is_connected(self) -> bool:
        """Связь есть, только когда пришли И joint_states, И преобразование TF.

        Без TF положение схвата неизвестно, а подставлять его из приблизительной
        модели нельзя: у неё другая геометрия, и UI нарисовал бы схват не там.
        Честнее подержать 503 лишнюю секунду на старте.
        """
        with self._lock:
            seen = self._last_seen
            has_ee = self._last_ee is not None
        fresh = seen is not None and (self._clock() - seen) < STALE_AFTER_SEC
        return fresh and has_ee

    def get_state(self) -> RobotState:
        with self._lock:
            joints = dict(self._joints)
        ee = self._node.lookup_ee()
        with self._lock:
            if ee is not None:
                self._last_ee = ee
            else:
                ee = self._last_ee
        # До первого TF отдаём нули и держим connected=False: пусть UI покажет
        # «подключаемся», а не схват в выдуманной точке.
        x, y, z = ee if ee is not None else (0.0, 0.0, 0.0)
        return {
            "joints": joints,
            "ee": {"x": x, "y": y, "z": z},
            "connected": self.is_connected(),
        }

    def send_joint_command(self, positions: Mapping[str, float]) -> None:
        with self._lock:
            merged = dict(self._joints)
        for name, value in positions.items():
            if name in merged:
                merged[name] = self._limits.clamp(name, float(value))
        self._node.publish_command(merged)

    def go_home(self) -> None:
        self.send_joint_command(HOME_POSE)

    def preset(self, name: str) -> Optional[Dict[str, float]]:
        pose = PRESETS.get(name)
        if pose is None:
            return None
        return {n: self._limits.clamp(n, v) for n, v in pose.items()}

    # --- декартов джоггинг --------------------------------------------- #

    def jog_ee(
        self,
        axis: str,
        delta: object,
        from_joints: Optional[Mapping[str, float]] = None,
    ) -> JogResult:
        """Сместить схват по оси на ``delta`` метров через IK-ноду апстрима.

        Поведение намеренно совпадает с ``SimBackend``: точка подрезается в
        рабочую зону и движение ВСЁ РАВНО исполняется, а ``out_of_range``
        приходит как пометка. Иначе на один и тот же запрос два бэкенда
        отвечали бы по-разному, и UI пришлось бы знать, какой из них внизу.

        Порядок проверок задан контрактом: сначала границы зоны, только потом
        IK. Точка вне зоны не должна получать ``ik_failed``.
        """
        if axis not in ("x", "y", "z"):
            return JogResult(ok=False, error=ErrorCode.BAD_MESSAGE)
        try:
            step = float(delta)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return JogResult(ok=False, error=ErrorCode.BAD_MESSAGE, axis=axis)

        base = dict(from_joints) if from_joints is not None else self.get_state()["joints"]
        pose = self._node.lookup_ee_pose()
        ee = None if pose is None else pose[0]
        if ee is None:
            # Без TF джоггинг делать нельзя: своя модель разойдётся с моделью
            # решателя, и рука поедет не туда.
            return JogResult(ok=False, error=ErrorCode.IK_FAILED, axis=axis)
        x, y, z = ee
        shift = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}[axis]
        target = (x + shift[0] * step, y + shift[1] * step, z + shift[2] * step)

        (cx, cy, cz), out_of_zone = self._workspace.clamp(*target)

        # Сообщать надо ту ось, которая ДЕЙСТВИТЕЛЬНО упёрлась, а не ту, по
        # которой джогали. Рука в нулевой позе стоит на x=0.391, то есть вне
        # коробки; без этой поправки джог по z жаловался бы на z, и человек
        # искал бы предел не там, где он есть.
        violated = axis
        for name, before, after in (
            ("x", target[0], cx), ("y", target[1], cy), ("z", target[2], cz)
        ):
            if abs(before - after) > 1e-9:
                violated = name
                break

        with self._lock:
            before = self._ik_stamp
        self._node.publish_servo_target(cx, cy, cz, quat=pose[1])

        # Решение прилетит обратно на /web/ik_cmd. Ждём именно СВЕЖЕЕ: старое
        # осталось бы от предыдущего джога и увело бы руку не туда.
        deadline = self._clock() + IK_WAIT_SEC
        while self._clock() < deadline:
            with self._lock:
                fresh = self._ik_stamp > before and self._ik_solution is not None
                solution = dict(self._ik_solution) if fresh else None
            if solution is not None:
                positions = {n: float(base.get(n, 0.0)) for n in JOINT_ORDER}
                positions.update(solution)
                return JogResult(
                    ok=True,
                    positions=positions,
                    error=ErrorCode.OUT_OF_RANGE if out_of_zone else None,
                    axis=violated if out_of_zone else axis,
                    point=(cx, cy, cz),
                )
            time.sleep(0.005)

        # Молчание решателя трактуем как несходимость, но точку вне зоны
        # по-прежнему помечаем именно как выход за зону.
        return JogResult(
            ok=False,
            error=ErrorCode.OUT_OF_RANGE if out_of_zone else ErrorCode.IK_FAILED,
            axis=violated if out_of_zone else axis,
            point=(cx, cy, cz),
        )

    def has_cartesian(self) -> bool:
        """Есть ли реально поднятая IK-нода (для features.cartesian)."""
        try:
            return self._node.has_ik_services()
        except Exception:  # noqa: BLE001
            return False
