"""Helpers for administrator API tests."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi.testclient import TestClient

from app.main import create_app
from app.security import hash_password
from tests.conftest import make_settings
from tests.crm_helpers import BASE_URL, SECRET

PASSWORD = "correct horse battery staple"
PASSWORD_HASH = hash_password(PASSWORD)  # computed once: scrypt is deliberately slow
XHR = {"X-Requested-With": "XMLHttpRequest"}


@contextmanager
def admin_app(database_url: str, **overrides: Any) -> Iterator[TestClient]:
    values = {"crm_webhook_secret": SECRET, "app_base_url": BASE_URL, "admin_password_hash": PASSWORD_HASH,
              **overrides}
    app = create_app(make_settings(database_url, **values))
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def login(client: TestClient, password: str = PASSWORD):
    return client.post("/api/admin/login", json={"password": password}, headers=XHR)


def logged_in(client: TestClient) -> TestClient:
    response = login(client)
    assert response.status_code == 200, response.text
    return client


def post(client: TestClient, path: str, body: dict | None = None):
    return client.post(path, json=body, headers=XHR) if body is not None else client.post(path, headers=XHR)


def patch(client: TestClient, path: str, body: dict):
    return client.patch(path, json=body, headers=XHR)
