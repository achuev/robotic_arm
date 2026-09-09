"""Поведение шлюза в первые мгновения после старта.

Лимитер строится из ``backend.get_state()`` ещё до того, как ROS-бэкенд получил
хоть один ``joint_states``, — и в этот момент бэкенд честно отдаёт нули. Если
начать публиковать сразу, то перезапуск шлюза на стенде увёл бы руку из текущего
положения в нули. На mock это незаметно (там рука и правда в нулях), поэтому
свойство закрывается отдельным тестом с бэкендом, который сначала молчит.
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from so101_gateway.app import create_app
from so101_gateway.safety.limits import JOINT_ORDER

ADMIN = "test-admin-token"

#: Поза, в которой «застали» руку: заведомо не нули и не home.
FOUND_AT = {
    "shoulder_pan": 0.7,
    "shoulder_lift": -0.4,
    "elbow_flex": 0.5,
    "wrist_flex": 0.2,
    "wrist_roll": -0.3,
    "gripper": 0.1,
}


class LateBackend:
    """Бэкенд, который узнаёт положение руки не сразу.

    Повторяет поведение ROS-бэкенда: до первого ``joint_states``
    ``is_connected()`` ложно, а ``get_state()`` отдаёт нули.
    """

    def __init__(self) -> None:
        self.connected = False
        self.published: list[dict] = []

    def is_connected(self) -> bool:
        return self.connected

    def get_state(self):
        joints = dict(FOUND_AT) if self.connected else {n: 0.0 for n in JOINT_ORDER}
        return {"joints": joints, "ee": {"x": 0.0, "y": 0.0, "z": 0.0},
                "connected": self.connected}

    def send_joint_command(self, positions) -> None:
        self.published.append(dict(positions))

    def go_home(self) -> None: ...


def _config(config):
    return config.replace(admin_token=ADMIN, cmd_rate_hz=50.0, state_rate_hz=25.0,
                          handover_home=0.2)


def test_nothing_is_published_until_the_arm_position_is_known(config):
    """Пока обратной связи нет, шлюз не командует НИЧЕГО."""
    backend = LateBackend()
    with TestClient(create_app(config=_config(config), backend=backend)):
        time.sleep(0.3)
        assert backend.published == [], (
            "шлюз командовал рукой, не зная, где она находится"
        )


def test_first_command_matches_where_the_arm_actually_is(config):
    """Узнав положение, шлюз продолжает ОТТУДА, а не из нулей.

    Это и есть защита от рывка при перезапуске: на стенде рука может стоять
    где угодно, и первая же команда обязана совпасть с её фактической позой.
    """
    backend = LateBackend()
    with TestClient(create_app(config=_config(config), backend=backend)):
        time.sleep(0.15)
        backend.connected = True
        time.sleep(0.3)

    assert backend.published, "после появления связи шлюз обязан начать командовать"
    first = backend.published[0]
    for name, value in FOUND_AT.items():
        assert abs(first[name] - value) < 0.05, (
            f"{name}: первая команда {first[name]:+.3f} вместо фактических {value:+.3f}"
        )


def test_arm_is_sent_home_once_position_is_known(config):
    """Стенд встречает первого посетителя в домашней позе.

    Уход в home идёт через ограничитель скорости, поэтому рука ползёт к цели,
    а не прыгает: проверяем, что команды меняются постепенно и движутся в
    сторону home по тому суставу, где разница наибольшая.
    """
    backend = LateBackend()
    with TestClient(create_app(config=_config(config), backend=backend)):
        time.sleep(0.1)
        backend.connected = True
        time.sleep(0.6)

    assert len(backend.published) > 5
    start = backend.published[0]["shoulder_pan"]
    end = backend.published[-1]["shoulder_pan"]
    assert start > end, "рука не поехала в сторону home (shoulder_pan=0)"
    assert end >= 0.0, "проехали мимо home"

    # Ни одного скачка быстрее лимита скорости.
    cfg = _config(config)
    max_step = cfg.max_vel_rad_s / cfg.cmd_rate_hz * 3.0   # запас на дрожь таймера
    for a, b in zip(backend.published, backend.published[1:]):
        assert abs(b["shoulder_pan"] - a["shoulder_pan"]) <= max_step
