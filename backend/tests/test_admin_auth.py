"""Administrator login, sessions, logout, CSRF and login throttling."""

import logging

import pytest

from app.main import create_app
from app.security import hash_token
from tests.admin_helpers import PASSWORD, XHR, admin_app, logged_in, login


@pytest.fixture
def client(db_conn, migrated_database, pool_cleanup):
    with admin_app(migrated_database) as c:
        yield c


def _session_rows(conn):
    return conn.execute("SELECT * FROM admin_sessions ORDER BY created_at").fetchall()


# ------------------------------------------------------------------ login
def test_correct_password_creates_session_cookie(client, db_conn) -> None:
    response = login(client)
    assert response.status_code == 200
    assert response.json()["authenticated"] is True
    cookie = response.headers["set-cookie"]
    assert "ltd_admin_session=" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Path=/api/admin" in cookie and "Max-Age=43200" in cookie
    assert "Secure" not in cookie  # APP_ENV=test: local HTTP
    raw = client.cookies.get("ltd_admin_session")
    assert raw and raw not in response.text  # the token travels only in the cookie
    (row,) = _session_rows(db_conn)
    assert row["token_hash"] == hash_token(raw) and raw not in str(row)
    hours = (row["expires_at"] - row["created_at"]).total_seconds() / 3600
    assert 11.99 < hours <= 12.0
    me = client.get("/api/admin/me")
    assert me.status_code == 200 and me.json()["authenticated"] is True


def test_cookie_is_secure_in_production(db_conn, migrated_database, pool_cleanup) -> None:
    with admin_app(migrated_database, app_env="production", app_base_url="https://hub.example.test",
                   crm_webhook_secret="s" * 40) as client:
        cookie = login(client).headers["set-cookie"]
    assert "Secure" in cookie


def test_wrong_password_is_generic_401(client, db_conn) -> None:
    response = login(client, "wrong password")
    assert (response.status_code, response.json()) == (401, {"detail": "Incorrect password.", "code": "invalid_credentials"})
    assert "set-cookie" not in response.headers
    assert _session_rows(db_conn) == []


def test_missing_password_hash_refuses_login(db_conn, migrated_database, pool_cleanup) -> None:
    with admin_app(migrated_database, admin_password_hash=None) as client:
        response = login(client)
        assert (response.status_code, response.json()["code"]) == (503, "admin_not_configured")
        assert client.get("/api/admin/dashboard").status_code == 401


def test_malformed_password_hash_prevents_startup(migrated_database) -> None:
    from pydantic import ValidationError

    from tests.conftest import make_settings
    with pytest.raises(ValidationError):
        create_app(make_settings(migrated_database, admin_password_hash="plaintext"))


def test_login_body_is_validated(client, db_conn) -> None:
    assert client.post("/api/admin/login", json={"password": ""}, headers=XHR).status_code == 422
    r = client.post("/api/admin/login", json={"password": PASSWORD, "admin": True}, headers=XHR)
    assert r.status_code == 422 and PASSWORD not in r.text  # input never echoed


# -------------------------------------------------------------- sessions
def test_expired_session_is_rejected(client, db_conn) -> None:
    logged_in(client)
    db_conn.execute("UPDATE admin_sessions SET expires_at = now() - interval '1 second'")
    response = client.get("/api/admin/me")
    assert (response.status_code, response.json()["code"]) == (401, "admin_auth_required")


def test_revoked_session_is_rejected(client, db_conn) -> None:
    logged_in(client)
    db_conn.execute("UPDATE admin_sessions SET revoked_at = now()")
    assert client.get("/api/admin/dashboard").status_code == 401


def test_forged_cookie_is_rejected(client, db_conn) -> None:
    client.cookies.set("ltd_admin_session", "A" * 43, path="/api/admin")
    assert client.get("/api/admin/me").status_code == 401


def test_logout_revokes_session_and_clears_cookie(client, db_conn) -> None:
    logged_in(client)
    raw = client.cookies.get("ltd_admin_session")
    response = client.post("/api/admin/logout", headers=XHR)
    assert response.status_code == 204
    assert 'ltd_admin_session=""' in response.headers["set-cookie"] or "Max-Age=0" in response.headers["set-cookie"]
    assert _session_rows(db_conn)[0]["revoked_at"] is not None
    client.cookies.set("ltd_admin_session", raw, path="/api/admin")  # replaying the old cookie
    assert client.get("/api/admin/me").status_code == 401


