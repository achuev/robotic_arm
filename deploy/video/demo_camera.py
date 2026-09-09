#!/usr/bin/env python3
"""Источник кадров для демо-стенда SO-101.

Один ROS-узел, три режима — переключаются параметром `source`:

  source:=""              синтетический поток (камеры на стенде ещё нет)
  source:=/media/loop.mp4 зацикленный видеофайл
  source:=/dev/video0     живая камера через OpenCV (или просто `0`)

Публикует `sensor_msgs/Image` в тот же топик, который дала бы настоящая камера
апстрима (`cameras.launch.py` + `config/cameras/so101_cameras.yaml`): узел
`cam_overhead` в пространстве имён `static_camera` отдаёт `/static_camera/image_raw`.
Поэтому переезд на железную камеру не трогает ни nginx, ни `web_video_server`,
ни фронтенд — меняется только то, кто наполняет топик.

Разрешение и частота взяты оттуда же (640×480 @ 30 fps, см. `so101_v4l2_cam.yaml`).

Зачем свой узел, а не `gscam`/`v4l2_camera` из апстрима: их пакеты намеренно
не ставятся в образ (`ROSDEP_SKIP_KEYS` в `deploy/Dockerfile.ros` — они тянут
несколько ГБ зависимостей). OpenCV и `cv_bridge` в образе уже есть, и их
хватает на все три режима.

Живой оверлей с углами суставов читает `/follower/joint_states` СТРОГО ПО ИМЕНИ:
порядок в `joint_states` алфавитный и не совпадает с порядком команд
(см. docs/ros-integration.md). На синтетическом фоне это ещё и полезно:
посетитель видит, что картинка реагирует на его движения, а не крутится по кругу.
"""

from __future__ import annotations

import os
import time

import cv2
import numpy as np
import rclpy
from cv_bridge import CvBridge
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from sensor_msgs.msg import CameraInfo, Image, JointState

# Порядок показа в оверлее — как в команде (`follower_controllers.yaml`),
# а не как в `joint_states`. Значения берутся по имени, не по индексу.
JOINT_ORDER = [
    "shoulder_pan",
    "shoulder_lift",
    "elbow_flex",
    "wrist_flex",
    "wrist_roll",
    "gripper",
]

# Пределы из URDF (docs/ros-integration.md) — только для длины полоски.
JOINT_LIMITS = {
    "shoulder_pan": (-1.9199, 1.9199),
    "shoulder_lift": (-1.7453, 1.7453),
    "elbow_flex": (-1.6900, 1.6900),
    "wrist_flex": (-1.6581, 1.6581),
    "wrist_roll": (-2.7439, 2.8412),
    "gripper": (-0.1745, 1.7453),
}

FONT = cv2.FONT_HERSHEY_SIMPLEX  # кириллицу putText не умеет — подписи латиницей


