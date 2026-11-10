"""POST /api/admin/login, POST /api/admin/logout, POST /api/admin/logout-all, GET /api/admin/me."""

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
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=1024)
    # A 6-digit authenticator code or a recovery code (xxxx-xxxx-xxxx).
    code: str = Field(min_length=6, max_length=20)


def _peer(request: Request) -> str:
    # The TCP peer as seen by this process. Behind a reverse proxy, run uvicorn
    # with --forwarded-allow-ips=<proxy address> so this is the real client;
    # X-Forwarded-For from anyone else is ignored.
    return request.client.host if request.client else "unknown"


def _me(session: AdminSession) -> dict:
    return {"authenticated": True, "expires_at": session.expires_at.isoformat(),
            "admin": {"email": session.email, "display_name": session.display_name}}


@router.post("/login")
def login(body: LoginRequest, request: Request) -> Response:
    settings = request.app.state.settings
    try:
        raw, expires_at = admin_auth.login(body.email, body.password, body.code, source_key=_peer(request),
                                           settings=settings)
    except AdminNotConfiguredError:
        return _error(503, "admin_not_configured", "Administrator sign-in is not configured.", headers=NO_STORE)
    except AdminLoginThrottledError as exc:
        return _error(429, "login_throttled", "Too many failed attempts. Try again later.",
                      headers={"Retry-After": str(exc.retry_after), **NO_STORE})
    except AdminAuthError:
        return _error(401, "invalid_credentials", "Incorrect email, password or code.", headers=NO_STORE)
    response = JSONResponse({"authenticated": True, "expires_at": expires_at.isoformat()}, headers=NO_STORE)
    response.set_cookie(
        COOKIE_NAME, raw, max_age=SESSION_LIFETIME_SECONDS, path=COOKIE_PATH,
        httponly=True, secure=settings.is_production, samesite="strict",
    )
    return response


def _cleared(request: Request, status: int = 204) -> Response:
    response = Response(status_code=status, headers=NO_STORE)
    response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH, httponly=True,
                           secure=request.app.state.settings.is_production, samesite="strict")
    return response


@router.post("/logout")
def logout(request: Request) -> Response:
    admin_auth.logout(request.cookies.get(COOKIE_NAME))
    return _cleared(request)


@router.post("/logout-all")
def logout_all(request: Request, session: AdminSession = Depends(require_admin)) -> Response:
    """Sign this administrator out on every device (including this one)."""
    admin_auth.logout_everywhere(session)
    return _cleared(request)


@router.get("/me")
def me(session: AdminSession = Depends(require_admin)) -> Response:
    return JSONResponse(_me(session), headers=NO_STORE)
