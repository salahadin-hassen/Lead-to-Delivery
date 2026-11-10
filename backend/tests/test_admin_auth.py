"""Administrator accounts: login with password + MFA, sessions, logout,
CSRF, throttling, account recovery tooling and MFA primitives."""

import base64
import logging

import pytest

from app import admin_users, mfa
from app.main import create_app
from app.security import hash_token
from tests.admin_helpers import (
    EMAIL, MFA_KEY, PASSWORD, RECOVERY_CODES, TOTP_SECRET, XHR, admin_app, fresh_code, logged_in, login, seed_admin,
)
from tests.conftest import make_settings

INVALID = {"detail": "Incorrect email, password or code.", "code": "invalid_credentials"}


@pytest.fixture
def client(db_conn, migrated_database, pool_cleanup):
    with admin_app(migrated_database) as c:
        yield c


def _session_rows(conn):
    return conn.execute("SELECT * FROM admin_sessions ORDER BY created_at").fetchall()


# ------------------------------------------------------------------ login
def test_password_and_totp_create_a_session_for_that_admin(client, db_conn) -> None:
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
    user = db_conn.execute("SELECT id, last_login_at FROM admin_users").fetchone()
    assert row["token_hash"] == hash_token(raw) and raw not in str(row)
    assert row["admin_user_id"] == user["id"] and user["last_login_at"] is not None
    hours = (row["expires_at"] - row["created_at"]).total_seconds() / 3600
    assert 11.99 < hours <= 12.0
    me = client.get("/api/admin/me")
    assert me.status_code == 200
    assert me.json()["admin"] == {"email": EMAIL, "display_name": "Test Admin"}


def test_cookie_is_secure_in_production(db_conn, migrated_database, pool_cleanup) -> None:
    with admin_app(migrated_database, app_env="production", app_base_url="https://hub.example.test",
                   crm_webhook_secret="s" * 40) as client:
        cookie = login(client, headers={**XHR, "Host": "hub.example.test"}).headers["set-cookie"]
    assert "Secure" in cookie


@pytest.mark.parametrize("case", ["wrong_password", "wrong_code", "unknown_email", "disabled", "other_secret"])
def test_every_failure_is_the_same_generic_401(client, db_conn, case) -> None:
    if case == "disabled":
        db_conn.execute("UPDATE admin_users SET disabled_at = now()")
    kwargs = {
        "wrong_password": {"password": "wrong password"},
        "wrong_code": {"code": "000000" if fresh_code(client) != "000000" else "111111"},
        "unknown_email": {"email": "nobody@agency.example"},
        "disabled": {},
        "other_secret": {"code": mfa.totp_code(mfa.new_totp_secret())},
    }[case]
    response = login(client, **kwargs)
    assert (response.status_code, response.json()) == (401, INVALID)
    assert "set-cookie" not in response.headers
    assert _session_rows(db_conn) == []


def test_email_is_case_insensitive(client, db_conn) -> None:
    assert login(client, email="  Admin@Agency.Example ").status_code == 200


def test_a_totp_code_is_accepted_only_once(client, db_conn) -> None:
    code = fresh_code(client)
    assert login(client, code=code).status_code == 200
    replay = login(client, code=code)
    assert (replay.status_code, replay.json()) == (401, INVALID)


def test_recovery_code_works_once_and_only_for_its_own_account(client, db_conn, migrated_database) -> None:
    seed_admin(migrated_database, email="other@agency.example", recovery_codes=["zzzz-zzzz-zzzz"])
    assert login(client, code="zzzz-zzzz-zzzz").status_code == 401  # another admin's code
    assert login(client, code=RECOVERY_CODES[0].upper().replace("-", " ")).status_code == 200
    assert login(client, code=RECOVERY_CODES[0]).status_code == 401  # used
    used = db_conn.execute("SELECT count(*) AS n FROM admin_recovery_codes WHERE used_at IS NOT NULL").fetchone()
    assert used["n"] == 1


def test_missing_mfa_key_refuses_login(db_conn, migrated_database, pool_cleanup) -> None:
    with admin_app(migrated_database, admin_mfa_encryption_key=None) as client:
        response = login(client)
        assert (response.status_code, response.json()["code"]) == (503, "admin_not_configured")
        assert client.get("/api/admin/dashboard").status_code == 401


