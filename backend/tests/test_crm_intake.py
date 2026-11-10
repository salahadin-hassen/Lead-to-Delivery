"""CRM deal-won intake: idempotency, client resolution, invitations, tokens,
transactions and concurrency, all against PostgreSQL ltd_test."""

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from app.security import hash_token
from app.services import crm_intake
from app.store import onboarding as onboarding_store
from tests.crm_helpers import (
    BASE_URL,
    INTAKE_TABLES,
    ZERO,
    counts,
    encode,
    payload,
    post,
    signed_headers,
    webhook_client,
    URL,
)


@pytest.fixture
def client(db_conn, migrated_database, pool_cleanup):
    with webhook_client(migrated_database) as c:
        yield c


def _token(response) -> str:
    return response.json()["onboarding_url"].split("#t=", 1)[1]


def _activity_types(conn) -> list[str]:
    return [r["event_type"] for r in conn.execute(
        "SELECT event_type FROM activity_events ORDER BY created_at, event_type").fetchall()]


def _insert_client(conn, **values) -> str:
    cols = ", ".join(values)
    marks = ", ".join(["%s"] * len(values))
    return conn.execute(f"INSERT INTO clients ({cols}) VALUES ({marks}) RETURNING id",
                        tuple(values.values())).fetchone()["id"]


# ------------------------------------------------------------- happy path
def test_first_delivery_creates_client_invitation_token_and_activity(client, db_conn) -> None:
    response = post(client, payload())
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "created"
    assert set(body) == {"status", "client_id", "invitation_id", "onboarding_url", "expires_at"}

    c = db_conn.execute("SELECT * FROM clients").fetchone()
    assert str(c["id"]) == body["client_id"]
    assert (c["name"], c["email"], c["company"], c["phone"]) == (
        "Demo Client", "client@example.com", "Example Company", "+10000000000")
    assert (c["crm_source"], c["crm_external_id"]) == ("generic", "contact_456")

    inv = db_conn.execute(
        "SELECT i.*, t.key AS template_key FROM onboarding_invitations i "
        "JOIN project_templates t ON t.id = i.template_id").fetchone()
    assert str(inv["id"]) == body["invitation_id"]
    assert inv["client_id"] == c["id"]
    assert (inv["crm_source"], inv["crm_deal_id"], inv["deal_name"]) == (
        "generic", "deal_123", "Website Development Project")
    assert (inv["deal_value_cents"], inv["currency"], inv["service_type"]) == (250000, "USD", "website_build")
    assert inv["template_key"] == "website_build"
    assert inv["status"] == "pending" and inv["submitted_at"] is None

    event = db_conn.execute("SELECT * FROM webhook_events").fetchone()
    assert (event["source"], event["external_event_id"], event["status"]) == ("generic", "evt_2026_0001", "processed")
    assert event["error_code"] is None and event["processed_at"] is not None
    assert event["payload"]["contact"]["email"] == "client@example.com"

    assert _activity_types(db_conn) == ["client.created", "crm.deal_received", "onboarding.invitation_created"]
    assert counts(db_conn) == {**ZERO, "webhook_events": 1, "clients": 1, "onboarding_invitations": 1,
                               "client_access_tokens": 1, "activity_events": 3}


# --------------------------------------------------------------- tokens
def test_token_is_hashed_bound_to_client_and_invitation_and_never_stored_raw(client, db_conn) -> None:
    response = post(client, payload())
    token = _token(response)
    assert response.json()["onboarding_url"] == f"{BASE_URL}/portal#t={token}"
    assert len(token) >= 43

    row = db_conn.execute("SELECT * FROM client_access_tokens").fetchone()
    assert row["token_hash"] == hash_token(token)
    assert str(row["client_id"]) == response.json()["client_id"]
    assert str(row["invitation_id"]) == response.json()["invitation_id"]
    assert row["revoked_at"] is None and row["last_used_at"] is None

    # The raw token appears in no column of any table.
    for table in INTAKE_TABLES + ("projects", "onboarding_submissions"):
        dump = db_conn.execute(f"SELECT coalesce(string_agg(t::text, ' '), '') AS d FROM {table} t").fetchone()["d"]
        assert token not in dump, table


