"""Helpers for the demo relay tests: a real API server (uvicorn, ltd_test), the
relay, and a recording fake upstream."""

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import uvicorn

from app import demo_relay
from app.main import create_app
from tests.conftest import make_settings
from tests.crm_helpers import BASE_URL, SECRET

SYNTHETIC_TOKEN = "SyntheticBearerToken_0123456789abcdefghijklmnopqrstuvwxyz"
RELAY_HEADERS = {"content-type", "content-length", "cache-control", "date", "server", "connection"}


@contextmanager
def live_api(database_url: str, **overrides: Any) -> Iterator[str]:
    """The real FastAPI app over real HTTP on a free loopback port."""
    app = create_app(make_settings(database_url, crm_webhook_secret=SECRET, app_base_url=BASE_URL, **overrides))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        assert time.monotonic() < deadline and thread.is_alive(), "API did not start"
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)


@contextmanager
def running_relay(upstream: str) -> Iterator[str]:
    server = demo_relay.make_server(0, upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


class FakeUpstream:
    """Records the raw request it receives and answers with a scripted response."""

    def __init__(self, status: int = 201, body: bytes | str | dict = b"{}", headers: dict[str, str] | None = None):
        self.status, self.headers = status, headers or {"Content-Type": "application/json"}
        self.body = json.dumps(body).encode() if isinstance(body, dict) else (
            body.encode() if isinstance(body, str) else body)
        self.requests: list[dict[str, Any]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                fake.requests.append({"path": self.path, "body": raw, "headers": dict(self.headers.items())})
                self.send_response(fake.status)
                for name, value in fake.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(fake.body)))
                self.end_headers()
                self.wfile.write(fake.body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "FakeUpstream":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()


def post_raw(url: str, body: bytes, headers: dict[str, str], *, path: str = demo_relay.PATH,
             method: str = "POST") -> tuple[int, dict[str, str], bytes]:
    """Plain HTTP client that never follows redirects; returns (status, headers, raw body)."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    request = urllib.request.Request(url + path, data=body if method == "POST" else None, headers=headers,
                                     method=method)
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as err:
        return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read()


def assert_safe_relay_response(headers: dict[str, str], raw: bytes, *secrets: str) -> dict[str, Any]:
    """What n8n's HTTP node would receive: only allowlisted fields and headers, no token material."""
    text = raw.decode("utf-8")
    for needle in ("onboarding", "#t=", "://", SYNTHETIC_TOKEN, *secrets):
        assert needle not in text, f"relay response leaks {needle[:12]!r}"
        assert all(needle not in v for v in headers.values())
    data = json.loads(text)
    assert isinstance(data, dict) and set(data) <= demo_relay.RESPONSE_FIELDS, sorted(data)
    assert set(headers) <= RELAY_HEADERS, sorted(headers)
    return data
