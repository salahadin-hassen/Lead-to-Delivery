"""Helpers for administrator API tests: a seeded administrator with MFA."""

import base64
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from fastapi.testclient import TestClient

from app import mfa
from app.main import create_app
from app.security import hash_password
from tests.conftest import make_settings
from tests.crm_helpers import BASE_URL, SECRET

EMAIL = "admin@agency.example"
NAME = "Test Admin"
PASSWORD = "correct horse battery staple"
PASSWORD_HASH = hash_password(PASSWORD)  # computed once: scrypt is deliberately slow
# Test-only values, never used outside this suite.
MFA_KEY = base64.b64encode(bytes(range(32))).decode("ascii")
TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
RECOVERY_CODES = ["abcd-efgh-jkmn", "pqrs-tuvw-xyz2", "3456-789a-bcde"]
XHR = {"X-Requested-With": "XMLHttpRequest"}


def seed_admin(database_url: str, *, email: str = EMAIL, name: str = NAME, totp_secret: str = TOTP_SECRET,
               recovery_codes: list[str] | tuple[str, ...] = tuple(RECOVERY_CODES)) -> str:
    key = mfa.parse_key(MFA_KEY)
    with psycopg.connect(database_url, autocommit=True) as conn:
        user_id = conn.execute(
            "INSERT INTO admin_users (email, display_name, password_hash, totp_secret_enc) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email RETURNING id",
            (email, name, PASSWORD_HASH, mfa.encrypt_secret(totp_secret, key)),
        ).fetchone()[0]
        for code in recovery_codes:
            conn.execute("INSERT INTO admin_recovery_codes (admin_user_id, code_hash) VALUES (%s, %s) "
                         "ON CONFLICT DO NOTHING", (user_id, mfa.hash_recovery_code(code)))
    return str(user_id)


@contextmanager
def admin_app(database_url: str, *, seed: bool = True, **overrides: Any) -> Iterator[TestClient]:
    values = {"crm_webhook_secret": SECRET, "app_base_url": BASE_URL, "admin_mfa_encryption_key": MFA_KEY,
              **overrides}
    if seed:
        seed_admin(database_url)
    app = create_app(make_settings(database_url, **values))
    with TestClient(app, raise_server_exceptions=False) as client:
        client.ltd_database_url = database_url  # type: ignore[attr-defined]
        yield client


def fresh_code(client: TestClient, email: str = EMAIL, secret: str = TOTP_SECRET) -> str:
    """A TOTP code the server will accept now: allow the current step again
    (a real authenticator would wait for the next 30-second step)."""
    with psycopg.connect(client.ltd_database_url, autocommit=True) as conn:  # type: ignore[attr-defined]
        conn.execute("UPDATE admin_users SET totp_last_step = 0 WHERE email = %s", (email,))
    return mfa.totp_code(secret)


def login(client: TestClient, password: str = PASSWORD, *, code: str | None = None, email: str = EMAIL,
          headers: dict[str, str] | None = None):
    body = {"email": email, "password": password, "code": code if code is not None else fresh_code(client, email)}
    return client.post("/api/admin/login", json=body, headers=headers or XHR)


def logged_in(client: TestClient) -> TestClient:
    response = login(client)
    assert response.status_code == 200, response.text
    return client


def post(client: TestClient, path: str, body: dict | None = None):
    return client.post(path, json=body, headers=XHR) if body is not None else client.post(path, headers=XHR)


def patch(client: TestClient, path: str, body: dict):
    return client.patch(path, json=body, headers=XHR)