class DemoCamera(Node):
    def __init__(self) -> None:
        super().__init__("cam_overhead")

        self.declare_parameter("source", "")
        self.declare_parameter("topic", "image_raw")
        self.declare_parameter("frame_id", "cam_overhead")
        self.declare_parameter("width", 640)
        self.declare_parameter("height", 480)
        self.declare_parameter("fps", 15.0)
        self.declare_parameter("overlay", True)
        self.declare_parameter("joint_states_topic", "/follower/joint_states")

        self.source = str(self.get_parameter("source").value)
        self.width = int(self.get_parameter("width").value)
        self.height = int(self.get_parameter("height").value)
        self.fps = float(self.get_parameter("fps").value)
        self.frame_id = str(self.get_parameter("frame_id").value)
        self.overlay = bool(self.get_parameter("overlay").value)

        self.bridge = CvBridge()
        self.frame_no = 0
        self.joints: dict[str, float] = {}
        self.cap: cv2.VideoCapture | None = None

        # RELIABLE/VOLATILE: image_transport в web_video_server подписывается
        # профилем по умолчанию (RELIABLE). BEST_EFFORT здесь был бы
        # несовместим, и поток молча остался бы пустым.
        qos = QoSProfile(
            reliability=QoSReliabilityPolicy.RELIABLE,
            durability=QoSDurabilityPolicy.VOLATILE,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=1,
        )
        topic = str(self.get_parameter("topic").value)
        self.pub = self.create_publisher(Image, topic, qos)
        self.pub_info = self.create_publisher(CameraInfo, "camera_info", qos)

        self.create_subscription(
            JointState,
            str(self.get_parameter("joint_states_topic").value),
            self._on_joint_states,
            10,
        )

        if self.source:
            self._open_source()

        self.get_logger().info(
            f"demo_camera: source={self.source or '<synthetic>'} "
            f"topic={self.pub.topic_name} {self.width}x{self.height}@{self.fps:g}"
        )
        self.create_timer(1.0 / max(self.fps, 1.0), self._tick)

    # ------------------------------------------------------------ источники

    def _open_source(self) -> None:
        """Открывает видеофайл или устройство. Ошибка не фатальна: узел
        остаётся жив и отдаёт синтетический кадр с надписью, иначе стенд
        терял бы всё видео из-за выдернутого USB."""
        # Живая камера открывается ЯВНО через CAP_V4L2. Без указания бэкенда
        # OpenCV пробует ffmpeg/gstreamer, и `/dev/so101_camera` (симлинк из
        # udev-правила) он не откроет — а сообщение об ошибке будет про
        # «не тот формат файла», что уводит расследование не туда.
        is_device = self.source.isdigit() or self.source.startswith("/dev/")
        src: str | int = int(self.source) if self.source.isdigit() else self.source

        cap = cv2.VideoCapture(src, cv2.CAP_V4L2) if is_device else cv2.VideoCapture(src)
        if not cap.isOpened():
            self.get_logger().error(f"не открылся источник {self.source!r} — синтетический кадр")
            self.cap = None
            return
        if is_device:  # живая камера: навязываем формат апстрима
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            cap.set(cv2.CAP_PROP_FPS, self.fps)
        self.cap = cap

    def _read_source(self) -> np.ndarray | None:
        if self.cap is None:
            return None
        ok, frame = self.cap.read()
        if not ok:  # конец файла — перематываем в начало (зацикливание)
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self.cap.read()
        if not ok:
            return None
        if frame.shape[1] != self.width or frame.shape[0] != self.height:
            frame = cv2.resize(frame, (self.width, self.height), interpolation=cv2.INTER_AREA)
        return frame

    # ------------------------------------------------------- синтетический кадр

    def _synthetic(self) -> np.ndarray:
        w, h = self.width, self.height
        t = self.frame_no / max(self.fps, 1.0)

        # Фон-градиент считается один раз, дальше только копируется.
        if not hasattr(self, "_bg"):
            ramp = np.linspace(0, 1, h, dtype=np.float32)[:, None]
            bg = np.zeros((h, w, 3), dtype=np.float32)
            bg[:, :, 0] = 40 + 25 * ramp[:, 0][:, None].repeat(w, 1)  # B
            bg[:, :, 1] = 26 + 14 * ramp[:, 0][:, None].repeat(w, 1)  # G
            bg[:, :, 2] = 22 + 10 * ramp[:, 0][:, None].repeat(w, 1)  # R
            self._bg = bg.astype(np.uint8)
        frame = self._bg.copy()

        # Сетка «стола» — чтобы движение было заметно глазом.
        step = 48
        shift = int((t * 24) % step)
        for x in range(-step, w + step, step):
            cv2.line(frame, (x + shift, 0), (x + shift, h), (58, 44, 36), 1)
        for y in range(0, h, step):
            cv2.line(frame, (0, y), (w, y), (58, 44, 36), 1)

        # Бегущая полоса развёртки: доказывает, что кадры новые, а не один
        # застывший JPEG (в MJPEG это иначе неотличимо).
        sweep = int((t * 90) % w)
        cv2.line(frame, (sweep, 0), (sweep, h), (120, 200, 255), 2)

        cv2.putText(frame, "SO-101", (24, 62), FONT, 1.6, (235, 235, 235), 3, cv2.LINE_AA)
        cv2.putText(
            frame, "synthetic feed - no camera on the stand yet",
            (24, 94), FONT, 0.5, (150, 190, 220), 1, cv2.LINE_AA,
        )
        return frame

    # ------------------------------------------------------------- оверлей

    def _on_joint_states(self, msg: JointState) -> None:
        # ТОЛЬКО по имени: в joint_states порядок алфавитный и не совпадает
        # с порядком команд (docs/ros-integration.md).
        for name, pos in zip(msg.name, msg.position):
            self.joints[name] = float(pos)

    def _draw_overlay(self, frame: np.ndarray) -> None:
        h = self.height
        y = h - 12 - 20 * len(JOINT_ORDER)
        cv2.rectangle(frame, (12, y - 26), (300, h - 8), (24, 18, 14), -1)
        live = "LIVE" if self.joints else "waiting for /follower/joint_states"
        cv2.putText(frame, live, (22, y - 8), FONT, 0.45, (120, 220, 140), 1, cv2.LINE_AA)

        for name in JOINT_ORDER:
            value = self.joints.get(name)
            lo, hi = JOINT_LIMITS[name]
            cv2.putText(frame, name[:13], (22, y + 13), FONT, 0.4, (200, 200, 200), 1, cv2.LINE_AA)
            x0, bar_w = 150, 130
            cv2.rectangle(frame, (x0, y + 3), (x0 + bar_w, y + 13), (60, 50, 44), -1)
            if value is not None:
                frac = (value - lo) / (hi - lo) if hi > lo else 0.0
                frac = min(max(frac, 0.0), 1.0)
                cv2.rectangle(
                    frame, (x0, y + 3), (x0 + int(bar_w * frac), y + 13), (90, 200, 255), -1
                )
                cv2.putText(
                    frame, f"{value:+.2f}", (x0 + bar_w + 6, y + 13),
                    FONT, 0.4, (200, 200, 200), 1, cv2.LINE_AA,
                )
            y += 20

        stamp = time.strftime("%H:%M:%S")
        cv2.putText(
            frame, f"{stamp}  #{self.frame_no}", (self.width - 190, self.height - 16),
            FONT, 0.5, (220, 220, 220), 1, cv2.LINE_AA,
        )

    # ---------------------------------------------------------------- такт

    def _tick(self) -> None:
        frame = self._read_source()
        if frame is None:
            frame = self._synthetic()
        if self.overlay:
            self._draw_overlay(frame)

        now = self.get_clock().now().to_msg()
        msg = self.bridge.cv2_to_imgmsg(frame, encoding="bgr8")
        msg.header.stamp = now
        msg.header.frame_id = self.frame_id
        self.pub.publish(msg)

        info = CameraInfo()
        info.header = msg.header
        info.width, info.height = self.width, self.height
        self.pub_info.publish(info)

        self.frame_no += 1


def main() -> None:
    rclpy.init()
    node = DemoCamera()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node.cap is not None:
            node.cap.release()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    # Внутри контейнера OpenCV не нужен ни X11, ни GUI-бэкенд.
    os.environ.setdefault("OPENCV_VIDEOIO_PRIORITY_INTEL_MFX", "0")
    main()
