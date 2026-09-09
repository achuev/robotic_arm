from __future__ import annotations

import pytest

from so101_gateway.config import Config


class FakeClock:
    """Инъецируемые часы. Время двигается только руками теста."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> float:
        self.t += float(dt)
        return self.t


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def config() -> Config:
    """Умолчания контракта, но с обязательным admin-токеном.

    Мягкая посадка выключена: она ждёт доезда руки до позы парковки, а в
    тестах приложение поднимается и гасится сотни раз — с ней набор идёт
    минуту с лишним вместо двадцати секунд. Проверяется отдельно, в
    test_park.py, где ожидание и есть предмет проверки.
    """
    return Config(admin_token="test-admin-token", park_on_shutdown=False)


@pytest.fixture
def cartesian_config() -> Config:
    """Конфиг с ВКЛЮЧЁННЫМ декартовым режимом.

    На стенде он выключен (`feature_cartesian=False`): прохожему понятнее
    слайдеры суставов. Но код режима жив и должен оставаться проверенным,
    поэтому тесты джоггинга берут эту фикстуру, а не общую.
    """
    return Config(admin_token="test-admin-token", feature_cartesian=True)
