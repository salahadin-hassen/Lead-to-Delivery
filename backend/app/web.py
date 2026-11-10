"""HTTP hardening shared by every response, and same-origin serving of the
built frontend.

* Security headers on every response: no sniffing, no framing, no referrer,
  a strict Content-Security-Policy (``default-src 'none'`` for the API; a
  self-only policy for the React app), and HSTS in production.
* Host header allowlist (ALLOWED_HOSTS; in production defaults to the host of
  APP_BASE_URL). ``/api/health*`` is exempt so platform probes that use an
  internal host name keep working.
* A global request-body cap (routes enforce smaller limits of their own).
* In production the interactive API docs (/docs, /redoc, /openapi.json) are
  not served.
* FRONTEND_DIST_DIR (the output of ``npm run build``) is served by the API at
  /, /admin/* and /portal/*, so the admin console, the client portal and the
  API share one origin: the admin cookie stays first-party and no CORS is
  needed or configured.
"""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

MAX_REQUEST_BYTES = 1024 * 1024

API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
APP_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
           "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
COMMON_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}
HSTS = "max-age=31536000; includeSubDomains"
DOCS_PATHS = ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect")
SPA_PREFIXES = ("/admin", "/portal")


def _host_allowed(request: Request) -> bool:
    allowed = request.app.state.settings.allowed_host_list()
    if allowed is None or request.url.path.startswith("/api/health"):
        return True
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
    return host in allowed


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def _hardening(request: Request, call_next):
        settings = request.app.state.settings
        if not _host_allowed(request):
            response: Response = JSONResponse(status_code=400, content={"detail": "Invalid host.", "code": "invalid_host"})
        elif settings.is_production and request.url.path in DOCS_PATHS:
            response = JSONResponse(status_code=404, content={"detail": "Not Found"})
        else:
            length = request.headers.get("content-length")
            if length is not None and (not length.isdigit() or int(length) > MAX_REQUEST_BYTES):
                response = JSONResponse(status_code=413,
                                        content={"detail": "Request body too large.", "code": "payload_too_large"})
            else:
                response = await call_next(request)
        for name, value in COMMON_HEADERS.items():
            response.headers.setdefault(name, value)
        is_app_page = response.headers.get("content-type", "").startswith("text/html")
        response.headers.setdefault("Content-Security-Policy", APP_CSP if is_app_page else API_CSP)
        if settings.is_production:
            response.headers.setdefault("Strict-Transport-Security", HSTS)
        return response

    async def _serve(request: Request, path: str = "") -> Response:
        dist: Path | None = request.app.state.settings.frontend_dist_dir
        if dist is None:
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        root = dist.resolve()
        if request.url.path.startswith("/assets/"):
            target = (root / "assets" / path).resolve()
            if not target.is_relative_to(root / "assets") or not target.is_file():
                return JSONResponse(status_code=404, content={"detail": "Not Found"})
            # Vite gives assets content-hashed names: safe to cache for a year.
            return FileResponse(target, headers={"Cache-Control": "public, max-age=31536000, immutable"})
        index = root / "index.html"
        if not index.is_file():
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        return FileResponse(index, media_type="text/html", headers={"Cache-Control": "no-cache"})

    app.add_api_route("/assets/{path:path}", _serve, methods=["GET", "HEAD"], include_in_schema=False)
    app.add_api_route("/", _serve, methods=["GET", "HEAD"], include_in_schema=False)
    for prefix in SPA_PREFIXES:
        app.add_api_route(prefix, _serve, methods=["GET", "HEAD"], include_in_schema=False)
        app.add_api_route(prefix + "/{path:path}", _serve, methods=["GET", "HEAD"], include_in_schema=False)
