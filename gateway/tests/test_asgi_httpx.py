"""REST-контракт через httpx ASGITransport (без поднятия сокета)."""

from __future__ import annotations

import uuid

import httpx
import pytest

from so101_gateway.app import create_app
from so101_gateway.safety.limits import JOINT_ORDER

ADMIN = "test-admin-token"


@pytest.fixture
def asgi_client(config):
    app = create_app(config=config.replace(admin_token=ADMIN))
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://gateway")


async def test_health(asgi_client):
    async with asgi_client as client:
        response = await client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"ok": True}


async def test_status(asgi_client):
    async with asgi_client as client:
        body = (await client.get("/api/status")).json()
        assert body == {
            "robot": "idle",
            "occupied": False,
            "queue_length": 0,
            "estimated_wait_sec": 0,
        }


async def test_session_issues_token_and_cookie(asgi_client):
    async with asgi_client as client:
        response = await client.post("/api/session")
        token = response.json()["token"]
        assert uuid.UUID(token).version == 4
        assert response.cookies["so101_token"] == token

        again = await client.post("/api/session")
        assert again.json()["token"] == token


async def test_config_contract(asgi_client):
    async with asgi_client as client:
        body = (await client.get("/api/config")).json()
        assert body["protocol"] == 1
        assert [j["name"] for j in body["joints"]] == list(JOINT_ORDER)
        assert body["features"]["cartesian"] is False   # выключен на стенде


async def test_admin_requires_header(asgi_client):
    async with asgi_client as client:
        assert (await client.get("/api/admin/queue")).status_code == 403
        ok = await client.get("/api/admin/queue", headers={"X-Admin-Token": ADMIN})
        assert ok.status_code == 200
        assert ok.json()["controller"] is None


async def test_estop_flow(asgi_client):
    async with asgi_client as client:
        headers = {"X-Admin-Token": ADMIN}
        await client.post("/api/admin/estop", headers=headers)
        assert (await client.get("/api/status")).json()["robot"] == "estop"
        await client.post("/api/admin/release_estop", headers=headers)
        assert (await client.get("/api/status")).json()["robot"] != "estop"
