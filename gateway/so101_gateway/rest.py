"""REST-часть контракта (docs/api.md §2)."""

from __future__ import annotations

import hmac
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)

__all__ = ["build_router", "COOKIE_NAME"]

COOKIE_NAME = "so101_token"
COOKIE_MAX_AGE = 7 * 24 * 3600


def _is_https(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "")
    if forwarded:
        return forwarded.split(",")[0].strip().lower() == "https"
    return request.url.scheme == "https"


def _valid_token(value: Optional[str]) -> bool:
    if not value:
        return False
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def build_router(service) -> APIRouter:
    router = APIRouter()
    config = service.config

    def require_admin(token: Optional[str]) -> None:
        """403 без подробностей: не подтверждаем даже существование эндпоинта."""
        expected = config.admin_token
        if not expected or not token or not hmac.compare_digest(token, expected):
            raise HTTPException(status_code=403, detail="forbidden")

    # --- публичное ---------------------------------------------------- #

    @router.get("/api/health")
    async def health() -> Response:
        if service.backend.is_connected():
            return JSONResponse({"ok": True})
        return JSONResponse(
            {"ok": False, "reason": "бэкенд робота недоступен"}, status_code=503
        )

    @router.get("/api/status")
    async def status() -> dict:
        return service.public_status()

    @router.post("/api/session")
    async def session(request: Request, response: Response) -> dict:
        token = request.cookies.get(COOKIE_NAME)
        if not _valid_token(token):
            token = str(uuid.uuid4())
        response.set_cookie(
            COOKIE_NAME,
            token,
            httponly=True,
            samesite="lax",
            secure=_is_https(request),
            max_age=COOKIE_MAX_AGE,
            path="/",
        )
        return {"token": token}

    @router.get("/api/config")
    async def client_config() -> dict:
        return service.client_config()

    # --- админское ----------------------------------------------------- #

    @router.get("/api/admin/queue")
    async def admin_queue(x_admin_token: Optional[str] = Header(default=None)) -> dict:
        require_admin(x_admin_token)
        return service.manager.admin_snapshot()

    @router.post("/api/admin/kick")
    async def admin_kick(x_admin_token: Optional[str] = Header(default=None)) -> dict:
        require_admin(x_admin_token)
        await service.dispatch(service.manager.kick())
        log.warning("админ снял текущего оператора")
        return {"ok": True}

    @router.post("/api/admin/estop")
    async def admin_estop(x_admin_token: Optional[str] = Header(default=None)) -> dict:
        require_admin(x_admin_token)
        await service.dispatch(service.manager.estop())
        log.warning("АВАРИЙНАЯ ОСТАНОВКА")
        return {"ok": True, "estop": True}

    @router.post("/api/admin/release_estop")
    async def admin_release_estop(x_admin_token: Optional[str] = Header(default=None)) -> dict:
        require_admin(x_admin_token)
        await service.dispatch(service.manager.release_estop())
        log.warning("аварийная остановка снята")
        return {"ok": True, "estop": False}

    return router
