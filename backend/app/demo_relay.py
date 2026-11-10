"""Loopback-only response-sanitizing relay for the n8n CRM demo (local demo tool).

The webhook's 201 response contains ``onboarding_url``, a link carrying the
client's bearer token. n8n shows every node's output in the editor, so an n8n
HTTP Request node must never receive that response. This relay sits between n8n
and the API:

    n8n --POST raw bytes + X-LTD-Timestamp/X-LTD-Signature--> relay --same bytes/headers--> API
    n8n <------------- allowlisted fields only -------------- relay <--- full response --- API

* The request body is read as bytes and forwarded unchanged (never parsed or
  re-serialized); ``Content-Type``, ``X-LTD-Timestamp`` and ``X-LTD-Signature``
  are copied verbatim. The API still verifies the HMAC, the timestamp window,
  every field and idempotency: the relay holds no secret and decides nothing.
* The API response is kept in memory only, reduced to an allowlist, and never
  logged. Anything that does not fit the allowlist is dropped.
* It binds to a loopback address only, forwards only to a loopback ``http``
  upstream, accepts one path, and never follows redirects.

Run:  python -m app.demo_relay [--port 8011] [--upstream http://127.0.0.1:8010]
"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import logging
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger("ltd.demo_relay")

PATH = "/api/webhooks/crm/deal-won"
FORWARDED_HEADERS = ("Content-Type", "X-LTD-Timestamp", "X-LTD-Signature")
MAX_BODY_BYTES = 64 * 1024  # same limit as the API
UPSTREAM_TIMEOUT_SECONDS = 15.0
LOOPBACK_NAMES = {"localhost"}

# Every field the relay can ever return to n8n.
RESPONSE_FIELDS = frozenset({"status", "client_id", "invitation_id", "event_id", "code", "field", "relay"})
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,199}$")
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_LOC_PART = re.compile(r"^[A-Za-z0-9_]{1,64}$")
# Belt and braces: none of these may ever appear in what the relay returns.
_FORBIDDEN = ("#t=", "onboarding", "://", "token")


class RelayConfigError(ValueError):
    pass


def _is_loopback(host: str) -> bool:
    if host.lower() in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_bind_host(host: str) -> str:
    if not _is_loopback(host):
        raise RelayConfigError(f"refusing to bind to non-loopback address {host!r}")
    return host


def parse_upstream(url: str) -> tuple[str, int]:
    """``http://<loopback>:<port>`` with no path, query, fragment or credentials."""
    parts = urlsplit(url)
    if parts.scheme != "http":
        raise RelayConfigError("upstream must be a plain http:// loopback URL")
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise RelayConfigError("upstream must be scheme://host:port only")
    host = parts.hostname or ""
    if not _is_loopback(host):
        raise RelayConfigError(f"refusing non-loopback upstream {host!r}")
    try:
        port = parts.port or 80
    except ValueError as exc:
        raise RelayConfigError("upstream port is invalid") from exc
    return host, port


def _uuid(value: Any) -> str | None:
    return value if isinstance(value, str) and _UUID.match(value) else None


def sanitize(status: int, body: bytes) -> dict[str, Any]:
    """Reduce an API response to the allowlist. Never returns upstream text."""
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        data = None
    if not isinstance(data, dict):
        return {"status": "error", "code": "unexpected_response"}

    if status == 201 and data.get("status") == "created":
        client_id, invitation_id = _uuid(data.get("client_id")), _uuid(data.get("invitation_id"))
        if client_id and invitation_id:
            return {"status": "created", "client_id": client_id, "invitation_id": invitation_id}
        return {"status": "error", "code": "unexpected_response"}
    if status == 200 and data.get("status") in ("duplicate", "duplicate_deal"):
        event_id = data.get("event_id")
        out = {"status": data["status"]}
        if isinstance(event_id, str) and _EVENT_ID.match(event_id):
            out["event_id"] = event_id
        return out

    out: dict[str, Any] = {"status": "error"}
    code = data.get("code")
    if isinstance(code, str) and _CODE.match(code):
        out["code"] = code
    detail = data.get("detail")
    if status == 422 and isinstance(detail, list) and detail and isinstance(detail[0], dict):
        loc = detail[0].get("loc")
        if isinstance(loc, list) and loc and all(isinstance(p, (str, int)) and _LOC_PART.match(str(p)) for p in loc):
            out["field"] = ".".join(str(p) for p in loc)[:200]
    return out


