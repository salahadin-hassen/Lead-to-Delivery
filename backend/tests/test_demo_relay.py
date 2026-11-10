"""The loopback-only response-sanitizing relay used by the n8n demo (app/demo_relay.py).

Fake-upstream tests prove byte-exact forwarding and response reduction; the
end-to-end tests run the real API over HTTP (uvicorn) against ltd_test.
All tokens and contact data are synthetic."""

import json
import logging
import time

import pytest

from app import demo_relay
from app.security import hash_token, webhook_signature
from tests.crm_helpers import SECRET, counts, payload, signed_headers
from tests.relay_helpers import (
    SYNTHETIC_TOKEN,
    FakeUpstream,
    assert_safe_relay_response,
    live_api,
    post_raw,
    running_relay,
)

CLIENT_ID = "6f1d2c3b-4a5e-4f60-8a7b-9c0d1e2f3a4b"
INVITATION_ID = "0a1b2c3d-4e5f-4061-8273-94a5b6c7d8e9"
CREATED = {"status": "created", "client_id": CLIENT_ID, "invitation_id": INVITATION_ID,
           "onboarding_url": f"https://hub.example.test/onboarding#t={SYNTHETIC_TOKEN}",
           "expires_at": "2026-11-16T00:00:00Z"}
# Deliberately odd but valid JSON: key order, spacing, escapes and non-ASCII must survive.
ODD_BODY = '{ "z":1,"a" : "café \\u00e9 – \U0001f600","n":[1.50, 2e3] }'.encode("utf-8")


def _headers(body: bytes, **extra: str) -> dict[str, str]:
    return {**signed_headers(body), **extra}


# ------------------------------------------------------- forwarding exactness
def test_forwards_the_exact_bytes_and_auth_headers_and_nothing_else() -> None:
    headers = _headers(ODD_BODY, Authorization="Bearer should-not-be-forwarded", Cookie="a=b", **{"X-Extra": "1"})
    with FakeUpstream(201, CREATED) as upstream, running_relay(upstream.url) as relay:
        post_raw(relay, ODD_BODY, headers)
    [seen] = upstream.requests
    assert seen["path"] == demo_relay.PATH
    assert seen["body"] == ODD_BODY  # byte for byte
    sent = {k.lower(): v for k, v in seen["headers"].items()}
    assert sent["x-ltd-timestamp"] == headers["X-LTD-Timestamp"]
    assert sent["x-ltd-signature"] == headers["X-LTD-Signature"]
    assert sent["content-type"] == "application/json"
    assert sent["content-length"] == str(len(ODD_BODY))
    assert not {"authorization", "cookie", "x-extra"} & set(sent)