def test_wrong_mfa_key_refuses_login_without_a_500(db_conn, migrated_database, pool_cleanup) -> None:
    other = base64.b64encode(b"x" * 32).decode()
    with admin_app(migrated_database, admin_mfa_encryption_key=other) as client:
        response = login(client)
    assert (response.status_code, response.json()["code"]) == (503, "admin_not_configured")
    assert _session_rows(db_conn) == []


@pytest.mark.parametrize("bad", ["not-base64!!", base64.b64encode(b"short").decode()])
def test_malformed_mfa_key_prevents_startup(migrated_database, bad) -> None:
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        create_app(make_settings(migrated_database, admin_mfa_encryption_key=bad))


def test_production_requires_the_mfa_key(migrated_database) -> None:
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="ADMIN_MFA_ENCRYPTION_KEY is required"):
        make_settings(migrated_database, app_env="production", app_base_url="https://hub.example.test",
                      crm_webhook_secret="s" * 40)


def test_login_body_is_validated(client, db_conn) -> None:
    assert client.post("/api/admin/login", json={"email": EMAIL, "password": PASSWORD}, headers=XHR).status_code == 422
    r = client.post("/api/admin/login", json={"email": EMAIL, "password": PASSWORD, "code": "123456", "admin": True},
                    headers=XHR)
    assert r.status_code == 422 and PASSWORD not in r.text  # input never echoed
    old_style = client.post("/api/admin/login", json={"password": PASSWORD}, headers=XHR)
    assert old_style.status_code == 422  # the shared-password login no longer exists


# -------------------------------------------------------------- sessions
def test_expired_session_is_rejected(client, db_conn) -> None:
    logged_in(client)
    db_conn.execute("UPDATE admin_sessions SET expires_at = now() - interval '1 second'")
    response = client.get("/api/admin/me")
    assert (response.status_code, response.json()["code"]) == (401, "admin_auth_required")


def test_idle_session_is_rejected(client, db_conn) -> None:
    logged_in(client)
    assert client.get("/api/admin/me").status_code == 200
    db_conn.execute("UPDATE admin_sessions SET last_seen_at = now() - interval '61 minutes'")
    assert client.get("/api/admin/me").status_code == 401


def test_revoked_session_is_rejected(client, db_conn) -> None:
    logged_in(client)
    db_conn.execute("UPDATE admin_sessions SET revoked_at = now()")
    assert client.get("/api/admin/dashboard").status_code == 401


def test_disabling_an_admin_ends_their_sessions_at_once(client, db_conn) -> None:
    logged_in(client)
    db_conn.execute("UPDATE admin_users SET disabled_at = now()")
    assert client.get("/api/admin/me").status_code == 401


def test_session_without_an_owner_is_rejected(client, db_conn) -> None:
    """Shared-password sessions (before migration 0005) have no administrator."""
    raw = "L" * 43
    db_conn.execute("INSERT INTO admin_sessions (token_hash, expires_at) VALUES (%s, now() + interval '1 hour')",
                    (hash_token(raw),))
    client.cookies.set("ltd_admin_session", raw, path="/api/admin")
    assert client.get("/api/admin/me").status_code == 401


def test_each_login_issues_a_new_session_token(client, db_conn) -> None:
    logged_in(client)
    first = client.cookies.get("ltd_admin_session")
    client.cookies.clear()
    logged_in(client)
    assert client.cookies.get("ltd_admin_session") != first
    assert len(_session_rows(db_conn)) == 2


def test_forged_cookie_is_rejected(client, db_conn) -> None:
    client.cookies.set("ltd_admin_session", "A" * 43, path="/api/admin")
    assert client.get("/api/admin/me").status_code == 401


def test_portal_token_cannot_reach_admin_endpoints(client, db_conn) -> None:
    from tests.portal_helpers import new_invitation
    token = new_invitation(client)["token"]
    for path in ("/api/admin/me", "/api/admin/dashboard", "/api/admin/clients"):
        assert client.get(path, headers={"Authorization": f"Bearer {token}"}).status_code == 401
    client.cookies.set("ltd_admin_session", token, path="/api/admin")
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


