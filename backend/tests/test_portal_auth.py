"""Portal authentication and invitation-scoped authorization."""

import logging

import pytest

from app.security import generate_token, hash_token
from tests.crm_helpers import webhook_client
from tests.portal_helpers import (
    FORBIDDEN_KEYS,
    auth,
    new_invitation,
    submit,
    walk_keys,
)


@pytest.fixture
def client(db_conn, migrated_database, pool_cleanup):
    with webhook_client(migrated_database) as c:
        yield c


ENDPOINTS = ["/api/portal/me", "/api/portal/onboarding", "/api/portal/project"]


# ----------------------------------------------------------- authentication
def test_valid_token_returns_invitation_context(client, db_conn) -> None:
    inv = new_invitation(client)
    response = client.get("/api/portal/me", headers=auth(inv["token"]))
    assert response.status_code == 200
    body = response.json()
    assert body["client"] == {"name": "Demo Client", "company": "Example Company"}
    assert body["invitation"]["status"] == "pending"
    assert body["invitation"]["service_type"] == "website_build"
    assert body["invitation"]["template_name"] == "Website Build"
    assert body["next_step"] == "onboarding"
    assert response.headers["cache-control"] == "no-store"
    assert not walk_keys(body) & FORBIDDEN_KEYS


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize(
    ("header", "code"),
    [
        (None, "token_missing"),
        ("Bearer", "token_missing"),
        ("Basic dXNlcjpwYXNz", "token_missing"),
        ("Bearer short", "token_invalid"),
        ("Bearer " + "A" * 43, "token_invalid"),          # well-formed but unknown
        ("Bearer " + "a" * 40 + "!*", "token_invalid"),   # characters outside the token alphabet
    ],
)
def test_missing_or_invalid_tokens_are_401(client, db_conn, endpoint, header, code) -> None:
    headers = {"Authorization": header} if header else {}
    response = client.get(endpoint, headers=headers)
    assert response.status_code == 401
    assert response.json()["code"] == code
    assert response.headers["www-authenticate"] == "Bearer"


def test_post_requires_authentication_too(client, db_conn) -> None:
    response = client.post("/api/portal/onboarding", json={})
    assert (response.status_code, response.json()["code"]) == (401, "token_missing")


def test_expired_token_is_rejected(client, db_conn) -> None:
    inv = new_invitation(client)
    db_conn.execute("UPDATE client_access_tokens SET expires_at = now() - interval '1 second'")
    for endpoint in ENDPOINTS:
        response = client.get(endpoint, headers=auth(inv["token"]))
        assert (response.status_code, response.json()["code"]) == (401, "token_expired")


def test_revoked_token_is_rejected(client, db_conn) -> None:
    inv = new_invitation(client)
    db_conn.execute("UPDATE client_access_tokens SET revoked_at = now()")
    response = client.get("/api/portal/me", headers=auth(inv["token"]))
    assert (response.status_code, response.json()["code"]) == (401, "token_revoked")


def test_revoked_invitation_disables_its_tokens(client, db_conn) -> None:
    inv = new_invitation(client)
    db_conn.execute("UPDATE onboarding_invitations SET status = 'revoked'")
    response = client.get("/api/portal/me", headers=auth(inv["token"]))
    assert (response.status_code, response.json()["code"]) == (401, "token_revoked")


def test_lookup_uses_only_the_hash(client, db_conn) -> None:
    inv = new_invitation(client)
    # Presenting the stored hash itself as a token must not authenticate.
    stored = db_conn.execute("SELECT token_hash FROM client_access_tokens").fetchone()["token_hash"]
    assert client.get("/api/portal/me", headers=auth(stored)).status_code == 401
    assert client.get("/api/portal/me", headers=auth(inv["token"])).status_code == 200


def test_expired_invitation_reports_expired_status(client, db_conn) -> None:
    inv = new_invitation(client)
    db_conn.execute("UPDATE onboarding_invitations SET expires_at = now() - interval '1 minute'")
    body = client.get("/api/portal/me", headers=auth(inv["token"])).json()
    assert body["invitation"]["status"] == "expired"
    assert body["next_step"] == "contact_agency"


# ---------------------------------------------------------- authorization
def test_client_a_cannot_reach_client_b_project(client, db_conn) -> None:
    a = new_invitation(client, email="a@example.com", company="A Co")
    b = new_invitation(client, email="b@example.com", company="B Co")
    a_project = submit(client, a["token"]).json()["project"]["id"]
    b_project = submit(client, b["token"]).json()["project"]["id"]

    assert client.get("/api/portal/project", headers=auth(b["token"])).json()["id"] == b_project
    response = client.get(f"/api/portal/projects/{a_project}", headers=auth(b["token"]))
    assert (response.status_code, response.json()["code"]) == (404, "project_not_found")
    assert client.get(f"/api/portal/projects/{a_project}", headers=auth(a["token"])).status_code == 200


