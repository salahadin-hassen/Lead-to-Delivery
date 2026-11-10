"""Dependencies shared by every /api/admin route: session auth and CSRF checks.

CSRF defence for state-changing requests (anything but GET/HEAD/OPTIONS):
* the session cookie is SameSite=Strict, so cross-site requests carry none;
* ``X-Requested-With: XMLHttpRequest`` is required: a custom header that a
  cross-site form cannot send and that forces a CORS preflight, which fails
  because no CORS is configured;
* if an ``Origin`` header is present it must equal APP_BASE_URL's origin;
* request bodies must be JSON and at most 64 KiB.
"""

from urllib.parse import urlparse

from fastapi import Request

from app.errors import AdminAuthError, AdminCsrfError
from app.services import admin_auth
from app.services.admin_auth import AdminSession

COOKIE_NAME = "ltd_admin_session"
COOKIE_PATH = "/api/admin"
MAX_ADMIN_BODY_BYTES = 64 * 1024
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def expected_origin(app_base_url: str) -> str:
    parsed = urlparse(app_base_url)
    return f"{parsed.scheme}://{parsed.netloc}".lower()


def csrf_protect(request: Request) -> None:
    if request.method in _SAFE_METHODS:
        return
    if request.headers.get("x-requested-with") != "XMLHttpRequest":
        raise AdminCsrfError("csrf_header_missing")
    origin = request.headers.get("origin")
    if origin is not None and origin.lower() != expected_origin(request.app.state.settings.app_base_url):
        raise AdminCsrfError("origin_not_allowed")
    length = request.headers.get("content-length")
    has_body = (length is not None and length != "0") or "transfer-encoding" in request.headers
    if length is not None and (not length.isdigit() or int(length) > MAX_ADMIN_BODY_BYTES):
        raise AdminCsrfError("payload_too_large")
    if has_body and request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise AdminCsrfError("json_required")


def require_admin(request: Request) -> AdminSession:
    """The administrator session behind the cookie, or 401."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise AdminAuthError("admin_auth_required")
    return admin_auth.authenticate(token)