def safe_json(payload: dict[str, Any]) -> bytes:
    """Serialize the relay response, refusing anything outside the allowlist."""
    text = json.dumps(payload, separators=(",", ":"))
    if set(payload) - RESPONSE_FIELDS or any(s in text.lower() for s in _FORBIDDEN):
        return b'{"status":"error","code":"unsafe_response","relay":true}'
    return text.encode("ascii")


class RelayHandler(BaseHTTPRequestHandler):
    server_version = "ltd-demo-relay"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    upstream: tuple[str, int] = ("127.0.0.1", 8010)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        pass  # the stdlib access log is replaced by _log_line (no bodies, no query strings)

    def _log_line(self, status: int, upstream_status: int | None, started: float) -> None:
        log.info("relay %s %s -> %s (upstream %s) %.0f ms", self.command, PATH if self.path == PATH else "<other>",
                 status, upstream_status if upstream_status is not None else "-", (time.monotonic() - started) * 1000)

    def _reply(self, status: int, payload: dict[str, Any]) -> None:
        body = safe_json(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        started = time.monotonic()
        self.close_connection = True
        self._reply(404, {"status": "error", "code": "not_found", "relay": True})
        self._log_line(404, None, started)

    do_PUT = do_DELETE = do_PATCH = do_HEAD = do_GET  # noqa: N815

    def do_POST(self) -> None:  # noqa: N802
        started = time.monotonic()
        self.close_connection = True
        if self.path != PATH:
            self._reply(404, {"status": "error", "code": "not_found", "relay": True})
            return self._log_line(404, None, started)
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._reply(411, {"status": "error", "code": "length_required", "relay": True})
            return self._log_line(411, None, started)
        if length < 0 or length > MAX_BODY_BYTES:
            self._reply(413, {"status": "error", "code": "payload_too_large", "relay": True})
            return self._log_line(413, None, started)
        raw = self.rfile.read(length)  # bytes, forwarded as-is

        headers = {name: self.headers[name] for name in FORWARDED_HEADERS if self.headers.get(name) is not None}
        headers["Content-Length"] = str(len(raw))
        host, port = self.upstream
        conn = http.client.HTTPConnection(host, port, timeout=UPSTREAM_TIMEOUT_SECONDS)
        try:
            conn.request("POST", PATH, body=raw, headers=headers)
            response = conn.getresponse()  # http.client never follows redirects
            upstream_status, upstream_body = response.status, response.read(1024 * 1024)
        except (OSError, http.client.HTTPException):
            self._reply(502, {"status": "error", "code": "upstream_unreachable", "relay": True})
            return self._log_line(502, None, started)
        finally:
            conn.close()

        if 300 <= upstream_status < 400 or upstream_status >= 600:
            status, payload = 502, {"status": "error", "code": "unexpected_response", "relay": True}
        else:
            status, payload = upstream_status, sanitize(upstream_status, upstream_body)
        del upstream_body  # the only copy of the API response; never stored or logged
        self._reply(status, payload)
        self._log_line(status, upstream_status, started)


def make_server(port: int, upstream: str, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    handler = type("ConfiguredRelayHandler", (RelayHandler,), {"upstream": parse_upstream(upstream)})
    return ThreadingHTTPServer((check_bind_host(host), port), handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1", help="loopback address to bind (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8011)
    parser.add_argument("--upstream", default="http://127.0.0.1:8010", help="the API, loopback only")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        server = make_server(args.port, args.upstream, args.host)
    except RelayConfigError as exc:
        parser.error(str(exc))
    log.info("demo relay listening on http://%s:%s, forwarding to %s:%s", args.host, args.port, *server.RequestHandlerClass.upstream)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        log.info("demo relay stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