def test_second_invitation_token_cannot_see_first_invitations_project(client, db_conn) -> None:
    first = new_invitation(client, email="same@example.com", contact_id="same-contact")
    first_project = submit(client, first["token"]).json()["project"]["id"]
    second = new_invitation(client, email="same@example.com", contact_id="same-contact",
                            service_type="seo_retainer")
    assert second["client_id"] == first["client_id"]  # same client, different invitation

    # The new invitation has no project yet; it must not fall back to the old one.
    response = client.get("/api/portal/project", headers=auth(second["token"]))
    assert (response.status_code, response.json()["code"]) == (404, "project_not_found")
    assert client.get(f"/api/portal/projects/{first_project}", headers=auth(second["token"])).status_code == 404
    me = client.get("/api/portal/me", headers=auth(second["token"])).json()
    assert me["invitation"]["status"] == "pending" and me["invitation"]["service_type"] == "seo_retainer"
    # And the old token still sees only its own project.
    assert client.get("/api/portal/project", headers=auth(first["token"])).json()["id"] == first_project


def test_extra_token_for_another_invitation_is_scoped_to_that_invitation(client, db_conn) -> None:
    """A token row pointing at invitation X never yields invitation Y's data."""
    a = new_invitation(client, email="a@example.com", company="A Co")
    a_project = submit(client, a["token"]).json()["project"]["id"]
    b = new_invitation(client, email="b@example.com", company="B Co")
    raw = generate_token()
    db_conn.execute(
        "INSERT INTO client_access_tokens (client_id, invitation_id, token_hash, expires_at) "
        "VALUES (%s, %s, %s, now() + interval '1 day')",
        (b["client_id"], b["invitation_id"], hash_token(raw)),
    )
    assert client.get(f"/api/portal/projects/{a_project}", headers=auth(raw)).status_code == 404
    assert client.get("/api/portal/me", headers=auth(raw)).json()["client"]["company"] == "B Co"


@pytest.mark.parametrize("bad_id", ["not-a-uuid", "00000000-0000-0000-0000-000000000000"])
def test_unknown_project_ids_are_404(client, db_conn, bad_id) -> None:
    inv = new_invitation(client)
    submit(client, inv["token"])
    assert client.get(f"/api/portal/projects/{bad_id}", headers=auth(inv["token"])).status_code == 404


def test_project_before_submission_is_404(client, db_conn) -> None:
    inv = new_invitation(client)
    response = client.get("/api/portal/project", headers=auth(inv["token"]))
    assert (response.status_code, response.json()["code"]) == (404, "project_not_found")


def test_get_requests_do_not_modify_the_database(client, db_conn) -> None:
    inv = new_invitation(client)
    submit(client, inv["token"])

    def snapshot():
        return db_conn.execute(
            """
            SELECT (SELECT count(*) FROM activity_events) AS activity,
                   (SELECT count(*) FROM integration_jobs) AS jobs,
                   (SELECT max(updated_at) FROM client_access_tokens) AS tokens_updated,
                   (SELECT max(last_used_at) FROM client_access_tokens) AS last_used,
                   (SELECT max(updated_at) FROM projects) AS projects_updated,
                   (SELECT max(updated_at) FROM tasks) AS tasks_updated,
                   (SELECT max(updated_at) FROM onboarding_invitations) AS invitations_updated,
                   (SELECT max(updated_at) FROM milestones) AS milestones_updated,
                   (SELECT count(*) FROM onboarding_submissions) AS submissions
            """
        ).fetchone()

    before = snapshot()
    for _ in range(3):
        for endpoint in ENDPOINTS:
            assert client.get(endpoint, headers=auth(inv["token"])).status_code == 200
    assert snapshot() == before


def test_raw_token_and_header_never_logged(client, db_conn, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    inv = new_invitation(client)
    client.get("/api/portal/me", headers=auth(inv["token"]))
    submit(client, inv["token"])
    client.get("/api/portal/project", headers=auth(inv["token"]))
    client.get("/api/portal/me", headers=auth(inv["token"][:-2] + "zz"))  # failed auth is logged by code only
    assert inv["token"] not in caplog.text
    assert inv["token"][:-2] not in caplog.text
    assert "Bearer" not in caplog.text