def test_invitation_and_token_expire_after_documented_default(client, db_conn) -> None:
    before = datetime.now(UTC)
    response = post(client, payload())
    after = datetime.now(UTC)
    inv = db_conn.execute("SELECT expires_at FROM onboarding_invitations").fetchone()["expires_at"]
    tok = db_conn.execute("SELECT expires_at FROM client_access_tokens").fetchone()["expires_at"]
    ttl = timedelta(days=crm_intake.INVITATION_TTL_DAYS)
    assert before + ttl - timedelta(seconds=5) <= inv <= after + ttl + timedelta(seconds=5)
    assert tok == inv
    assert datetime.fromisoformat(response.json()["expires_at"]) == inv


def test_each_new_deal_gets_its_own_token(client, db_conn) -> None:
    first = post(client, payload())
    second = post(client, payload(event_id="evt_2", deal={"external_id": "deal_999", "service_type": "seo_retainer"}))
    assert second.status_code == 201
    assert _token(first) != _token(second)
    assert second.json()["client_id"] == first.json()["client_id"]
    invs = db_conn.execute(
        "SELECT i.crm_deal_id, t.key FROM onboarding_invitations i JOIN project_templates t ON t.id = i.template_id "
        "ORDER BY i.crm_deal_id").fetchall()
    assert [(r["crm_deal_id"], r["key"]) for r in invs] == [("deal_123", "website_build"), ("deal_999", "seo_retainer")]
    assert counts(db_conn)["client_access_tokens"] == 2


# ------------------------------------------------------ event idempotency
def test_repeated_identical_event_is_duplicate_and_creates_nothing(client, db_conn) -> None:
    assert post(client, payload()).status_code == 201
    after_first = counts(db_conn)
    for _ in range(3):
        response = post(client, payload())
        assert response.status_code == 200
        assert response.json() == {"status": "duplicate", "event_id": "evt_2026_0001"}
    assert counts(db_conn) == after_first
    event = db_conn.execute("SELECT duplicate_count, last_duplicate_at, status FROM webhook_events").fetchone()
    assert event["duplicate_count"] == 3 and event["last_duplicate_at"] is not None
    assert event["status"] == "processed"


def test_equivalent_payload_after_normalisation_is_a_duplicate(client, db_conn) -> None:
    assert post(client, payload()).status_code == 201
    equivalent = payload(occurred_at="2026-11-07T21:00:00+03:00", source="Generic",
                         deal={"value": "2500.00", "currency": "usd"},
                         contact={"email": "  CLIENT@example.com "})
    response = post(client, equivalent)
    assert (response.status_code, response.json()["status"]) == (200, "duplicate")


def test_same_event_id_with_different_payload_is_409_and_changes_nothing(client, db_conn) -> None:
    assert post(client, payload()).status_code == 201
    before = counts(db_conn)
    stored = db_conn.execute("SELECT payload, updated_at FROM webhook_events").fetchone()
    response = post(client, payload(deal={"value": 9999}))
    assert response.status_code == 409
    assert response.json()["code"] == "event_id_reused"
    assert counts(db_conn) == before
    assert db_conn.execute("SELECT payload, updated_at FROM webhook_events").fetchone() == stored


def test_same_event_id_from_another_source_is_a_different_event(client, db_conn) -> None:
    assert post(client, payload()).status_code == 201
    response = post(client, payload(source="hubspot", contact={"email": "other@example.com", "external_id": "c-9"}))
    assert response.status_code == 201


