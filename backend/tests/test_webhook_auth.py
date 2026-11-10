"""Webhook authentication and request validation happen before any database write."""

import logging
import time

import pytest

from app.security import webhook_signature
from tests.crm_helpers import (
    SECRET,
    URL,
    ZERO,
    counts,
    encode,
    payload,
    post,
    signed_headers,
    webhook_client,
)


@pytest.fixture
def client(db_conn, migrated_database, pool_cleanup):
    with webhook_client(migrated_database) as c:
        yield c


def test_valid_signature_is_accepted(client, db_conn) -> None:
    response = post(client, payload())
    assert response.status_code == 201, response.text


def test_invalid_signature_is_rejected_without_records(client, db_conn) -> None:
    response = post(client, payload(), secret="wrong-secret-" + "y" * 32)
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_signature"
    assert counts(db_conn) == ZERO


def test_missing_signature_headers(client, db_conn) -> None:
    body = encode(payload())
    headers = signed_headers(body)
    for missing in ("X-LTD-Signature", "X-LTD-Timestamp"):
        partial = {k: v for k, v in headers.items() if k != missing}
        response = client.post(URL, content=body, headers=partial)
        assert response.status_code == 401
        assert response.json()["code"] == "missing_signature"
    assert counts(db_conn) == ZERO


@pytest.mark.parametrize(
    ("signature", "code"),
    [
        ("deadbeef", "malformed_signature"),
        ("sha256=xyz", "malformed_signature"),
        ("sha1=" + "a" * 40, "malformed_signature"),
        ("sha256=" + "a" * 63, "malformed_signature"),
        ("sha256=" + "a" * 64, "invalid_signature"),
    ],
)
def test_malformed_or_wrong_signature(client, db_conn, signature: str, code: str) -> None:
    body = encode(payload())
    headers = {**signed_headers(body), "X-LTD-Signature": signature}
    response = client.post(URL, content=body, headers=headers)
    assert (response.status_code, response.json()["code"]) == (401, code)
    assert counts(db_conn) == ZERO


@pytest.mark.parametrize("timestamp", ["abc", "-5", "1.5", ""])
def test_malformed_timestamp(client, db_conn, timestamp: str) -> None:
    body = encode(payload())
    headers = {**signed_headers(body), "X-LTD-Timestamp": timestamp}
    response = client.post(URL, content=body, headers=headers)
    assert response.status_code == 401
    assert counts(db_conn) == ZERO


# HTTP tests keep a margin from the 300 s edge (wall clock moves between test
# and server); the exact boundary is tested with an injected clock below.
@pytest.mark.parametrize("offset", [-305, 305, -86400, 86400])
def test_timestamp_outside_five_minute_window(client, db_conn, offset: int) -> None:
    response = post(client, payload(), timestamp=int(time.time()) + offset)
    assert (response.status_code, response.json()["code"]) == (401, "timestamp_out_of_window")
    assert counts(db_conn) == ZERO


@pytest.mark.parametrize("offset", [-295, 295])
def test_timestamp_inside_window_is_accepted(client, db_conn, offset: int) -> None:
    response = post(client, payload(), timestamp=int(time.time()) + offset)
    assert response.status_code == 201


def test_signature_covers_exact_raw_bytes(client, db_conn) -> None:
    original = encode(payload())
    reformatted = original.replace(b'", "', b'","')  # same JSON, different bytes
    assert reformatted != original
    headers = signed_headers(original)
    response = client.post(URL, content=reformatted, headers=headers)
    assert (response.status_code, response.json()["code"]) == (401, "invalid_signature")
    assert counts(db_conn) == ZERO


@pytest.mark.parametrize("secret", [None, "too-short-secret"])
def test_unconfigured_or_weak_secret_returns_503(db_conn, migrated_database, pool_cleanup, secret) -> None:
    with webhook_client(migrated_database, crm_webhook_secret=secret) as client:
        body = encode(payload())
        # Even a request "signed" with the configured weak secret is refused.
        response = client.post(URL, content=body, headers=signed_headers(body, secret=secret or "x"))
    assert (response.status_code, response.json()["code"]) == (503, "webhook_not_configured")
    assert counts(db_conn) == ZERO


def test_malformed_json_with_valid_signature_is_422(client, db_conn) -> None:
    body = b'{"event_id": "evt_1", "event_type": '
    response = client.post(URL, content=body, headers=signed_headers(body))
    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "json_invalid"
    assert counts(db_conn) == ZERO


def test_non_object_json_is_422(client, db_conn) -> None:
    body = b"[1, 2, 3]"
    response = client.post(URL, content=body, headers=signed_headers(body))
    assert response.status_code == 422
    assert counts(db_conn) == ZERO


def test_oversized_body_is_413(client, db_conn) -> None:
    data = payload(deal={"name": "x" * 70_000})
    response = post(client, data)
    assert (response.status_code, response.json()["code"]) == (413, "payload_too_large")
    assert counts(db_conn) == ZERO


