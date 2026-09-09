"""Единственное место, где читается окружение.

Все параметры шлюза приходят из переменных окружения. Модуль намеренно не
импортирует ничего тяжёлого (и уж точно не rclpy), чтобы его можно было
использовать и в тестах, и в ROS-ноде.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Tuple

__all__ = ["Config", "load_config", "ConfigError", "default_urdf_path"]

#: Имя файла, который кладёт сборка (см. deploy/).
GENERATED_URDF_NAME = "so101_follower.generated.urdf"


def default_urdf_path() -> str:
    """Путь к сгенерированному URDF.

    Файл лежит в ``deploy/`` в корне репозитория, а шлюз запускают то из
    ``gateway/``, то из корня, то из контейнера. Поднимаемся от пакета вверх,
    пока не найдём каталог ``deploy``; если не нашли — отдаём относительный
    путь, а ``load_joint_limits`` честно уйдёт в фолбэк с предупреждением.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "deploy"
        if candidate.is_dir():
            return str(candidate / GENERATED_URDF_NAME)
    return os.path.join("deploy", GENERATED_URDF_NAME)


class ConfigError(RuntimeError):
    """Некорректная или отсутствующая обязательная переменная окружения."""


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:  # pragma: no cover - защита от опечаток в .env
        raise ConfigError(f"{name}={raw!r} не является числом") from exc


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover
        raise ConfigError(f"{name}={raw!r} не является целым числом") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw


def _env_range(name: str, default: Tuple[float, float]) -> Tuple[float, float]:
    """Диапазон в виде "min,max"."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    parts = raw.split(",")
    if len(parts) != 2:
        raise ConfigError(f"{name}={raw!r} должен быть в формате 'min,max'")
    try:
        lo, hi = float(parts[0]), float(parts[1])
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} содержит нечисловые границы") from exc
    if lo > hi:
        raise ConfigError(f"{name}={raw!r}: min больше max")
    return (lo, hi)


@dataclass(frozen=True)
class Config:
    """Снимок настроек шлюза."""

    # --- очередь и ход ---
    control_duration: float = 90.0
    idle_timeout: float = 20.0
    reconnect_grace: float = 10.0
    max_queue: int = 20
    cooldown: float = 30.0
    handover_home: float = 2.0

    # --- частоты ---
    cmd_rate_hz: float = 50.0
    state_rate_hz: float = 10.0
    max_msg_per_sec: float = 30.0

    # --- сеть ---
    port: int = 8080
    host: str = "0.0.0.0"

    # --- безопасность ---
    max_vel_rad_s: float = 1.0
    #: Watchdog следит за ЖИВОСТЬЮ соединения: нет ни одного сообщения
    #: (ни команды, ни ping) столько секунд — движение замирает.
    watchdog_timeout: float = 3.0
    admin_token: str = ""

    # --- рабочая зона (декартов режим) ---
    # Полный вылет схвата по URDF-кинематике (замер по сетке поз, фрейм
    # base_link → gripper_frame_link): x −0.33…+0.47, y ±0.40, z −0.20…+0.49,
    # максимальный горизонтальный вылет 0.47 м. Зона демо — НЕ полный вылет:
    #  * x снизу 0.10 — ближе рука начинает складываться на собственное
    #    основание, сверху 0.42 при вылете 0.47 оставляет запас до вытянутой
    #    «в струну» позы, где якобиан вырождается и IK перестаёт сходиться;
    #  * y ±0.30 при вылете ±0.40 — тот же запас по бокам;
    #  * z снизу 0.05 — рука не утыкается в стол; отрицательные z физически
    #    достижимы, но это «под стол». Сверху 0.45 при пределе 0.49.
    # Отрицательные x в зону не входят намеренно: за спину рука на демо-стенде
    # уходить не должна. Домашняя поза (+0.293, 0.000, +0.206) внутри с запасом.
    # Рабочая зона схвата. Не полный вылет руки, а безопасная коробка для демо.
    #
    # Полный вылет замерен по 144 позам на живой системе: x −0.329…+0.469,
    # y ±0.404, z −0.204…+0.493, максимум 0.469 м по горизонтали.
    #
    # Границы подобраны замером доли решаемых точек, а не на глаз. Углы любой
    # прямоугольной коробки недостижимы (прямоугольник в сферу не вписать), и
    # широкая зона набирает их слишком много: x .10-.42 / y ±.30 / z .05-.45
    # даёт лишь 77.8 % решаемых точек, то есть каждый четвёртый джог упирался бы
    # в необъяснимое «рука не дотянется». Текущая коробка даёт 98.9 % при объёме
    # 25.8 л (38 × 40 × 28 см) — для демо этого с избытком.
    #
    # Домашняя поза (0.293, 0.000, 0.206) внутри с запасом по всем осям.
    workspace_x: Tuple[float, float] = (0.15, 0.38)
    workspace_y: Tuple[float, float] = (-0.20, 0.20)
    workspace_z: Tuple[float, float] = (0.08, 0.36)
    ee_jog_step_m: float = 0.01

    # --- прочее ---
    urdf_path: str = field(default_factory=default_urdf_path)
    video_url: str = "/video/stream"
    #: Камера в сайте. Выключена: на стенде картинку показывает отдельный
    #: экран рядом с рукой, а посетителю в сайте её заменяет 3D-модель —
    #: она рисуется по тем же joint_states и стоит почти ничего.
    #:
    #: Дело не в экономии ради экономии. MJPEG отдаётся КАЖДОМУ смотрящему
    #: своим потоком, 2.9 Мбит/с на человека, и все они уходят через
    #: исходящий канал зала. Если этот канал — сотовый модем, видео съедает
    #: его целиком и роняет то, ради чего стенд существует: управление.
    #: Включается FEATURE_VIDEO=1, но перед этим стоит прочитать
    #: docs/public-access.md.
    feature_video: bool = False
    #: Декартов режим («точка»): джоггинг схвата по x/y/z через IK.
    #: Выключен: на демо-стенде он оказался лишним — прохожему понятнее
    #: слайдеры суставов, а IK тянет за собой отдельный контейнер и
    #: собственный класс отказов (ik_failed, out_of_range по чужой оси).
    #: Включается FEATURE_CARTESIAN=1 вместе с сервисом `ik` в compose.
    feature_cartesian: bool = False

    # --- режим привлечения внимания ---
    #: Стоящий манипулятор выглядит выключенным. Если никого нет столько
    #: секунд — рука сама делает жест. Ноль/False отключает.
    attract_enabled: bool = True
    attract_after_sec: float = 120.0
    #: Не чаще, чем раз в столько секунд, даже если стенд пустует часами.
    attract_period_sec: float = 240.0

    # --- мягкая посадка ---
    #: Перед остановкой шлюза увести руку в низкую позу, чтобы она не падала
    #: с высоты, когда пропадёт питание приводов.
    park_on_shutdown: bool = True
    park_timeout_sec: float = 8.0
    backend: str = "sim"
    protocol: int = 1

    @property
    def turn_slot_sec(self) -> float:
        """Сколько «стоит» один ход в очереди: работа + передача руки."""
        return self.control_duration + self.handover_home

    def replace(self, **kwargs) -> "Config":
        """Копия с изменёнными полями (удобно в тестах)."""
        known = {f.name for f in fields(self)}
        unknown = set(kwargs) - known
        if unknown:
            raise ConfigError(f"неизвестные поля конфигурации: {sorted(unknown)}")
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        data.update(kwargs)
        return Config(**data)


def load_config(*, require_admin_token: bool = True) -> Config:
    """Собрать конфигурацию из переменных окружения.

    ADMIN_TOKEN обязателен и умолчания не имеет: шлюз без него запускать нельзя,
    иначе админские эндпоинты окажутся открыты пустым токеном.
    """
    admin_token = os.environ.get("ADMIN_TOKEN", "")
    if require_admin_token and not admin_token:
        raise ConfigError(
            "ADMIN_TOKEN не задан. Задайте переменную окружения ADMIN_TOKEN "
            "перед запуском шлюза (умолчания у неё нет по соображениям безопасности)."
        )

    return Config(
        control_duration=_env_float("CONTROL_DURATION", 90.0),
        idle_timeout=_env_float("IDLE_TIMEOUT", 20.0),
        reconnect_grace=_env_float("RECONNECT_GRACE", 10.0),
        max_queue=_env_int("MAX_QUEUE", 20),
        cooldown=_env_float("COOLDOWN", 30.0),
        handover_home=_env_float("HANDOVER_HOME", 2.0),
        cmd_rate_hz=_env_float("CMD_RATE_HZ", 50.0),
        state_rate_hz=_env_float("STATE_RATE_HZ", 10.0),
        max_msg_per_sec=_env_float("MAX_MSG_PER_SEC", 30.0),
        port=_env_int("PORT", 8080),
        host=_env_str("HOST", "0.0.0.0"),
        max_vel_rad_s=_env_float("MAX_VEL_RAD_S", 1.0),
        watchdog_timeout=_env_float("WATCHDOG_TIMEOUT", 3.0),
        admin_token=admin_token,
        workspace_x=_env_range("WORKSPACE_X", (0.15, 0.38)),
        workspace_y=_env_range("WORKSPACE_Y", (-0.20, 0.20)),
        workspace_z=_env_range("WORKSPACE_Z", (0.08, 0.36)),
        ee_jog_step_m=_env_float("EE_JOG_STEP_M", 0.01),
        urdf_path=_env_str("URDF_PATH", default_urdf_path()),
        video_url=_env_str("VIDEO_URL", "/video/stream"),
        feature_video=_env_bool("FEATURE_VIDEO", False),
        feature_cartesian=_env_bool("FEATURE_CARTESIAN", False),
        attract_enabled=_env_bool("ATTRACT_ENABLED", True),
        attract_after_sec=_env_float("ATTRACT_AFTER_SEC", 120.0),
        attract_period_sec=_env_float("ATTRACT_PERIOD_SEC", 240.0),
        park_on_shutdown=_env_bool("PARK_ON_SHUTDOWN", True),
        park_timeout_sec=_env_float("PARK_TIMEOUT_SEC", 8.0),
        backend=_env_str("ROBOT_BACKEND", "sim"),
    )
