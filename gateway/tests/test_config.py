"""Тесты чтения конфигурации из окружения."""

from __future__ import annotations

import pytest

from so101_gateway.config import Config, ConfigError, default_urdf_path, load_config

ENV_VARS = [
    "CONTROL_DURATION",
    "IDLE_TIMEOUT",
    "RECONNECT_GRACE",
    "MAX_QUEUE",
    "COOLDOWN",
    "HANDOVER_HOME",
    "CMD_RATE_HZ",
    "STATE_RATE_HZ",
    "MAX_MSG_PER_SEC",
    "PORT",
    "MAX_VEL_RAD_S",
    "WATCHDOG_TIMEOUT",
    "ADMIN_TOKEN",
    "URDF_PATH",
    "VIDEO_URL",
    "FEATURE_VIDEO",
    "ROBOT_BACKEND",
    "WORKSPACE_X",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_admin_token_is_mandatory():
    with pytest.raises(ConfigError) as exc:
        load_config()
    assert "ADMIN_TOKEN" in str(exc.value)


def test_defaults_match_the_contract(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    cfg = load_config()
    assert cfg.control_duration == 90.0
    assert cfg.idle_timeout == 20.0
    assert cfg.reconnect_grace == 10.0
    assert cfg.max_queue == 20
    assert cfg.cooldown == 30.0
    assert cfg.handover_home == 2.0
    assert cfg.cmd_rate_hz == 50.0
    assert cfg.state_rate_hz == 10.0
    assert cfg.max_msg_per_sec == 30.0
    assert cfg.port == 8080
    assert cfg.max_vel_rad_s == 1.0
    assert cfg.watchdog_timeout == 3.0
    assert cfg.admin_token == "secret"


def test_values_are_read_from_environment(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "s")
    monkeypatch.setenv("CONTROL_DURATION", "45")
    monkeypatch.setenv("MAX_QUEUE", "5")
    monkeypatch.setenv("PORT", "9000")
    monkeypatch.setenv("MAX_VEL_RAD_S", "0.4")
    monkeypatch.setenv("FEATURE_VIDEO", "no")
    cfg = load_config()
    assert cfg.control_duration == 45.0
    assert cfg.max_queue == 5
    assert cfg.port == 9000
    assert cfg.max_vel_rad_s == 0.4
    assert cfg.feature_video is False


def test_workspace_range_is_parsed(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "s")
    monkeypatch.setenv("WORKSPACE_X", "0.1,0.4")
    assert load_config().workspace_x == (0.1, 0.4)


def test_broken_range_is_rejected(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "s")
    monkeypatch.setenv("WORKSPACE_X", "0.4")
    with pytest.raises(ConfigError):
        load_config()


def test_inverted_range_is_rejected(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "s")
    monkeypatch.setenv("WORKSPACE_X", "0.4,0.1")
    with pytest.raises(ConfigError):
        load_config()


def test_non_numeric_value_is_rejected(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "s")
    monkeypatch.setenv("CONTROL_DURATION", "долго")
    with pytest.raises(ConfigError):
        load_config()


def test_turn_slot_is_duration_plus_handover():
    cfg = Config(control_duration=90.0, handover_home=2.0)
    assert cfg.turn_slot_sec == 92.0


def test_replace_rejects_unknown_fields():
    with pytest.raises(ConfigError):
        Config().replace(nonexistent=1)


def test_default_urdf_path_points_at_deploy():
    assert default_urdf_path().endswith("so101_follower.generated.urdf")
    assert "deploy" in default_urdf_path()