def test_logout_all_ends_every_session_of_this_admin_only(client, db_conn, migrated_database) -> None:
    other_id = seed_admin(migrated_database, email="other@agency.example", recovery_codes=[])
    db_conn.execute("INSERT INTO admin_sessions (token_hash, expires_at, admin_user_id) "
                    "VALUES (%s, now() + interval '1 hour', %s)", (hash_token("O" * 43), other_id))
    logged_in(client)
    first = client.cookies.get("ltd_admin_session")
    client.cookies.clear()
    logged_in(client)
    assert client.post("/api/admin/logout-all", headers=XHR).status_code == 204
    client.cookies.set("ltd_admin_session", first, path="/api/admin")
    assert client.get("/api/admin/me").status_code == 401
    live = db_conn.execute("SELECT admin_user_id FROM admin_sessions WHERE revoked_at IS NULL").fetchall()
    assert [str(r["admin_user_id"]) for r in live] == [other_id]


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
    assert checked >= 17


# ------------------------------------------------------------------- CSRF
def test_mutations_require_custom_header(client, db_conn) -> None:
    logged_in(client)
    response = client.post("/api/admin/clients", json={"name": "X"})
    assert (response.status_code, response.json()["code"]) == (403, "csrf_header_missing")
    response = client.post("/api/admin/login", json={"email": EMAIL, "password": PASSWORD, "code": "123456"})
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
    assert login(client).status_code == 429  # even the right credentials, while locked
    db_conn.execute("UPDATE admin_login_attempts SET created_at = created_at - interval '16 minutes'")
    assert login(client).status_code == 200  # window passed


def test_five_failures_lock_the_account_from_any_source(client, db_conn) -> None:
    db_conn.execute("INSERT INTO admin_login_attempts (source_key, succeeded, account_key) "
                    "SELECT '198.51.100.' || g, false, %s FROM generate_series(1, 5) g", (EMAIL,))
    assert login(client).status_code == 429
    assert login(client, email="other@agency.example").status_code == 401  # other accounts unaffected


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
    for _ in range(5):
        login(client, "wrong")
    spoofed = login(client, headers={**XHR, "X-Forwarded-For": "203.0.113.9"})
    assert spoofed.status_code == 429
    sources = {r["source_key"] for r in db_conn.execute("SELECT source_key FROM admin_login_attempts").fetchall()}
    assert sources == {"testclient"}


