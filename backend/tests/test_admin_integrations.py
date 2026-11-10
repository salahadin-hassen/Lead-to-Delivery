"""Integration monitoring, attempt history, authenticated retry, webhook history."""

import pytest

from app.jobs import runner
from app.jobs.registry import Handler, HandlerRegistry
from app.jobs.result import IntegrationResult
from tests.admin_helpers import XHR, admin_app, logged_in, post
from tests.crm_helpers import encode, payload, signed_headers
from tests.portal_helpers import new_invitation, submit


@pytest.fixture
def client(db_conn, migrated_database, pool_cleanup):
    with admin_app(migrated_database) as c:
        yield logged_in(c)


def _project(client):
    inv = new_invitation(client)
    return inv, submit(client, inv["token"]).json()["project"]


def _run_slack(result: IntegrationResult):
    registry = HandlerRegistry()
    registry.register(Handler("slack.notify", execute=lambda ctx: result))
    return runner.run_one(registry, worker_id="test-worker", lease_seconds=120)


def test_status_reports_configuration_without_secrets(db_conn, migrated_database, pool_cleanup) -> None:
    secret = "pk_live_" + "s" * 30
    with admin_app(migrated_database, clickup_api_token=secret, clickup_list_id="123456",
                   slack_webhook_url="https://hooks.slack.com/services/T0/B0/XYZ") as c:
        logged_in(c)
        response = c.get("/api/admin/integrations/status")
    data = response.json()
    assert data["clickup"]["credentials_present"] is True and data["clickup"]["configuration_valid"] is True
    assert data["clickup"]["live_verification"] == "not_verified"  # env vars alone prove nothing
    # Updated in Task 7: Slack is implemented. The test URL is not a valid webhook shape.
    assert data["slack"]["implemented"] is True and data["slack"]["credentials_present"] is True
    assert data["slack"]["configuration_error"] == "SLACK_INVALID_WEBHOOK_URL"
    assert data["slack"]["live_verification"] == "not_verified"
    # Updated in Task 8: Sheets is implemented; nothing is configured here.
    assert data["google_sheets"]["implemented"] is True
    assert data["google_sheets"]["configuration_error"] == "SHEETS_NOT_CONFIGURED"
    assert data["google_sheets"]["live_verification"] == "not_verified"
    assert secret not in response.text and "hooks.slack.com" not in response.text


def test_status_flags_invalid_clickup_configuration(db_conn, migrated_database, pool_cleanup) -> None:
    with admin_app(migrated_database, clickup_api_token="pk_x" * 5, clickup_list_id="abc") as c:
        logged_in(c)
        data = c.get("/api/admin/integrations/status").json()["clickup"]
    assert (data["configuration_valid"], data["configuration_error"]) == (False, "CLICKUP_INVALID_LIST_ID")


def test_job_list_filters(client, db_conn) -> None:
    _, project = _project(client)
    inv2 = new_invitation(client, email="z@example.com", company="Z Co")
    submit(client, inv2["token"])
    jobs = client.get("/api/admin/integration-jobs", params={"limit": 100}).json()
    assert jobs["total"] == 24
    by_type = client.get("/api/admin/integration-jobs", params={"job_type": "slack.notify"}).json()
    assert by_type["total"] == 2 and {j["job_type"] for j in by_type["items"]} == {"slack.notify"}
    by_project = client.get("/api/admin/integration-jobs", params={"project_id": project["id"], "limit": 100}).json()
    assert by_project["total"] == 12 and {j["project_id"] for j in by_project["items"]} == {project["id"]}
    assert sum(1 for j in by_project["items"] if j["task_title"]) == 10
    today = db_conn.execute("SELECT current_date AS d").fetchone()["d"].isoformat()
    assert client.get("/api/admin/integration-jobs", params={"created_from": today}).json()["total"] == 24
    assert client.get("/api/admin/integration-jobs", params={"created_to": "2000-01-01"}).json()["total"] == 0
    assert client.get("/api/admin/integration-jobs", params={"status": "bogus"}).status_code == 422


def test_job_detail_has_sanitised_attempt_history(client, db_conn) -> None:
    _project(client)
    _run_slack(IntegrationResult.permanent(
        "SLACK_REJECTED", "POST https://hooks.slack.com/services/T0/B0/XYZ failed token=xoxb-123 for a@b.example",
        http_status=404))
    job_id = db_conn.execute("SELECT id FROM integration_jobs WHERE job_type = 'slack.notify'").fetchone()["id"]
    detail = client.get(f"/api/admin/integration-jobs/{job_id}")
    data = detail.json()
    assert data["status"] == "dead" and data["requires_reconciliation"] is False
    (attempt,) = data["attempt_history"]
    assert (attempt["outcome"], attempt["http_status"], attempt["error_code"]) == ("permanent", 404, "SLACK_REJECTED")
    for leaked in ("hooks.slack.com", "xoxb-123", "a@b.example"):
        assert leaked not in detail.text
    assert "payload" not in data  # only the reconciliation subset is exposed