def test_oversized_chunked_body_without_content_length_is_413(client, db_conn) -> None:
    """The streaming cap applies even when no Content-Length is declared."""
    body = encode(payload(deal={"name": "x" * 70_000}))
    headers = signed_headers(body)

    def chunks():
        for i in range(0, len(body), 8192):
            yield body[i:i + 8192]

    response = client.post(URL, content=chunks(), headers=headers)
    assert "content-length" not in {k.lower() for k in response.request.headers}
    assert (response.status_code, response.json()["code"]) == (413, "payload_too_large")
    assert counts(db_conn) == ZERO


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", ""])
def test_wrong_content_type_is_415(client, db_conn, content_type: str) -> None:
    body = encode(payload())
    headers = {**signed_headers(body), "Content-Type": content_type}
    response = client.post(URL, content=body, headers=headers)
    assert (response.status_code, response.json()["code"]) == (415, "unsupported_media_type")
    assert counts(db_conn) == ZERO


def test_content_type_with_charset_is_accepted(client, db_conn) -> None:
    body = encode(payload())
    headers = {**signed_headers(body), "Content-Type": "application/json; charset=utf-8"}
    assert client.post(URL, content=body, headers=headers).status_code == 201


@pytest.mark.parametrize(
    ("overrides", "loc"),
    [
        ({"event_type": "deal.lost"}, ["body", "event_type"]),
        ({"occurred_at": "2026-11-07T18:00:00"}, ["body", "occurred_at"]),
        ({"source": "salesforce"}, ["body", "source"]),
        ({"deal": {"value": -1}}, ["body", "deal", "value"]),
        ({"deal": {"currency": "US"}}, ["body", "deal", "currency"]),
        ({"deal": {"currency": None}}, ["body", "deal"]),
        ({"deal": {"external_id": "  "}}, ["body", "deal", "external_id"]),
        ({"contact": {"email": "not-an-email"}}, ["body", "contact", "email"]),
        ({"contact": {"email": None, "external_id": None}}, ["body", "contact"]),
        ({"unexpected": True}, ["body", "unexpected"]),
    ],
)
def test_invalid_payloads_are_422_without_records(client, db_conn, overrides, loc) -> None:
    response = post(client, payload(**overrides))
    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["loc"] == loc
    assert "input" not in response.json()["detail"][0]  # inputs are never echoed
    assert counts(db_conn) == ZERO


def test_missing_deal_external_id_is_422(client, db_conn) -> None:
    data = payload()
    del data["deal"]["external_id"]
    response = post(client, data)
    assert response.status_code == 422
    assert counts(db_conn) == ZERO


@pytest.mark.parametrize("service_type", ["web_design", "random"])
def test_unknown_service_type_is_422_without_records(client, db_conn, service_type: str) -> None:
    response = post(client, payload(deal={"service_type": service_type}))
    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "unknown_service_type"
    assert counts(db_conn) == ZERO


def test_inactive_template_is_rejected(client, db_conn) -> None:
    db_conn.execute("UPDATE project_templates SET active = false WHERE key = 'seo_retainer'")
    try:
        response = post(client, payload(deal={"service_type": "seo_retainer"}))
        assert response.status_code == 422
        assert counts(db_conn) == ZERO
    finally:
        db_conn.execute("UPDATE project_templates SET active = true WHERE key = 'seo_retainer'")


def test_logs_contain_no_secrets_tokens_or_personal_data(client, db_conn, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    data = payload()
    body = encode(data)
    headers = signed_headers(body)
    response = client.post(URL, content=body, headers=headers)
    assert response.status_code == 201
    token = response.json()["onboarding_url"].split("#t=")[1]
    post(client, payload(), secret="wrong-secret-" + "z" * 32)  # an auth failure is logged too
    text = caplog.text
    for secret_value in (SECRET, token, headers["X-LTD-Signature"].removeprefix("sha256="),
                         data["contact"]["email"], data["contact"]["phone"]):
        assert secret_value not in text
    assert webhook_signature(SECRET, headers["X-LTD-Timestamp"], body) not in text


@pytest.mark.parametrize(("delta", "ok"), [(0, True), (300, True), (-300, True), (301, False), (-301, False)])
def test_signature_window_boundary_is_exact(delta: int, ok: bool) -> None:
    from app.errors import WebhookAuthError
    from app.security import verify_webhook_signature

    body = b'{"a": 1}'
    ts = "1700000000"
    signature = webhook_signature(SECRET, ts, body)
    now = 1700000000 + delta
    if ok:
        verify_webhook_signature(secret=SECRET, timestamp=ts, signature=signature, body=body, now=now)
    else:
        with pytest.raises(WebhookAuthError, match="timestamp_out_of_window"):
            verify_webhook_signature(secret=SECRET, timestamp=ts, signature=signature, body=body, now=now)


def test_signature_is_case_insensitive_hex_but_secret_sensitive() -> None:
    from app.errors import WebhookAuthError
    from app.security import verify_webhook_signature

    body, ts = b"{}", "1700000000"
    upper = "sha256=" + webhook_signature(SECRET, ts, body).removeprefix("sha256=").upper()
    verify_webhook_signature(secret=SECRET, timestamp=ts, signature=upper, body=body, now=1700000000)
    with pytest.raises(WebhookAuthError, match="invalid_signature"):
        verify_webhook_signature(secret=SECRET + "!", timestamp=ts, signature=upper, body=body, now=1700000000)
