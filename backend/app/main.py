"""FastAPI application factory.

Run locally::

    uvicorn app.main:app --port 8010

Migrations are NOT applied at startup; run ``python -m app.migrations``.
/api/health reports 503 until the database is reachable and fully migrated.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import db, migrations, web
from app.config import Settings, get_settings
from app.errors import (
    AdminAuthError,
    AdminCsrfError,
    ConflictError,
    DatabaseUnavailableError,
    NotFoundError,
    PortalAuthError,
)
from app.routes._http import error as _error
from app.routes.admin import router as admin_router
from app.routes.admin_auth import router as admin_auth_router
from app.routes.admin_deps import COOKIE_NAME, COOKIE_PATH
from app.routes.admin_integrations import router as admin_integrations_router
from app.routes.health import router as health_router
from app.routes.portal import portal_auth_response
from app.routes.portal import router as portal_router
from app.routes.webhooks import router as webhooks_router

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Building Settings validates configuration; production fails closed here.
        active = settings or get_settings()
        app.state.settings = active
        app.state.expected_schema_version = migrations.latest_version()
        db.configure_pool(active.database_url.get_secret_value())
        try:
            yield
        finally:
            db.close_pool()

    app = FastAPI(title="Agency Lead-to-Delivery Automation Hub", lifespan=lifespan)
    web.install(app)
    app.include_router(health_router, prefix="/api")
    app.include_router(webhooks_router, prefix="/api")
    app.include_router(portal_router, prefix="/api")
    app.include_router(admin_auth_router, prefix="/api")
    app.include_router(admin_router, prefix="/api")
    app.include_router(admin_integrations_router, prefix="/api")

    @app.middleware("http")
    async def _no_store(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith(("/api/admin", "/api/portal")):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Like FastAPI's default 422, but never echoes the submitted input.
        detail = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
                  for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": detail})

    @app.exception_handler(AdminAuthError)
    async def _admin_auth(request: Request, exc: AdminAuthError) -> JSONResponse:
        response = _error(401, exc.code, "Administrator sign-in required.")
        response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)
        return response

    @app.exception_handler(AdminCsrfError)
    async def _admin_csrf(request: Request, exc: AdminCsrfError) -> JSONResponse:
        status_for = {"json_required": 415, "payload_too_large": 413}
        logger.warning("admin request refused by CSRF checks: %s", exc.code)
        return _error(status_for.get(exc.code, 403), exc.code, "Request refused by CSRF protection.")

    @app.exception_handler(NotFoundError)
    async def _not_found(request: Request, exc: NotFoundError) -> JSONResponse:
        return _error(404, "not_found", "Not found.")

    @app.exception_handler(ConflictError)
    async def _conflict(request: Request, exc: ConflictError) -> JSONResponse:
        return _error(409, exc.code, exc.message)

    @app.exception_handler(PortalAuthError)
    async def _portal_auth(request: Request, exc: PortalAuthError) -> JSONResponse:
        # The code says why (missing/invalid/expired/revoked); the token is never logged.
        logger.info("portal authentication failed: %s", exc.code)
        return portal_auth_response(exc)

    @app.exception_handler(DatabaseUnavailableError)
    async def _database_unavailable(request: Request, exc: DatabaseUnavailableError) -> JSONResponse:
        logger.warning("database unavailable during %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "Service temporarily unavailable"},
        )

    @app.exception_handler(psycopg.Error)
    async def _database_error(request: Request, exc: psycopg.Error) -> JSONResponse:
        # Class name and SQLSTATE only: driver messages may contain schema details.
        logger.error(
            "database error during %s %s: %s (SQLSTATE %s)",
            request.method, request.url.path, type(exc).__name__, exc.sqlstate,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error"},
        )

    return app


app = create_app()