def test_retry_dead_job_through_api(client, db_conn) -> None:
    _project(client)
    _run_slack(IntegrationResult.permanent("NOT_CONFIGURED"))
    job_id = db_conn.execute("SELECT id FROM integration_jobs WHERE job_type = 'slack.notify'").fetchone()["id"]
    response = post(client, f"/api/admin/integration-jobs/{job_id}/retry", {"additional_attempts": 2})
    assert response.status_code == 200
    assert (response.json()["status"], response.json()["max_attempts"]) == ("pending", 3)
    assert len(response.json()["attempt_history"]) == 1  # history preserved
    act = db_conn.execute("SELECT actor_type, data FROM activity_events WHERE event_type = 'integration.job_requeued'"
                          ).fetchone()
    assert act["actor_type"] == "admin" and act["data"]["requested_by"] == "admin"


def test_unknown_outcome_requires_explicit_confirmation(client, db_conn) -> None:
    _project(client)
    _run_slack(IntegrationResult.unknown("RESPONSE_LOST"))
    job_id = db_conn.execute("SELECT id FROM integration_jobs WHERE job_type = 'slack.notify'").fetchone()["id"]
    assert client.get(f"/api/admin/integration-jobs/{job_id}").json()["requires_reconciliation"] is True
    refused = post(client, f"/api/admin/integration-jobs/{job_id}/retry", {"additional_attempts": 1})
    assert (refused.status_code, refused.json()["code"]) == (409, "reconciliation_required")
    assert db_conn.execute("SELECT status FROM integration_jobs WHERE id = %s", (job_id,)).fetchone()["status"] == "dead"
    accepted = post(client, f"/api/admin/integration-jobs/{job_id}/retry",
                    {"additional_attempts": 1, "reconciliation_confirmed": True})
    assert accepted.status_code == 200
    assert accepted.json()["reconciliation"]["confirmed_by"] == "admin"


@pytest.mark.parametrize("status", ["pending", "succeeded", "running"])
def test_non_dead_jobs_cannot_be_retried(client, db_conn, status) -> None:
    _project(client)
    job_id = db_conn.execute("SELECT id FROM integration_jobs WHERE job_type = 'slack.notify'").fetchone()["id"]
    if status == "succeeded":
        _run_slack(IntegrationResult.success())
    elif status == "running":
        db_conn.execute("UPDATE integration_jobs SET status = 'running', locked_by = 'w', "
                        "locked_until = now() + interval '1 minute' WHERE id = %s", (job_id,))
    response = post(client, f"/api/admin/integration-jobs/{job_id}/retry", {"additional_attempts": 1})
    assert (response.status_code, response.json()["code"]) == (409, "job_not_retryable")


def test_retry_validation_and_auth(client, db_conn, migrated_database) -> None:
    _project(client)
    _run_slack(IntegrationResult.permanent("X"))
    job_id = db_conn.execute("SELECT id FROM integration_jobs WHERE job_type = 'slack.notify'").fetchone()["id"]
    assert post(client, f"/api/admin/integration-jobs/{job_id}/retry", {"additional_attempts": 0}).status_code == 422
    assert post(client, f"/api/admin/integration-jobs/{job_id}/retry", {"force": True}).status_code == 422
    assert client.post(f"/api/admin/integration-jobs/{job_id}/retry", json={}).status_code == 403  # no CSRF header
    client.post("/api/admin/logout", headers=XHR)
    assert post(client, f"/api/admin/integration-jobs/{job_id}/retry", {}).status_code == 401
    assert db_conn.execute("SELECT status FROM integration_jobs WHERE id = %s", (job_id,)).fetchone()["status"] == "dead"


def test_webhook_event_history_links_client_without_contact_data(client, db_conn) -> None:
    inv = new_invitation(client)
    dup = payload(event_id="evt_dup_deal", deal={"external_id": db_conn.execute(
        "SELECT crm_deal_id FROM onboarding_invitations").fetchone()["crm_deal_id"]},
        contact={"email": db_conn.execute("SELECT email FROM clients").fetchone()["email"],
                 "external_id": db_conn.execute("SELECT crm_external_id FROM clients").fetchone()["crm_external_id"]})
    body = encode(dup)
    assert client.post("/api/webhooks/crm/deal-won", content=body, headers=signed_headers(body)).json()["status"] == "duplicate_deal"
    response = client.get("/api/admin/webhook-events")
    events = response.json()["items"]
    assert len(events) == 2
    for e in events:
        assert e["client_id"] == inv["client_id"] and e["invitation_id"] == inv["invitation_id"]
    rejected = client.get("/api/admin/webhook-events", params={"status": "rejected"}).json()["items"]
    assert [(e["external_event_id"], e["error_code"]) for e in rejected] == [("evt_dup_deal", "duplicate_deal")]
    for private in ("@example.com", "+10000000000", "contact_"):
        assert private not in response.text
