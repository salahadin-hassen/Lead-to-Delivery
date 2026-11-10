"""Helpers for CRM webhook tests: payloads, signing, row counts."""

import copy
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi.testclient import TestClient

from app.main import create_app
from app.security import webhook_signature
from tests.conftest import make_settings

SECRET = "test-webhook-secret-" + "x" * 32
BASE_URL = "https://hub.example.test"
URL = "/api/webhooks/crm/deal-won"

INTAKE_TABLES = ("webhook_events", "clients", "onboarding_invitations", "client_access_tokens", "activity_events")


def payload(**overrides: Any) -> dict[str, Any]:
    """A valid normalized deal.won payload; nested keys via 'deal'/'contact' dicts."""
    data: dict[str, Any] = {
        "event_id": "evt_2026_0001",
        "event_type": "deal.won",
        "occurred_at": "2026-11-07T18:00:00Z",
        "source": "generic",
        "deal": {
            "external_id": "deal_123",
            "name": "Website Development Project",
            "value": 2500,
            "currency": "USD",
            "service_type": "website_build",
        },
        "contact": {
            "external_id": "contact_456",
            "name": "Demo Client",
            "email": "client@example.com",
            "company": "Example Company",
            "phone": "+10000000000",
        },
    }
    for key, value in overrides.items():
        if key in ("deal", "contact"):
            data[key] = {**data[key], **value}
        else:
            data[key] = value
    return copy.deepcopy(data)


def encode(data: dict[str, Any]) -> bytes:
    return json.dumps(data).encode("utf-8")


def signed_headers(body: bytes, *, secret: str = SECRET, timestamp: int | None = None) -> dict[str, str]:
    ts = str(int(time.time()) if timestamp is None else timestamp)
    return {
        "Content-Type": "application/json",
        "X-LTD-Timestamp": ts,
        "X-LTD-Signature": webhook_signature(secret, ts, body),
    }


def post(client: TestClient, data: dict[str, Any] | bytes, **header_kwargs: Any):
    body = data if isinstance(data, bytes) else encode(data)
    return client.post(URL, content=body, headers=signed_headers(body, **header_kwargs))


def counts(conn) -> dict[str, int]:
    return {
        table: conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
        for table in INTAKE_TABLES
    }


ZERO = {table: 0 for table in INTAKE_TABLES}


@contextmanager
def webhook_client(database_url: str, **settings_overrides: Any) -> Iterator[TestClient]:
    values = {"crm_webhook_secret": SECRET, "app_base_url": BASE_URL, **settings_overrides}
    app = create_app(make_settings(database_url, **values))
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
