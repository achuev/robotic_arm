"""Режим привлечения внимания и мягкая посадка.

Оба движения происходят БЕЗ человека за пультом — их некому остановить
кнопкой, поэтому проверяется в первую очередь, когда они НЕ должны
случаться.
"""

from __future__ import annotations

import pytest

from so101_gateway.app import GatewayService, create_app
from so101_gateway.backends.sim import SimBackend
from so101_gateway.gestures import PARK_POSE
from so101_gateway.safety.limits import load_joint_limits


@pytest.fixture
def limits():
    return load_joint_limits(None)


def make(config, clock, limits) -> GatewayService:
    backend = SimBackend(limits=limits, config=config, clock=clock)
    service = GatewayService(config=config, backend=backend, limits=limits, clock=clock)
    service._synced = True          # обычно ставится первым тактом цикла
    return service


def idle(service, clock, seconds: float, dt: float = 0.5) -> None:
    """Прогнать простой: только проверка привлечения, без движения."""
    steps = int(seconds / dt)
    for _ in range(steps):
        clock.advance(dt)
        service._maybe_attract()


# --------------------------------------------------------------------------- #
# привлечение внимания
# --------------------------------------------------------------------------- #

def test_attract_fires_after_the_configured_idle(config, clock, limits):
    cfg = config.replace(attract_after_sec=60.0, attract_period_sec=300.0)
    service = make(cfg, clock, limits)

    idle(service, clock, 59.0)
    assert service._gesture is None, "сработало раньше срока"

    idle(service, clock, 3.0)
    assert service._gesture is not None, "не сработало после срока"


def test_attract_ends_in_home_not_in_the_raised_pose(config, clock, limits):
    """После жеста рука возвращается домой.

    Иначе следующий посетитель встретит её задранной вверх — и решит, что
    робот сломан или что им уже кто-то управляет.
    """
    cfg = config.replace(attract_after_sec=1.0, attract_period_sec=300.0)
    service = make(cfg, clock, limits)
    idle(service, clock, 2.0)
    assert service._gesture is not None

    last_pose, _hold = service._gesture[-1]
    for joint, value in service._home_pose.items():
        assert last_pose[joint] == pytest.approx(value)


def test_attract_never_interrupts_an_operator(config, clock, limits):
    """Пока ход у человека, рука не шевелится сама."""
    cfg = config.replace(attract_after_sec=1.0, attract_period_sec=1.0)
    service = make(cfg, clock, limits)
    service.manager.hello("a")
    service.manager.enqueue("a")
    assert service.manager.controller == "a"

    idle(service, clock, 60.0)
    assert service._gesture is None


def test_attract_does_not_fire_while_someone_waits(config, clock, limits):
    """Очередь есть — значит стенд не пустует, привлекать некого."""
    cfg = config.replace(attract_after_sec=1.0, attract_period_sec=1.0)
    service = make(cfg, clock, limits)
    service.manager.hello("a")
    service.manager.enqueue("a")
    service.manager.hello("b")
    service.manager.enqueue("b")
    assert service.manager.queue_length == 1

    idle(service, clock, 60.0)
    assert service._gesture is None


def test_attract_is_silent_under_estop(config, clock, limits):
    """При аварийной остановке рука не двигается вообще."""
    cfg = config.replace(attract_after_sec=1.0, attract_period_sec=1.0)
    service = make(cfg, clock, limits)
    service.manager.estop()

    idle(service, clock, 60.0)
    assert service._gesture is None


def test_attract_respects_the_minimum_period(config, clock, limits):
    """Между жестами выдерживается пауза, рука не машет без остановки."""
    cfg = config.replace(attract_after_sec=1.0, attract_period_sec=100.0)
    service = make(cfg, clock, limits)

    idle(service, clock, 2.0)
    assert service._gesture is not None
    service._cancel_gesture()          # как будто жест доиграл

    idle(service, clock, 50.0)
    assert service._gesture is None, "сработало раньше периода"

    idle(service, clock, 60.0)
    assert service._gesture is not None


def test_attract_can_be_switched_off(config, clock, limits):
    cfg = config.replace(attract_enabled=False, attract_after_sec=1.0)
    service = make(cfg, clock, limits)
    idle(service, clock, 600.0)
    assert service._gesture is None


def test_visitor_resets_the_idle_countdown(config, clock, limits):
    """Ушедший посетитель не оставляет за собой почти истёкший отсчёт."""
    cfg = config.replace(attract_after_sec=60.0, attract_period_sec=300.0)
    service = make(cfg, clock, limits)

    idle(service, clock, 55.0)         # почти дозрели
    service.manager.hello("a")
    service.manager.enqueue("a")
    service._maybe_attract()           # оператор появился — отсчёт сброшен
    service.manager.release("a")

    idle(service, clock, 30.0)
    assert service._gesture is None, "отсчёт не начался заново"


# --------------------------------------------------------------------------- #
# мягкая посадка
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_park_moves_the_arm_to_the_park_pose(config, limits):
    """Рука доезжает до позы парковки, а не просто получает цель."""
    import time

    cfg = config.replace(park_on_shutdown=True, park_timeout_sec=30.0)
    backend = SimBackend(limits=limits, config=cfg, clock=time.time)
    service = GatewayService(config=cfg, backend=backend, limits=limits, clock=time.time)

    assert await service.park() is True
    for joint, value in PARK_POSE.items():
        assert service.limiter.q_cmd[joint] == pytest.approx(value, abs=0.03)


@pytest.mark.anyio
async def test_park_gives_up_instead_of_hanging(config, limits, monkeypatch):
    """Недостижимая поза не подвешивает остановку стенда навсегда."""
    import time

    cfg = config.replace(park_on_shutdown=True, park_timeout_sec=0.3)
    backend = SimBackend(limits=limits, config=cfg, clock=time.time)
    service = GatewayService(config=cfg, backend=backend, limits=limits, clock=time.time)
    # Командная точка не двигается — доехать невозможно.
    monkeypatch.setattr(service.limiter, "tick", lambda: dict(service.limiter.q_cmd))

    started = time.time()
    assert await service.park() is False
    assert time.time() - started < 3.0, "park() не уложился в свой таймаут"


@pytest.mark.anyio
async def test_park_cancels_a_running_gesture(config, limits):
    import time

    cfg = config.replace(park_on_shutdown=True, park_timeout_sec=30.0)
    backend = SimBackend(limits=limits, config=cfg, clock=time.time)
    service = GatewayService(config=cfg, backend=backend, limits=limits, clock=time.time)
    service._apply_preset("wave")
    assert service._gesture is not None

    await service.park()
    assert service._gesture is None


def test_shutdown_parks_the_arm(config, limits):
    """Полный цикл: приложение поднялось и погасло — рука в парковке."""
    from fastapi.testclient import TestClient
    import time

    cfg = config.replace(park_on_shutdown=True, park_timeout_sec=30.0)
    backend = SimBackend(limits=limits, config=cfg, clock=time.time)
    app = create_app(backend=backend, config=cfg, clock=time.time)
    with TestClient(app) as client:
        client.get("/api/health")
    service = app.state.service
    for joint, value in PARK_POSE.items():
        assert service.limiter.q_cmd[joint] == pytest.approx(value, abs=0.03)