def test_new_event_id_for_already_processed_deal_is_duplicate_deal(client, db_conn) -> None:
    first = post(client, payload())
    before = counts(db_conn)
    response = post(client, payload(event_id="evt_2026_0002"))
    assert response.status_code == 200
    assert response.json() == {"status": "duplicate_deal", "event_id": "evt_2026_0002"}
    after = counts(db_conn)
    assert after == {**before, "webhook_events": before["webhook_events"] + 1}
    rows = db_conn.execute(
        "SELECT external_event_id, status, error_code FROM webhook_events ORDER BY external_event_id").fetchall()
    assert [(r["external_event_id"], r["status"], r["error_code"]) for r in rows] == [
        ("evt_2026_0001", "processed", None), ("evt_2026_0002", "rejected", "duplicate_deal")]
    # The original invitation is untouched.
    inv = db_conn.execute("SELECT id, status FROM onboarding_invitations").fetchone()
    assert str(inv["id"]) == first.json()["invitation_id"] and inv["status"] == "pending"
    # Replaying the duplicate-deal event is a plain duplicate.
    replay = post(client, payload(event_id="evt_2026_0002"))
    assert replay.json() == {"status": "duplicate", "event_id": "evt_2026_0002"}


def test_duplicate_deal_with_changed_contact_does_not_create_a_client(client, db_conn) -> None:
    post(client, payload())
    response = post(client, payload(event_id="evt_x", contact={"external_id": "someone_else",
                                                                "email": "new@example.com"}))
    assert response.json()["status"] == "duplicate_deal"
    assert counts(db_conn)["clients"] == 1


# ------------------------------------------------------- client resolution
def test_existing_client_matched_by_crm_external_id(client, db_conn) -> None:
    existing = _insert_client(db_conn, name="Old Name", email="client@example.com", company="Example Company",
                              crm_source="generic", crm_external_id="contact_456")
    response = post(client, payload(contact={"email": None}))
    assert response.status_code == 201
    assert response.json()["client_id"] == str(existing)
    assert counts(db_conn)["clients"] == 1
    data = db_conn.execute("SELECT data FROM activity_events WHERE event_type = 'client.matched'").fetchone()["data"]
    assert data["matched_by"] == "crm_external_id"
    # Profile fields that already exist are preserved.
    assert db_conn.execute("SELECT name FROM clients").fetchone()["name"] == "Old Name"


def test_existing_client_matched_by_email_attaches_missing_crm_identity(client, db_conn) -> None:
    existing = _insert_client(db_conn, name="Demo Client", email="client@example.com")
    response = post(client, payload(contact={"email": "Client@Example.com"}))
    assert response.json()["client_id"] == str(existing)
    row = db_conn.execute("SELECT crm_source, crm_external_id, company, phone FROM clients").fetchone()
    assert row == {"crm_source": "generic", "crm_external_id": "contact_456",
                   "company": "Example Company", "phone": "+10000000000"}
    data = db_conn.execute("SELECT data FROM activity_events WHERE event_type = 'client.matched'").fetchone()["data"]
    assert data["matched_by"] == "email"


def test_client_from_another_crm_keeps_its_identity(client, db_conn) -> None:
    existing = _insert_client(db_conn, name="Demo", email="client@example.com",
                              crm_source="hubspot", crm_external_id="hs-1")
    response = post(client, payload())  # source generic, contact_456
    assert response.json()["client_id"] == str(existing)
    row = db_conn.execute("SELECT crm_source, crm_external_id FROM clients").fetchone()
    assert (row["crm_source"], row["crm_external_id"]) == ("hubspot", "hs-1")


def test_email_is_normalised_on_create(client, db_conn) -> None:
    post(client, payload(contact={"email": "  MiXeD@Example.COM "}))
    assert db_conn.execute("SELECT email FROM clients").fetchone()["email"] == "mixed@example.com"


