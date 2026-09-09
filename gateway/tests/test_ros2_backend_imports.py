"""Проверка ROS-бэкенда на машине БЕЗ ROS.

`backends/ros2.py` — единственный файл, которому позволено импортировать rclpy,
и поэтому единственный, который обычные тесты не трогают вовсе. Опечатка в имени
импорта пролежала бы там до самого стенда. Здесь мы подсовываем заглушки вместо
ROS-модулей и импортируем файл по-настоящему: всё, что он берёт ИЗ НАШЕГО пакета,
проверяется всерьёз.
"""

from __future__ import annotations

import sys
import types

import pytest


def _stub(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


@pytest.fixture
def ros_stubs(monkeypatch):
    class _Node:
        def __init__(self, *a, **kw): ...
        def create_subscription(self, *a, **kw): ...
        def create_publisher(self, *a, **kw): ...
        def get_clock(self): ...
        def get_service_names_and_types(self): return []
        def destroy_node(self): ...

    class _Buffer:
        def __init__(self, *a, **kw): ...
        def lookup_transform(self, *a, **kw): raise RuntimeError("нет TF")

    class _Listener:
        def __init__(self, *a, **kw): ...

    class _Executor:
        def __init__(self, *a, **kw): ...
        def add_node(self, *a, **kw): ...
        def spin(self): ...
        def shutdown(self): ...

    class _Msg:
        def __init__(self, *a, **kw): ...

    mods = {
        "rclpy": _stub(
            "rclpy",
            ok=lambda: True,
            init=lambda **kw: None,
            time=types.SimpleNamespace(Time=lambda *a, **kw: object()),
            signals=types.SimpleNamespace(
                SignalHandlerOptions=types.SimpleNamespace(NO=object())
            ),
        ),
        # rclpy.signals: бэкенд отключает через него перехват SIGTERM, иначе
        # rclpy гасит свой контекст раньше нашей мягкой посадки.
        "rclpy.signals": _stub(
            "rclpy.signals",
            SignalHandlerOptions=types.SimpleNamespace(NO=object()),
        ),
        "rclpy.node": _stub("rclpy.node", Node=_Node),
        "rclpy.executors": _stub("rclpy.executors", SingleThreadedExecutor=_Executor),
        "rclpy.qos": _stub(
            "rclpy.qos",
            QoSProfile=lambda **kw: object(),
            QoSReliabilityPolicy=types.SimpleNamespace(BEST_EFFORT=1),
            QoSDurabilityPolicy=types.SimpleNamespace(VOLATILE=1),
            QoSHistoryPolicy=types.SimpleNamespace(KEEP_LAST=1),
        ),
        "sensor_msgs": _stub("sensor_msgs"),
        "sensor_msgs.msg": _stub("sensor_msgs.msg", JointState=_Msg),
        "std_msgs": _stub("std_msgs"),
        "std_msgs.msg": _stub("std_msgs.msg", Float64MultiArray=_Msg),
        "geometry_msgs": _stub("geometry_msgs"),
        "geometry_msgs.msg": _stub("geometry_msgs.msg", PoseStamped=_Msg),
        "tf2_ros": _stub("tf2_ros", Buffer=_Buffer, TransformListener=_Listener),
    }
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.delitem(sys.modules, "so101_gateway.backends.ros2", raising=False)
    yield


def test_ros2_backend_imports_cleanly(ros_stubs):
    """Все имена, которые бэкенд берёт из нашего пакета, существуют."""
    import importlib

    mod = importlib.import_module("so101_gateway.backends.ros2")
    assert hasattr(mod, "Ros2Backend")


def test_ros2_backend_satisfies_protocol(ros_stubs):
    """Ros2Backend подходит под тот же протокол, что и SimBackend."""
    import importlib

    from so101_gateway.backends.base import RobotBackend

    mod = importlib.import_module("so101_gateway.backends.ros2")
    for name in ("get_state", "send_joint_command", "go_home", "is_connected"):
        assert callable(getattr(mod.Ros2Backend, name)), name
    assert callable(getattr(mod.Ros2Backend, "jog_ee"))
    assert callable(getattr(mod.Ros2Backend, "preset"))
    assert isinstance(RobotBackend, type)


def test_command_order_is_the_controller_order(ros_stubs):
    """Команды идут в порядке контроллера, а не в алфавитном.

    Порядок в /follower/joint_states алфавитный и НЕ совпадает с порядком
    команд; см. docs/ros-integration.md. Публикация обязана использовать
    JOINT_ORDER, иначе плечо поедет вместо локтя.
    """
    from so101_gateway.safety.limits import JOINT_ORDER

    assert list(JOINT_ORDER) == [
        "shoulder_pan",
        "shoulder_lift",
        "elbow_flex",
        "wrist_flex",
        "wrist_roll",
        "gripper",
    ]
    assert list(JOINT_ORDER) != sorted(JOINT_ORDER), (
        "если порядок вдруг стал алфавитным, ловушка из docs/ros-integration.md "
        "перестала быть ловушкой — перепроверь публикацию команд"
    )
