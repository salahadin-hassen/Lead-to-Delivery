"""POST /api/admin/login, POST /api/admin/logout, GET /api/admin/me."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from app.errors import AdminAuthError, AdminLoginThrottledError, AdminNotConfiguredError
from app.routes._http import error as _error
from app.routes.admin_deps import COOKIE_NAME, COOKIE_PATH, csrf_protect, require_admin
from app.services import admin_auth
from app.services.admin_auth import SESSION_LIFETIME_SECONDS, AdminSession

router = APIRouter(prefix="/admin", dependencies=[Depends(csrf_protect)])

NO_STORE = {"Cache-Control": "no-store"}


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=1, max_length=1024)


def _peer(request: Request) -> str:
    # The TCP peer as seen by this process. X-Forwarded-For is deliberately
    # ignored: it is client-controlled unless a trusted proxy rewrites it.
    return request.client.host if request.client else "unknown"


@router.post("/login")
def login(body: LoginRequest, request: Request) -> Response:
    settings = request.app.state.settings
    try:
        raw, expires_at = admin_auth.login(body.password, source_key=_peer(request), settings=settings)
    except AdminNotConfiguredError:
        return _error(503, "admin_not_configured", "Administrator login is not configured.", headers=NO_STORE)
    except AdminLoginThrottledError as exc:
        return _error(429, "login_throttled", "Too many failed attempts. Try again later.",
                      headers={"Retry-After": str(exc.retry_after), **NO_STORE})
    except AdminAuthError:
        return _error(401, "invalid_credentials", "Incorrect password.", headers=NO_STORE)
    response = JSONResponse({"authenticated": True, "expires_at": expires_at.isoformat()}, headers=NO_STORE)
    response.set_cookie(
        COOKIE_NAME, raw, max_age=SESSION_LIFETIME_SECONDS, path=COOKIE_PATH,
        httponly=True, secure=settings.is_production, samesite="strict",
    )
    return response


@router.post("/logout")
def logout(request: Request) -> Response:
    admin_auth.logout(request.cookies.get(COOKIE_NAME))
    response = Response(status_code=204, headers=NO_STORE)
    response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH, httponly=True,
                           secure=request.app.state.settings.is_production, samesite="strict")
    return response


@router.get("/me")
def me(session: AdminSession = Depends(require_admin)) -> Response:
    return JSONResponse({"authenticated": True, "expires_at": session.expires_at.isoformat()}, headers=NO_STORE)
