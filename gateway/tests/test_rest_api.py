"""Тесты REST-части контракта (docs/api.md §2)."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from so101_gateway.app import create_app
from so101_gateway.backends.sim import SimBackend
from so101_gateway.safety.limits import JOINT_ORDER, JointLimits

ADMIN = "test-admin-token"


class DeadBackend(SimBackend):
    def is_connected(self) -> bool:
        return False


@pytest.fixture
def app(config):
    return create_app(config=config.replace(admin_token=ADMIN))


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


# --------------------------------------------------------------------------- #
# health
# --------------------------------------------------------------------------- #

def test_health_ok(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_health_503_when_backend_is_down(config):
    cfg = config.replace(admin_token=ADMIN)
    backend = DeadBackend(limits=JointLimits.fallback(), config=cfg, clock=__import__("time").time)
    with TestClient(create_app(config=cfg, backend=backend)) as client:
        r = client.get("/api/health")
        assert r.status_code == 503
        body = r.json()
        assert body["ok"] is False
        assert isinstance(body["reason"], str) and body["reason"]


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #

def test_status_shape(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"robot", "occupied", "queue_length", "estimated_wait_sec"}
    assert body["robot"] in {"idle", "moving", "error", "estop"}
    assert body["occupied"] is False
    assert body["queue_length"] == 0
    assert body["estimated_wait_sec"] == 0


def test_status_needs_no_auth(client):
    assert client.get("/api/status").status_code == 200


# --------------------------------------------------------------------------- #
# session
# --------------------------------------------------------------------------- #

def test_session_issues_uuid_token(client):
    r = client.post("/api/session")
    assert r.status_code == 200
    token = r.json()["token"]
    assert uuid.UUID(token).version == 4


def test_session_sets_httponly_cookie(client):
    r = client.post("/api/session")
    raw = r.headers["set-cookie"]
    assert "so101_token=" in raw
    assert "HttpOnly" in raw
    # атрибуты cookie регистронезависимы (RFC 6265), starlette пишет "lax"
    assert "samesite=lax" in raw.lower()


def test_session_is_idempotent_with_cookie(client):
    first = client.post("/api/session").json()["token"]
    second = client.post("/api/session").json()["token"]
    assert first == second


def test_session_replaces_garbage_cookie(client):
    client.cookies.set("so101_token", "not-a-uuid")
    token = client.post("/api/session").json()["token"]
    assert token != "not-a-uuid"
    assert uuid.UUID(token).version == 4


def test_session_marks_cookie_secure_behind_https(client):
    r = client.post("/api/session", headers={"X-Forwarded-Proto": "https"})
    assert "Secure" in r.headers["set-cookie"]


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

def test_config_shape_matches_contract(client, config):
    body = client.get("/api/config").json()
    assert set(body) == {
        "protocol",
        "joints",
        "workspace",
        "ee_jog_step_m",
        "control_duration_sec",
        "idle_timeout_sec",
        "cooldown_sec",
        "reconnect_grace_sec",
        "watchdog_timeout_sec",
        "max_msg_per_sec",
        "max_queue",
        "video_url",
        "features",
    }
    assert body["protocol"] == 1
    assert body["control_duration_sec"] == 90
    assert body["idle_timeout_sec"] == 20
    assert body["cooldown_sec"] == 30
    assert body["reconnect_grace_sec"] == 10
    assert body["watchdog_timeout_sec"] == 3
    assert body["max_msg_per_sec"] == 30
    assert body["max_queue"] == 20
    assert body["video_url"] == "/video/stream"
    assert body["ee_jog_step_m"] == pytest.approx(0.01)
    # На стенде декартов режим выключен: см. Config.feature_cartesian.
    assert body["features"] == {"cartesian": False, "video": True}


def test_config_joints_come_in_canonical_order_with_limits(client):
    joints = client.get("/api/config").json()["joints"]
    assert [j["name"] for j in joints] == list(JOINT_ORDER)
    for entry in joints:
        assert entry["min"] < entry["max"]
        assert entry["label"]
        if entry["name"] == "gripper":
            assert set(entry) == {"name", "min", "max", "label", "normalized"}
        else:
            assert set(entry) == {"name", "min", "max", "label"}


def test_config_reports_gripper_normalised(client):
    """Захват на проводе — доли 0..1 с пометкой normalized (docs/api.md §2)."""
    joints = client.get("/api/config").json()["joints"]
    gripper = next(j for j in joints if j["name"] == "gripper")
    assert gripper["min"] == 0.0
    assert gripper["max"] == 1.0
    assert gripper["normalized"] is True


def test_config_workspace_is_a_box(client, config):
    ws = client.get("/api/config").json()["workspace"]
    assert ws == {
        "x": list(config.workspace_x),
        "y": list(config.workspace_y),
        "z": list(config.workspace_z),
    }


def test_cartesian_feature_is_off_without_ik(config):
    class NoIkBackend(SimBackend):
        jog_ee = None

    cfg = config.replace(admin_token=ADMIN)
    backend = NoIkBackend(limits=JointLimits.fallback(), config=cfg, clock=__import__("time").time)
    with TestClient(create_app(config=cfg, backend=backend)) as client:
        assert client.get("/api/config").json()["features"]["cartesian"] is False


# --------------------------------------------------------------------------- #
# админка
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "path", ["/api/admin/queue", "/api/admin/kick", "/api/admin/estop", "/api/admin/release_estop"]
)
def test_admin_requires_token(client, path):
    method = client.get if path.endswith("queue") else client.post
    assert method(path).status_code == 403
    assert method(path, headers={"X-Admin-Token": "wrong"}).status_code == 403


def test_admin_403_does_not_leak_details(client):
    r = client.get("/api/admin/queue")
    assert r.status_code == 403
    assert r.json() == {"detail": "forbidden"}


def test_admin_queue_snapshot(client):
    r = client.get("/api/admin/queue", headers={"X-Admin-Token": ADMIN})
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"now", "robot", "estop", "controller", "queue", "observers"}
    assert body["controller"] is None
    assert body["queue"] == []


def test_admin_estop_and_release(client):
    assert client.post("/api/admin/estop", headers={"X-Admin-Token": ADMIN}).status_code == 200
    assert client.get("/api/status").json()["robot"] == "estop"

    assert (
        client.post("/api/admin/release_estop", headers={"X-Admin-Token": ADMIN}).status_code == 200
    )
    assert client.get("/api/status").json()["robot"] in {"idle", "moving"}


def test_admin_kick_without_controller_is_ok(client):
    r = client.post("/api/admin/kick", headers={"X-Admin-Token": ADMIN})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_unknown_route_is_404(client):
    assert client.get("/api/nope").status_code == 404