def test_every_admin_endpoint_requires_a_session(client, db_conn) -> None:
    import re
    import uuid

    from app.main import app as default_app
    # Enumerated from the OpenAPI schema (included routers are wrapped in this FastAPI version).
    routes = [(m.upper(), path) for path, ops in default_app.openapi()["paths"].items()
              if path.startswith("/api/admin") for m in ops if m.upper() in ("GET", "POST", "PATCH")]
    open_routes = {("POST", "/api/admin/login"), ("POST", "/api/admin/logout")}
    checked = 0
    for method, path in routes:
        if (method, path) in open_routes:
            continue
        url = re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path)
        response = client.request(method, url, headers=XHR, json={} if method != "GET" else None)
        assert response.status_code == 401, (method, path, response.status_code)
        checked += 1
    assert checked >= 16


# ------------------------------------------------------------------- CSRF
def test_mutations_require_custom_header(client, db_conn) -> None:
    logged_in(client)
    response = client.post("/api/admin/clients", json={"name": "X"})
    assert (response.status_code, response.json()["code"]) == (403, "csrf_header_missing")
    response = client.post("/api/admin/login", json={"password": PASSWORD})
    assert response.status_code == 403  # login is protected too (login CSRF)
    assert client.get("/api/admin/dashboard").status_code == 200  # reads need no header


def test_foreign_origin_is_rejected_and_own_origin_accepted(client, db_conn) -> None:
    logged_in(client)
    bad = client.post("/api/admin/clients", json={"name": "X"}, headers={**XHR, "Origin": "https://evil.example"})
    assert (bad.status_code, bad.json()["code"]) == (403, "origin_not_allowed")
    good = client.post("/api/admin/clients", json={"name": "Y"}, headers={**XHR, "Origin": "https://hub.example.test"})
    assert good.status_code == 201


def test_non_json_bodies_are_rejected(client, db_conn) -> None:
    logged_in(client)
    response = client.post("/api/admin/clients", content=b"name=X",
                           headers={**XHR, "Content-Type": "application/x-www-form-urlencoded"})
    assert (response.status_code, response.json()["code"]) == (415, "json_required")


def test_no_cors_headers_are_emitted(client, db_conn) -> None:
    response = client.options("/api/admin/clients", headers={"Origin": "https://evil.example",
                                                            "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in {k.lower() for k in response.headers}


# ------------------------------------------------------------- throttling
def test_five_failures_lock_out_the_source(client, db_conn) -> None:
    for _ in range(5):
        assert login(client, "wrong").status_code == 401
    blocked = login(client, "wrong")
    assert (blocked.status_code, blocked.json()["code"]) == (429, "login_throttled")
    assert 1 <= int(blocked.headers["retry-after"]) <= 900
    assert login(client).status_code == 429  # even the right password, while locked
    db_conn.execute("UPDATE admin_login_attempts SET created_at = created_at - interval '16 minutes'")
    assert login(client).status_code == 200  # window passed


def test_success_resets_the_source_counter(client, db_conn) -> None:
    for _ in range(4):
        login(client, "wrong")
    assert login(client).status_code == 200
    for _ in range(4):
        assert login(client, "wrong").status_code == 401
    assert login(client).status_code == 200


def test_global_failure_cap_stops_distributed_guessing(client, db_conn) -> None:
    db_conn.execute("INSERT INTO admin_login_attempts (source_key, succeeded) "
                    "SELECT '10.0.0.' || g, false FROM generate_series(1, 50) g")
    assert login(client).status_code == 429


def test_forwarded_for_header_is_ignored(client, db_conn) -> None:
    for i in range(5):
        login(client, "wrong")
    spoofed = client.post("/api/admin/login", json={"password": PASSWORD},
                          headers={**XHR, "X-Forwarded-For": "203.0.113.9"})
    assert spoofed.status_code == 429
    assert {r["source_key"] for r in db_conn.execute("SELECT source_key FROM admin_login_attempts").fetchall()} == {"testclient"}


def test_secrets_never_logged(client, db_conn, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    login(client, "wrong-guess-123")
    logged_in(client)
    raw = client.cookies.get("ltd_admin_session")
    client.post("/api/admin/logout", headers=XHR)
    for secret in (PASSWORD, "wrong-guess-123", raw, hash_token(raw)):
        assert secret not in caplog.text