def test_contact_with_only_email_or_only_crm_id(client, db_conn) -> None:
    assert post(client, payload(contact={"external_id": None})).status_code == 201
    assert post(client, payload(event_id="e2", deal={"external_id": "d2"},
                                contact={"email": None, "external_id": "c-only", "company": None})).status_code == 201
    rows = db_conn.execute("SELECT email, crm_source, crm_external_id FROM clients ORDER BY email NULLS LAST").fetchall()
    assert [tuple(r.values()) for r in rows] == [("client@example.com", None, None), (None, "generic", "c-only")]


def test_optional_phone_and_company_may_be_null(client, db_conn) -> None:
    response = post(client, payload(contact={"phone": None, "company": None}))
    assert response.status_code == 201, response.text


def test_crm_id_and_email_matching_different_clients_is_409(client, db_conn) -> None:
    _insert_client(db_conn, name="A", email="a@example.com", crm_source="generic", crm_external_id="contact_456")
    _insert_client(db_conn, name="B", email="client@example.com")
    before = counts(db_conn)
    response = post(client, payload())
    assert response.status_code == 409
    assert response.json()["code"] == "identity_mismatch"
    after = counts(db_conn)
    # Only the rejected event is recorded; no invitation, token or activity.
    assert after == {**before, "webhook_events": 1}
    event = db_conn.execute("SELECT status, error_code FROM webhook_events").fetchone()
    assert (event["status"], event["error_code"]) == ("rejected", "identity_mismatch")


def test_conflicting_company_is_409_not_merged(client, db_conn) -> None:
    _insert_client(db_conn, name="Demo", email="client@example.com", company="Another Business Ltd")
    response = post(client, payload())
    assert (response.status_code, response.json()["code"]) == (409, "company_conflict")
    assert db_conn.execute("SELECT company, crm_source FROM clients").fetchone() == {
        "company": "Another Business Ltd", "crm_source": None}
    assert counts(db_conn)["onboarding_invitations"] == 0


def test_company_comparison_ignores_case_and_spacing(client, db_conn) -> None:
    _insert_client(db_conn, name="Demo", email="client@example.com", company="example   COMPANY")
    assert post(client, payload()).status_code == 201


def test_email_owned_by_other_contact_in_same_crm_is_409(client, db_conn) -> None:
    _insert_client(db_conn, name="Demo", email="client@example.com", crm_source="generic", crm_external_id="c-other")
    response = post(client, payload())
    assert (response.status_code, response.json()["code"]) == (409, "crm_identity_conflict")


def test_rejected_identity_conflict_is_reevaluated_on_redelivery(client, db_conn) -> None:
    cid = _insert_client(db_conn, name="Demo", email="client@example.com", company="Wrong Co")
    assert post(client, payload()).status_code == 409
    assert post(client, payload()).status_code == 409  # still conflicting
    db_conn.execute("UPDATE clients SET company = 'Example Company' WHERE id = %s", (cid,))
    response = post(client, payload())  # same event, same payload
    assert response.status_code == 201
    event = db_conn.execute("SELECT status, error_code, duplicate_count FROM webhook_events").fetchone()
    assert (event["status"], event["error_code"], event["duplicate_count"]) == ("processed", None, 2)
    assert post(client, payload()).json()["status"] == "duplicate"


def test_names_alone_never_match(client, db_conn) -> None:
    _insert_client(db_conn, name="Demo Client", email="someone@else.com")
    post(client, payload())
    assert counts(db_conn)["clients"] == 2