def test_created_response_is_reduced_to_ids(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    with FakeUpstream(201, CREATED, headers={"Content-Type": "application/json",
                                            "Location": f"/onboarding#t={SYNTHETIC_TOKEN}",
                                            "X-Debug": SYNTHETIC_TOKEN}) as upstream, \
            running_relay(upstream.url) as relay:
        status, headers, raw = post_raw(relay, ODD_BODY, _headers(ODD_BODY))
    assert upstream.body.count(SYNTHETIC_TOKEN.encode()) == 1  # the fixture really carries a token link
    assert status == 201
    assert assert_safe_relay_response(headers, raw) == {"status": "created", "client_id": CLIENT_ID,
                                                        "invitation_id": INVITATION_ID}
    assert headers["cache-control"] == "no-store"
    # Logs: one access line, no body, no token, no request content.
    assert SYNTHETIC_TOKEN not in caplog.text and "onboarding" not in caplog.text
    assert "caf" not in caplog.text and SECRET not in caplog.text
    assert any("relay POST /api/webhooks/crm/deal-won -> 201 (upstream 201)" in r.getMessage()
               for r in caplog.records)


def test_relay_process_output_contains_no_response_content(tmp_path) -> None:
    """The real `python -m app.demo_relay` process: its stdout/stderr never show bodies."""
    import socket
    import subprocess
    import sys
    from pathlib import Path

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with FakeUpstream(201, CREATED) as upstream:
        proc = subprocess.Popen([sys.executable, "-m", "app.demo_relay", "--port", str(port), "--upstream",
                                 upstream.url], cwd=Path(__file__).resolve().parents[1],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            relay = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 20
            while True:
                try:
                    status, _, raw = post_raw(relay, ODD_BODY, _headers(ODD_BODY))
                    break
                except OSError:
                    assert time.monotonic() < deadline and proc.poll() is None, "relay did not start"
                    time.sleep(0.1)
        finally:
            proc.terminate()
            output, _ = proc.communicate(timeout=10)
    assert status == 201 and SYNTHETIC_TOKEN.encode() not in raw
    assert "listening on http://127.0.0.1:" in output and "-> 201 (upstream 201)" in output
    for needle in (SYNTHETIC_TOKEN, "onboarding", "caf", CLIENT_ID, SECRET):
        assert needle not in output


# ------------------------------------------------------------ error responses
LEAKY_ERRORS = [
    (401, {"detail": f"bad {SYNTHETIC_TOKEN}", "code": "invalid_signature"}, {"status": "error", "code": "invalid_signature"}),
    (422, {"detail": [{"loc": ["body", "deal", "currency"], "msg": SYNTHETIC_TOKEN, "input": SYNTHETIC_TOKEN}]},
     {"status": "error", "field": "body.deal.currency"}),
    (422, {"detail": [{"loc": ["body", f"#t={SYNTHETIC_TOKEN}"], "msg": "x"}]}, {"status": "error"}),
    (409, {"detail": "x", "code": f"conflict#t={SYNTHETIC_TOKEN}"}, {"status": "error"}),
    (409, {"detail": SYNTHETIC_TOKEN, "code": "event_id_reused"}, {"status": "error", "code": "event_id_reused"}),
    (500, f"<html>{SYNTHETIC_TOKEN}</html>", {"status": "error", "code": "unexpected_response"}),
    (200, {"status": "duplicate", "event_id": f"https://x/onboarding#t={SYNTHETIC_TOKEN}"}, {"status": "duplicate"}),
    (201, {**CREATED, "client_id": SYNTHETIC_TOKEN}, {"status": "error", "code": "unexpected_response"}),
    (200, [SYNTHETIC_TOKEN], {"status": "error", "code": "unexpected_response"}),
]


@pytest.mark.parametrize("status,body,expected", LEAKY_ERRORS)
def test_error_and_odd_responses_never_leak_upstream_content(status, body, expected) -> None:
    raw_body = json.dumps(body) if isinstance(body, list) else body
    with FakeUpstream(status, raw_body) as upstream, running_relay(upstream.url) as relay:
        got_status, headers, raw = post_raw(relay, ODD_BODY, _headers(ODD_BODY))
    assert got_status == status
    assert assert_safe_relay_response(headers, raw) == expected


def test_redirects_are_not_followed_or_forwarded() -> None:
    with FakeUpstream(307, b"", headers={"Location": f"http://example.com/onboarding#t={SYNTHETIC_TOKEN}"}) as up, \
            running_relay(up.url) as relay:
        status, headers, raw = post_raw(relay, ODD_BODY, _headers(ODD_BODY))
    assert len(up.requests) == 1
    assert status == 502 and "location" not in headers
    assert assert_safe_relay_response(headers, raw) == {"status": "error", "code": "unexpected_response", "relay": True}


def test_unreachable_upstream_gives_a_safe_502() -> None:
    with FakeUpstream() as up:
        dead = up.url  # closed when the block exits
    with running_relay(dead) as relay:
        status, headers, raw = post_raw(relay, ODD_BODY, _headers(ODD_BODY))
    assert status == 502
    assert assert_safe_relay_response(headers, raw) == {"status": "error", "code": "upstream_unreachable", "relay": True}


@pytest.mark.parametrize("method,path,status", [("POST", "/other", 404), ("GET", demo_relay.PATH, 404),
                                                ("POST", demo_relay.PATH + "?x=1", 404)])
def test_only_the_webhook_path_is_relayed(method, path, status) -> None:
    with FakeUpstream(201, CREATED) as up, running_relay(up.url) as relay:
        got, headers, raw = post_raw(relay, ODD_BODY, _headers(ODD_BODY), path=path, method=method)
    assert got == status and up.requests == []
    assert_safe_relay_response(headers, raw)


def test_oversized_body_is_refused_without_contacting_the_api() -> None:
    body = b"{" + b" " * demo_relay.MAX_BODY_BYTES + b"}"
    with FakeUpstream(201, CREATED) as up, running_relay(up.url) as relay:
        status, headers, raw = post_raw(relay, body, _headers(body))
    assert status == 413 and up.requests == []
    assert assert_safe_relay_response(headers, raw)["code"] == "payload_too_large"


def test_response_serializer_refuses_non_allowlisted_output() -> None:
    assert json.loads(demo_relay.safe_json({"status": "created", "onboarding_url": "x"}))["code"] == "unsafe_response"
    assert json.loads(demo_relay.safe_json({"status": f"#t={SYNTHETIC_TOKEN}"}))["code"] == "unsafe_response"
    assert json.loads(demo_relay.safe_json({"status": "created", "client_id": CLIENT_ID})) == {
        "status": "created", "client_id": CLIENT_ID}


# ------------------------------------------------------- loopback restrictions
@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20", "10.0.0.5", "example.com", ""])
def test_refuses_non_loopback_bind_addresses(host) -> None:
    with pytest.raises(demo_relay.RelayConfigError):
        demo_relay.make_server(0, "http://127.0.0.1:8010", host=host)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_accepts_loopback_bind_addresses(host) -> None:
    server = demo_relay.make_server(0, "http://127.0.0.1:8010", host=host)
    server.server_close()


@pytest.mark.parametrize("url", [
    "https://127.0.0.1:8010", "http://10.0.0.5:8010", "http://example.com:8010", "http://0.0.0.0:8010",
    "http://127.0.0.1:8010/api", "http://user:pw@127.0.0.1:8010", "http://127.0.0.1:8010?x=1",
    "http://hub.example.test", "ftp://127.0.0.1:21", "127.0.0.1:8010", "http://127.0.0.1:99999",
])
def test_refuses_non_loopback_or_unusual_upstreams(url) -> None:
    with pytest.raises(demo_relay.RelayConfigError):
        demo_relay.parse_upstream(url)


@pytest.mark.parametrize("url,expected", [("http://127.0.0.1:8010", ("127.0.0.1", 8010)),
                                          ("http://localhost:8010/", ("localhost", 8010)),
                                          ("http://[::1]:8010", ("::1", 8010))])
def test_accepts_loopback_upstreams(url, expected) -> None:
    assert demo_relay.parse_upstream(url) == expected


def test_cli_refuses_a_public_bind_or_upstream(capsys) -> None:
    for argv in (["--host", "0.0.0.0"], ["--upstream", "http://203.0.113.10:8010"]):
        with pytest.raises(SystemExit) as exc:
            demo_relay.main(argv)
        assert exc.value.code == 2
    assert "non-loopback" in capsys.readouterr().err


# --------------------------------------------- end to end with the real API
@pytest.fixture
def api(db_conn, migrated_database, pool_cleanup):
    with live_api(migrated_database) as url:
        yield url


@pytest.fixture
def seen_upstream(monkeypatch):
    """Records what the relay received from the API (in memory), to prove what it stripped."""
    seen: list[tuple[int, bytes]] = []
    real = demo_relay.sanitize

    def spy(status, body):
        seen.append((status, body))
        return real(status, body)

    monkeypatch.setattr(demo_relay, "sanitize", spy)
    return seen


def test_api_authenticates_and_processes_through_the_relay(api, db_conn, seen_upstream) -> None:
    body = json.dumps(payload(event_id="relay-e2e-1"), separators=(",", ":")).encode()
    with running_relay(api) as relay:
        first = post_raw(relay, body, signed_headers(body))
        time.sleep(1.05)  # a later second: the replay carries a different timestamp and signature
        replay_headers = signed_headers(body)
        replay = post_raw(relay, body, replay_headers)

    # What the API really returned contains the invitation link; what n8n gets does not.
    (api_status, api_body), _ = seen_upstream
    upstream = json.loads(api_body)
    token = upstream["onboarding_url"].split("#t=", 1)[1]
    assert api_status == 201 and "#t=" in upstream["onboarding_url"]
    assert first[0] == 201
    out = assert_safe_relay_response(first[1], first[2], token, upstream["onboarding_url"])
    assert out == {"status": "created", "client_id": upstream["client_id"], "invitation_id": upstream["invitation_id"]}
    assert replay[0] == 200
    assert assert_safe_relay_response(replay[1], replay[2], token) == {"status": "duplicate", "event_id": "relay-e2e-1"}

    # One event, processed once, replay counted (byte exactness: see the re-serialization test).
    event = db_conn.execute("SELECT external_event_id, status, duplicate_count FROM webhook_events").fetchone()
    assert (event["external_event_id"], event["status"], event["duplicate_count"]) == ("relay-e2e-1", "processed", 1)
    invitations = db_conn.execute("SELECT * FROM onboarding_invitations ORDER BY created_at").fetchall()
    assert counts(db_conn)["clients"] == 1 and len(invitations) == 1, invitations
    assert db_conn.execute("SELECT count(*) AS n FROM client_access_tokens WHERE token_hash = %s",
                           (hash_token(token),)).fetchone()["n"] == 1  # the stripped token is real


def test_api_still_rejects_bad_signatures_through_the_relay(api, db_conn) -> None:
    body = json.dumps(payload(event_id="relay-e2e-bad")).encode()
    ts = str(int(time.time()))
    cases = [
        {"Content-Type": "application/json", "X-LTD-Timestamp": ts,
         "X-LTD-Signature": webhook_signature("x" * 40, ts, body)},                       # wrong secret
        {"Content-Type": "application/json", "X-LTD-Timestamp": ts,
         "X-LTD-Signature": webhook_signature(SECRET, ts, body + b" ")},                  # different bytes
        {"Content-Type": "application/json", "X-LTD-Timestamp": str(int(time.time()) - 3600),
         "X-LTD-Signature": webhook_signature(SECRET, str(int(time.time()) - 3600), body)},  # stale
        {"Content-Type": "application/json"},                                             # unsigned
    ]
    with running_relay(api) as relay:
        results = [post_raw(relay, body, h) for h in cases]
    for status, headers, raw in results:
        assert status == 401
        assert assert_safe_relay_response(headers, raw)["status"] == "error"
    assert all(n == 0 for n in counts(db_conn).values())


def test_relay_does_not_reserialize_semantically_equal_bodies(api, db_conn) -> None:
    """A signature over one byte sequence is rejected for an equivalent re-serialization:
    the relay must forward bytes, and the API proves it does."""
    data = payload(event_id="relay-e2e-bytes")
    signed = json.dumps(data, indent=2).encode()
    reserialized = json.dumps(data, separators=(",", ":")).encode()
    with running_relay(api) as relay:
        bad = post_raw(relay, reserialized, signed_headers(signed) | {"Content-Length": str(len(reserialized))})
        good = post_raw(relay, signed, signed_headers(signed))
    assert bad[0] == 401 and good[0] == 201


# ------------------------------------------------------------ negative control
def test_negative_control_a_leaky_relay_fails_the_safety_assertions(monkeypatch) -> None:
    """If the relay returned the API response unchanged, the checks used above must fail."""
    monkeypatch.setattr(demo_relay, "sanitize", lambda status, body: json.loads(body))
    monkeypatch.setattr(demo_relay, "safe_json", lambda data: json.dumps(data).encode())
    with FakeUpstream(201, CREATED) as up, running_relay(up.url) as relay:
        status, headers, raw = post_raw(relay, ODD_BODY, _headers(ODD_BODY))
    assert status == 201 and SYNTHETIC_TOKEN.encode() in raw  # the mutation really leaks
    with pytest.raises(AssertionError, match="leaks"):
        assert_safe_relay_response(headers, raw)