def test_secrets_never_logged(client, db_conn, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    login(client, "wrong-guess-123")
    code = fresh_code(client)
    login(client, code=code)
    raw = client.cookies.get("ltd_admin_session")
    login(client, code=RECOVERY_CODES[1])
    client.post("/api/admin/logout", headers=XHR)
    for secret in (PASSWORD, "wrong-guess-123", raw, hash_token(raw), code, RECOVERY_CODES[1], TOTP_SECRET, MFA_KEY):
        assert secret not in caplog.text
    assert "recovery code" in caplog.text  # its use is logged (without the code)


def test_totp_secret_is_encrypted_at_rest(client, db_conn) -> None:
    row = db_conn.execute("SELECT totp_secret_enc FROM admin_users").fetchone()
    assert TOTP_SECRET not in row["totp_secret_enc"] and row["totp_secret_enc"].startswith("v1:")
    codes = db_conn.execute("SELECT code_hash FROM admin_recovery_codes").fetchall()
    assert all(r["code_hash"] not in RECOVERY_CODES and len(r["code_hash"]) == 64 for r in codes)


# --------------------------------------------------- account tool (CLI core)
@pytest.fixture
def settings(db_conn, migrated_database, pool_cleanup):
    from app import db
    s = make_settings(migrated_database, admin_mfa_encryption_key=MFA_KEY)
    db.configure_pool(migrated_database)
    return s


def test_create_admin_requires_a_confirmed_authenticator(settings, db_conn) -> None:
    secret = mfa.new_totp_secret()
    with pytest.raises(admin_users.AdminUserError, match="not correct"):
        admin_users.create_admin(settings, email="new@agency.example", display_name="New", password="a long password!",
                                 totp_secret=secret, confirmation_code="000000" if mfa.totp_code(secret) != "000000" else "111111")
    assert db_conn.execute("SELECT count(*) AS n FROM admin_users").fetchone()["n"] == 0
    codes = admin_users.create_admin(settings, email="New@Agency.example", display_name="New",
                                     password="a long password!", totp_secret=secret,
                                     confirmation_code=mfa.totp_code(secret))
    assert len(codes) == 10 and len(set(codes)) == 10
    row = db_conn.execute("SELECT email, totp_last_step FROM admin_users").fetchone()
    assert row["email"] == "new@agency.example" and row["totp_last_step"] == mfa.current_step()


@pytest.mark.parametrize("email,password,message", [
    ("new@agency.example", "short", "at least 12"),
    ("new@agency.example", "new@agency.example", "must not be the email"),
    ("not-an-email", "a long password!", "valid email"),
])
def test_create_admin_refuses_weak_input(settings, db_conn, email, password, message) -> None:
    secret = mfa.new_totp_secret()
    with pytest.raises(admin_users.AdminUserError, match=message):
        admin_users.create_admin(settings, email=email, display_name="N", password=password, totp_secret=secret,
                                 confirmation_code=mfa.totp_code(secret))


def test_create_admin_refuses_a_duplicate_email(settings, db_conn, migrated_database) -> None:
    seed_admin(migrated_database)
    secret = mfa.new_totp_secret()
    with pytest.raises(admin_users.AdminUserError, match="already exists"):
        admin_users.create_admin(settings, email=EMAIL, display_name="N", password="a long password!",
                                 totp_secret=secret, confirmation_code=mfa.totp_code(secret))


def test_recovery_tools_revoke_sessions_and_replace_codes(settings, db_conn, migrated_database) -> None:
    user_id = seed_admin(migrated_database)
    db_conn.execute("INSERT INTO admin_sessions (token_hash, expires_at, admin_user_id) "
                    "VALUES (%s, now() + interval '1 hour', %s)", (hash_token("S" * 43), user_id))
    assert admin_users.reset_password(settings, email=EMAIL, password="another long one") == 1
    new = admin_users.new_recovery_codes(settings, email=EMAIL)
    hashes = {r["code_hash"] for r in db_conn.execute("SELECT code_hash FROM admin_recovery_codes").fetchall()}
    assert hashes == {mfa.hash_recovery_code(c) for c in new}  # old codes gone
    secret = mfa.new_totp_secret()
    admin_users.reset_mfa(settings, email=EMAIL, totp_secret=secret, confirmation_code=mfa.totp_code(secret))
    admin_users.set_disabled(settings, email=EMAIL, disabled=True)
    assert db_conn.execute("SELECT disabled_at FROM admin_users").fetchone()["disabled_at"] is not None
    with pytest.raises(admin_users.AdminUserError, match="No administrator"):
        admin_users.revoke_sessions(settings, email="nobody@agency.example")


def test_cli_never_takes_a_password_argument() -> None:
    with pytest.raises(SystemExit):
        admin_users.main(["create", "--email", EMAIL, "--name", "X", "--password", "secret"])


# ------------------------------------------------------------- primitives
def test_totp_matches_rfc_6238() -> None:
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert mfa.totp_code(secret, step=59 // 30) == "287082"          # RFC 6238 T=59 (last 6 of 94287082)
    assert mfa.totp_code(secret, step=1111111109 // 30) == "081804"  # T=1111111109 (07081804)
    assert mfa.verify_totp(secret, "287082", last_step=0, now=59) == 1
    assert mfa.verify_totp(secret, "287082", last_step=1, now=59) is None  # replay
    assert mfa.verify_totp(secret, "287082", last_step=0, now=59 + 90) is None  # too old


def test_secret_encryption_round_trip_and_tamper_detection() -> None:
    key = mfa.parse_key(MFA_KEY)
    sealed = mfa.encrypt_secret(TOTP_SECRET, key)
    assert mfa.decrypt_secret(sealed, key) == TOTP_SECRET
    assert mfa.encrypt_secret(TOTP_SECRET, key) != sealed  # fresh nonce
    with pytest.raises(mfa.MfaKeyError):
        mfa.decrypt_secret(sealed, b"y" * 32)
    tampered = sealed[:-4] + ("AAAA" if not sealed.endswith("AAAA") else "BBBB")
    with pytest.raises(mfa.MfaKeyError):
        mfa.decrypt_secret(tampered, key)


def test_recovery_codes_are_well_formed() -> None:
    codes = mfa.new_recovery_codes()
    assert len(set(codes)) == 10
    assert all(mfa.normalize_recovery_code(c) and len(c) == 14 for c in codes)
    assert mfa.normalize_recovery_code("123456") is None