# ----------------------------------------------------------- transactions
def test_failure_during_intake_rolls_back_everything(client, db_conn, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash after invitation insert")

    monkeypatch.setattr(onboarding_store, "insert_access_token", boom)
    response = post(client, payload())
    assert response.status_code == 500
    assert "simulated" not in response.text
    assert counts(db_conn) == ZERO  # no event, client, invitation, token or activity

    monkeypatch.undo()
    retry = post(client, payload())  # the same event processes normally afterwards
    assert retry.status_code == 201
    assert counts(db_conn) == {**ZERO, "webhook_events": 1, "clients": 1, "onboarding_invitations": 1,
                               "client_access_tokens": 1, "activity_events": 3}


def test_failure_on_matched_client_does_not_partially_update_it(client, db_conn, monkeypatch) -> None:
    _insert_client(db_conn, name="Demo", email="client@example.com")
    monkeypatch.setattr(onboarding_store, "insert_access_token",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert post(client, payload()).status_code == 500
    row = db_conn.execute("SELECT crm_source, company, phone FROM clients").fetchone()
    assert row == {"crm_source": None, "company": None, "phone": None}
    assert counts(db_conn) == {**ZERO, "clients": 1}


# ------------------------------------------------------------ concurrency
def _concurrent(client, bodies: list[bytes], ts: int | None = None):
    headers = [signed_headers(b, timestamp=ts) for b in bodies]
    with ThreadPoolExecutor(max_workers=len(bodies)) as pool:
        futures = [pool.submit(client.post, URL, content=b, headers=h) for b, h in zip(bodies, headers)]
        return [f.result() for f in futures]


def test_twenty_concurrent_identical_requests_create_exactly_one_intake(client, db_conn) -> None:
    body = encode(payload())
    responses = _concurrent(client, [body] * 20, ts=int(time.time()))
    statuses = sorted((r.status_code, r.json()["status"]) for r in responses)
    assert statuses == [(200, "duplicate")] * 19 + [(201, "created")]
    assert counts(db_conn) == {**ZERO, "webhook_events": 1, "clients": 1, "onboarding_invitations": 1,
                               "client_access_tokens": 1, "activity_events": 3}
    assert db_conn.execute("SELECT duplicate_count FROM webhook_events").fetchone()["duplicate_count"] == 19


def test_concurrent_same_deal_with_different_event_ids_creates_one_invitation(client, db_conn) -> None:
    bodies = [encode(payload(event_id=f"evt_c{i}")) for i in range(10)]
    responses = _concurrent(client, bodies)
    statuses = sorted((r.status_code, r.json()["status"]) for r in responses)
    assert statuses == [(200, "duplicate_deal")] * 9 + [(201, "created")]
    assert counts(db_conn) == {**ZERO, "webhook_events": 10, "clients": 1, "onboarding_invitations": 1,
                               "client_access_tokens": 1, "activity_events": 3}


def test_concurrent_new_deals_for_one_new_contact_create_one_client(client, db_conn) -> None:
    bodies = [encode(payload(event_id=f"evt_n{i}", deal={"external_id": f"deal_n{i}"})) for i in range(10)]
    responses = _concurrent(client, bodies)
    assert sorted(r.status_code for r in responses) == [201] * 10, [r.text for r in responses if r.status_code != 201]
    assert len({r.json()["client_id"] for r in responses}) == 1
    c = counts(db_conn)
    assert (c["clients"], c["onboarding_invitations"], c["client_access_tokens"]) == (1, 10, 10)
    # Exactly one client.created; the other nine matched it.
    types = _activity_types(db_conn)
    assert types.count("client.created") == 1 and types.count("client.matched") == 9


def test_a_rolled_back_first_attempt_does_not_block_a_concurrent_retry(client, db_conn, monkeypatch) -> None:
    calls = {"n": 0}
    original = onboarding_store.insert_access_token

    def fail_first(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            time.sleep(0.3)  # hold the event row lock, then fail
            raise RuntimeError("first attempt fails")
        return original(*args, **kwargs)

    monkeypatch.setattr(onboarding_store, "insert_access_token", fail_first)
    body = encode(payload())
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.post, URL, content=body, headers=signed_headers(body))
        time.sleep(0.1)
        second = pool.submit(client.post, URL, content=body, headers=signed_headers(body))
        results = sorted([first.result().status_code, second.result().status_code])
    assert results == [201, 500]
    assert counts(db_conn)["onboarding_invitations"] == 1
