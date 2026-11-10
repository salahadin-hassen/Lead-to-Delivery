"""Slack adapter: configuration, message content, delivery, classification,
unknown outcomes, manual retry/resolution, rate limits and secret protection
(fake Slack via httpx.MockTransport; PostgreSQL ltd_test)."""

import json
import logging
from datetime import date

import httpx
import pytest

from app import db
from app.errors import JobNotResolvableError, ReconciliationRequiredError
from app.integrations.slack import ProjectFacts, SlackAdapter, build_message, safe_text, webhook_url_problem
from app.jobs import runner
from app.jobs.handlers import build_registry
from app.jobs.registry import HandlerRegistry
from app.services.job_admin import resolve_slack_delivered, retry_dead_job
from tests.admin_helpers import XHR, admin_app, logged_in
from tests.conftest import make_settings
from tests.job_helpers import attempts, expire_lease, insert_job, job

# Synthetic secret: never a real webhook.
SECRET_PART = "SyntheticSecret0123456789abcdef"
WEBHOOK = f"https://hooks.slack.com/services/T0SYNTH01/B0SYNTH02/{SECRET_PART}"
BASE = "https://hub.example.test"


class FakeSlack:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.behavior = lambda req: httpx.Response(200, text="ok")
        self.checks: list[dict] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        stats = db._pool.get_stats() if db._pool is not None else {}
        self.checks.append({"held": db.connection_held(),
                            "in_use": stats.get("pool_size", 0) - stats.get("pool_available", 0)})
        return self.behavior(request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


@pytest.fixture
def pool(db_conn, migrated_database, pool_cleanup):
    db.configure_pool(migrated_database)
    yield migrated_database


@pytest.fixture
def fake() -> FakeSlack:
    return FakeSlack()


def adapter_for(url, fake, **overrides) -> SlackAdapter:
    values = {"slack_webhook_url": WEBHOOK, "app_base_url": BASE, "http_timeout_seconds": 5, **overrides}
    return SlackAdapter(make_settings(url, **values), transport=fake.transport())


def run(adapter, worker="w1"):
    registry = HandlerRegistry()
    registry.register(adapter.handler())
    return runner.run_one(registry, worker_id=worker, lease_seconds=120)


def make_project(conn, *, name="Website Development", company="Example Agency", tasks=3, with_job=True):
    client = conn.execute(
        "INSERT INTO clients (name, email, phone, company) VALUES ('Jane Client', "
        "'jane+' || gen_random_uuid() || '@client.example', '+15550100', %s) RETURNING id", (company,)).fetchone()["id"]
    template = conn.execute("SELECT id FROM project_templates WHERE key = 'website_build'").fetchone()["id"]
    project = conn.execute(
        "INSERT INTO projects (client_id, template_id, template_version, name, start_date, due_date) "
        "VALUES (%s, %s, 1, %s, '2026-11-08', '2026-12-20') RETURNING id", (client, template, name)).fetchone()["id"]
    for i in range(tasks):
        conn.execute("INSERT INTO tasks (project_id, title, position) VALUES (%s, %s, %s)", (project, f"Task {i}", i))
    job_id = None
    if with_job:
        job_id = insert_job(conn, job_type="slack.notify", key=f"slack.notify:project_created:{project}",
                            subject_type="project", subject_id=project, max_attempts=5,
                            payload={"event": "project_created"})
    return project, job_id


def last_attempt(conn, job_id):
    return attempts(conn, job_id)[-1]


# ------------------------------------------------------------ configuration
@pytest.mark.parametrize(("url", "code"), [
    (None, "SLACK_NOT_CONFIGURED"),
    ("", "SLACK_NOT_CONFIGURED"),
    ("http://hooks.slack.com/services/T0SYNTH01/B0SYNTH02/abcdefgh12", "SLACK_INVALID_WEBHOOK_URL"),
    ("https://hooks.evil.example/services/T0SYNTH01/B0SYNTH02/abcdefgh12", "SLACK_INVALID_WEBHOOK_URL"),
    ("https://hooks.slack.com.evil.example/services/T0SYNTH01/B0SYNTH02/abcdefgh12", "SLACK_INVALID_WEBHOOK_URL"),
    ("https://hooks.slack.com/api/T0SYNTH01/B0SYNTH02/abcdefgh12", "SLACK_INVALID_WEBHOOK_URL"),
    ("https://hooks.slack.com/services/T0SYNTH01/B0SYNTH02/abc/../x", "SLACK_INVALID_WEBHOOK_URL"),
    ("https://user:pw@hooks.slack.com/services/T0SYNTH01/B0SYNTH02/abcdefgh12", "SLACK_INVALID_WEBHOOK_URL"),
    ("https://hooks.slack.com:8443/services/T0SYNTH01/B0SYNTH02/abcdefgh12", "SLACK_INVALID_WEBHOOK_URL"),
    ("https://hooks.slack.com/services/T0SYNTH01/B0SYNTH02/abcdefgh12?channel=x", "SLACK_INVALID_WEBHOOK_URL"),
    ("https://hooks.slack.com/services/T0SYNTH01/B0SYNTH02/abcdefgh12#frag", "SLACK_INVALID_WEBHOOK_URL"),
    ("https://hooks.slack.com:notaport/services/T0SYNTH01/B0SYNTH02/abcdefgh12", "SLACK_INVALID_WEBHOOK_URL"),
])
def test_invalid_configuration_is_permanent_without_request(pool, db_conn, fake, url, code) -> None:
    assert webhook_url_problem(url) == code
    _, job_id = make_project(db_conn)
    assert run(adapter_for(pool, fake, slack_webhook_url=url)).final_state == "dead"
    assert job(db_conn, job_id)["last_error_code"] == code
    assert fake.requests == []


@pytest.mark.parametrize("url", [WEBHOOK, "https://hooks.slack.com:443/services/T0SYNTH01/B0SYNTH02/abcdefgh12",
                                 "https://HOOKS.slack.com/services/T0SYNTH01/B0SYNTH02/abcdefgh12"])
def test_valid_webhook_urls(url) -> None:
    assert webhook_url_problem(url) is None


def test_registry_has_slack(pool) -> None:
    # Updated in Task 8: Sheets is registered too.
    registry = build_registry(make_settings(pool))
    assert {"slack.notify", "sheets.upsert_project"} <= set(registry.job_types())
    registry.close()


def test_api_starts_without_slack_configuration(pool) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    with TestClient(create_app(make_settings(pool, slack_webhook_url="not a url"))) as c:
        assert c.get("/api/health").status_code == 200


# ------------------------------------------------------------ message content
def _facts(**kw) -> ProjectFacts:
    values = dict(id="11111111-1111-1111-1111-111111111111", name="Website Development", status="active",
                  company="Example Agency", service="Website Build", task_count=10,
                  start_date=date(2026, 11, 8), due_date=date(2026, 12, 20))
    values.update(kw)
    return ProjectFacts(**values)


def _all_text(message: dict) -> str:
    return json.dumps(message, ensure_ascii=False)


def test_message_fields_and_fallback() -> None:
    m = build_message(_facts(), BASE)
    assert m["text"] == "New project ready: Website Development (Example Agency)"
    assert m["unfurl_links"] is False and m["unfurl_media"] is False
    assert not {"channel", "username", "icon_url", "icon_emoji", "link_names"} & set(m)
    fields = [f["text"] for f in m["blocks"][1]["fields"]]
    assert fields == ["Project: Website Development", "Client: Example Agency", "Service: Website Build",
                      "Tasks created: 10", "Project status: Active", "Due: 20 Dec 2026"]
    assert all(f["type"] == "plain_text" for f in m["blocks"][1]["fields"])
    button = m["blocks"][3]["elements"][0]
    assert button["url"] == f"{BASE}/admin/projects/11111111-1111-1111-1111-111111111111"
    assert m["blocks"][0]["text"]["type"] == "plain_text"


def test_message_without_company_omits_client_line() -> None:
    m = build_message(_facts(company=None), BASE)
    assert m["text"] == "New project ready: Website Development"
    assert not any(f["text"].startswith("Client:") for f in m["blocks"][1]["fields"])


def test_untrusted_text_cannot_mention_link_or_format() -> None:
    m = build_message(_facts(name="Launch <!channel> @here <@U123|x> <https://evil.example|click> *bold*\nline2",
                             company="@everyone & Co"), BASE)
    text = _all_text(m)
    for forbidden in ("<!channel>", "<@U123", "<https://evil", "@here", "@everyone", "\nline2"):
        assert forbidden not in text
    assert "&lt;!channel&gt;" in text and "@​here" in text and "&amp; Co" in text


def test_long_values_are_truncated() -> None:
    m = build_message(_facts(name="x" * 500), BASE)
    assert len(m["blocks"][1]["fields"][0]["text"]) <= len("Project: ") + 150
    assert safe_text("y" * 400).endswith("…")


# --------------------------------------------------------------- delivery
def test_successful_delivery(pool, db_conn, fake) -> None:
    project, job_id = make_project(db_conn, tasks=4)
    report = run(adapter_for(pool, fake))
    assert report.final_state == "succeeded"
    (req,) = fake.requests
    assert req.method == "POST" and str(req.url) == WEBHOOK
    assert req.headers["content-type"] == "application/json"
    body = json.loads(req.content)
    assert body["text"] == "New project ready: Website Development (Example Agency)"
    assert "Tasks created: 4" in _all_text(body)
    sent = req.content.decode()
    for private in ("@client.example", "+15550100", "Jane Client"):
        assert private not in sent
    row = job(db_conn, job_id)
    assert (row["status"], row["last_error_code"]) == ("succeeded", None)
    a = last_attempt(db_conn, job_id)
    assert (a["outcome"], a["http_status"], a["external_id"]) == ("success", 200, None)  # no invented id
    act = db_conn.execute("SELECT summary, project_id FROM activity_events WHERE event_type = 'integration.slack_notified'"
                          ).fetchone()
    assert act["project_id"] == project


def test_onboarding_answers_are_never_sent(pool, db_conn, fake, migrated_database, pool_cleanup) -> None:
    from tests.crm_helpers import webhook_client
    from tests.portal_helpers import new_invitation, submit, valid_form

    with webhook_client(migrated_database) as client:
        inv = new_invitation(client)
        assert submit(client, inv["token"], valid_form(business_goals="SECRET-GOAL increase revenue a lot")).status_code == 201
    db.configure_pool(migrated_database)
    assert run(adapter_for(migrated_database, fake)).final_state == "succeeded"
    sent = fake.requests[0].content.decode()
    for private in ("SECRET-GOAL", "Demo Client", "@example.com", "+10000000000", inv["token"]):
        assert private not in sent
    assert "Tasks created: 10" in sent and "Website Build" in sent


def test_rerun_of_succeeded_job_sends_nothing(pool, db_conn, fake) -> None:
    make_project(db_conn)
    adapter = adapter_for(pool, fake)
    assert run(adapter).final_state == "succeeded"
    for _ in range(3):
        assert run(adapter) is None
    assert len(fake.requests) == 1


def test_no_connection_held_during_post(pool, db_conn, fake) -> None:
    make_project(db_conn)
    run(adapter_for(pool, fake))
    assert fake.checks == [{"held": False, "in_use": 0}]


def test_client_settings_tls_timeouts_no_redirects(pool, fake) -> None:
    adapter = SlackAdapter(make_settings(pool, slack_webhook_url=WEBHOOK, http_timeout_seconds=12))
    client = adapter._http()
    try:
        assert client.follow_redirects is False
        assert client.timeout.connect == 5.0 and client.timeout.read == 12
        assert client._transport._pool._ssl_context.verify_mode.name == "CERT_REQUIRED"
    finally:
        adapter.close()


def test_redirect_is_not_followed(pool, db_conn, fake) -> None:
    _, job_id = make_project(db_conn)
    fake.behavior = lambda req: httpx.Response(302, headers={"location": "https://evil.example/collect"})
    assert run(adapter_for(pool, fake)).final_state == "dead"
    assert len(fake.requests) == 1
    assert last_attempt(db_conn, job_id)["error_code"] == "SLACK_HTTP_302"


# --------------------------------------------------------- classification
@pytest.mark.parametrize(("status", "body", "code"), [
    (400, "invalid_payload", "SLACK_INVALID_PAYLOAD"),
    (400, "no_text", "SLACK_NO_TEXT"),
    (403, "action_prohibited", "SLACK_ACTION_PROHIBITED"),
    (403, "invalid_token", "SLACK_INVALID_TOKEN"),
    (404, "channel_not_found", "SLACK_CHANNEL_NOT_FOUND"),
    (404, "no_service", "SLACK_NO_SERVICE"),
    (410, "channel_is_archived", "SLACK_CHANNEL_IS_ARCHIVED"),
    (400, "<html>Something " + "x" * 300 + "</html>", "SLACK_HTTP_400"),
])
def test_permanent_errors(pool, db_conn, fake, status, body, code) -> None:
    _, job_id = make_project(db_conn)
    fake.behavior = lambda req: httpx.Response(status, text=body)
    assert run(adapter_for(pool, fake)).final_state == "dead"
    a = last_attempt(db_conn, job_id)
    assert (a["outcome"], a["error_code"], a["http_status"]) == ("permanent", code, status)
    assert "<html>" not in str(a)  # raw bodies are never stored


def test_429_with_retry_after_is_retryable_and_capped(pool, db_conn, fake) -> None:
    _, job_id = make_project(db_conn)
    fake.behavior = lambda req: httpx.Response(429, headers={"Retry-After": "37"}, text="rate_limited")
    assert run(adapter_for(pool, fake)).final_state == "retry_scheduled"
    wait = db_conn.execute("SELECT EXTRACT(EPOCH FROM next_run_at - now()) AS s FROM integration_jobs WHERE id = %s",
                           (job_id,)).fetchone()["s"]
    assert 33 <= wait <= 37
    a = last_attempt(db_conn, job_id)
    assert (a["outcome"], a["error_code"], a["http_status"]) == ("retryable", "SLACK_RATE_LIMITED", 429)

    _, job2 = make_project(db_conn)
    db_conn.execute("UPDATE integration_jobs SET next_run_at = now() + interval '1 hour' WHERE id = %s", (job_id,))
    fake.behavior = lambda req: httpx.Response(429, headers={"Retry-After": "86400"})
    run(adapter_for(pool, fake, worker_max_retry_delay_seconds=120))
    wait2 = db_conn.execute("SELECT EXTRACT(EPOCH FROM next_run_at - now()) AS s FROM integration_jobs WHERE id = %s",
                            (job2,)).fetchone()["s"]
    assert 115 <= wait2 <= 120


def test_429_without_retry_after_uses_backoff(pool, db_conn, fake) -> None:
    _, job_id = make_project(db_conn)
    fake.behavior = lambda req: httpx.Response(429)
    run(adapter_for(pool, fake))
    wait = db_conn.execute("SELECT EXTRACT(EPOCH FROM next_run_at - now()) AS s FROM integration_jobs WHERE id = %s",
                           (job_id,)).fetchone()["s"]
    assert 27 <= wait <= 30


def _raise(exc):
    def behaviour(req):
        raise exc("boom", request=req)
    return behaviour


@pytest.mark.parametrize("exc", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
def test_failures_before_dispatch_are_retryable(pool, db_conn, fake, exc) -> None:
    _, job_id = make_project(db_conn)
    fake.behavior = _raise(exc)  # ConnectError also covers DNS resolution failures
    assert run(adapter_for(pool, fake)).final_state == "retry_scheduled"
    assert last_attempt(db_conn, job_id)["error_code"] == "SLACK_CONNECT_FAILED"


@pytest.mark.parametrize("exc", [httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError, httpx.ReadError])
def test_failures_after_dispatch_are_unknown(pool, db_conn, fake, exc) -> None:
    _, job_id = make_project(db_conn)
    fake.behavior = _raise(exc)
    report = run(adapter_for(pool, fake))
    assert (report.outcome, report.final_state) == ("unknown", "dead")
    assert job(db_conn, job_id)["last_error_code"] == "UNKNOWN_OUTCOME_REQUIRES_RECONCILIATION"
    assert last_attempt(db_conn, job_id)["error_code"] == "SLACK_DELIVERY_UNKNOWN"


@pytest.mark.parametrize(("status", "body", "code"), [
    (408, "", "SLACK_DELIVERY_UNKNOWN"), (500, "", "SLACK_DELIVERY_UNKNOWN"), (502, "", "SLACK_DELIVERY_UNKNOWN"),
    (503, "", "SLACK_DELIVERY_UNKNOWN"), (200, "", "SLACK_UNEXPECTED_RESPONSE"),
    (200, "OK", "SLACK_UNEXPECTED_RESPONSE"), (200, '{"ok": true}', "SLACK_UNEXPECTED_RESPONSE"),
    (200, "ok but more", "SLACK_UNEXPECTED_RESPONSE"),
])
def test_ambiguous_responses_are_unknown(pool, db_conn, fake, status, body, code) -> None:
    _, job_id = make_project(db_conn)
    fake.behavior = lambda req: httpx.Response(status, text=body)
    report = run(adapter_for(pool, fake))
    assert (report.outcome, report.final_state) == ("unknown", "dead")
    assert last_attempt(db_conn, job_id)["error_code"] == code


# ------------------------------------------------------- unknown outcomes
def _unknown_dead(pool, db_conn, fake):
    project, job_id = make_project(db_conn)
    fake.behavior = _raise(httpx.ReadTimeout)
    adapter = adapter_for(pool, fake)
    assert run(adapter).final_state == "dead"
    fake.behavior = lambda req: httpx.Response(200, text="ok")
    return project, job_id, adapter


def test_unknown_is_never_retried_automatically(pool, db_conn, fake) -> None:
    _, job_id, adapter = _unknown_dead(pool, db_conn, fake)
    for _ in range(3):
        assert run(adapter) is None
    assert len(fake.requests) == 1


def test_lease_expiry_does_not_resend_without_confirmation(pool, db_conn, fake) -> None:
    _, job_id = make_project(db_conn)
    adapter = adapter_for(pool, fake)
    registry = HandlerRegistry()
    registry.register(adapter.handler())
    runner.claim_next(registry, worker_id="crashed", lease_seconds=120)  # may have posted, then died
    expire_lease(db_conn, job_id)
    report = run(adapter, "w2")
    assert (report.outcome, report.final_state) == ("unknown", "dead")
    assert last_attempt(db_conn, job_id)["error_code"] == "SLACK_DELIVERY_UNCONFIRMED"
    assert fake.requests == []


def test_retry_without_confirmation_refused_then_confirmed_absent_resends_once(pool, db_conn, fake) -> None:
    _, job_id, adapter = _unknown_dead(pool, db_conn, fake)
    with pytest.raises(ReconciliationRequiredError):
        retry_dead_job(job_id, additional_attempts=2)
    retry_dead_job(job_id, additional_attempts=2, reconciliation_confirmed=True, actor="ops")
    assert run(adapter).final_state == "succeeded"
    assert len(fake.requests) == 2
    assert [a["outcome"] for a in attempts(db_conn, job_id)] == ["unknown", "success"]


def test_resolve_delivered_marks_success_without_post(pool, db_conn, fake) -> None:
    project, job_id, adapter = _unknown_dead(pool, db_conn, fake)
    history = attempts(db_conn, job_id)
    resolve_slack_delivered(job_id, message_found_in_channel=True, actor="ops")
    row = job(db_conn, job_id)
    assert (row["status"], row["last_error_code"], row["locked_by"]) == ("succeeded", None, None)
    assert row["completed_at"] is not None
    assert row["payload"]["resolution"]["resolved_as"] == "delivered_confirmed_by_admin"
    assert row["payload"]["resolution"]["after_attempt"] == 1
    assert attempts(db_conn, job_id) == history  # the unknown attempt is preserved
    assert run(adapter) is None and len(fake.requests) == 1
    act = db_conn.execute("SELECT data, project_id FROM activity_events WHERE event_type = 'integration.job_resolved_delivered'"
                          ).fetchone()
    assert act["project_id"] == project and act["data"]["resolved_by"] == "ops"


@pytest.mark.parametrize("case", ["clickup", "not_dead", "not_unknown", "no_confirmation"])
def test_resolve_delivered_is_refused_outside_its_case(pool, db_conn, fake, case) -> None:
    if case == "clickup":
        job_id = insert_job(db_conn, job_type="clickup.create_task", status="dead")
        db_conn.execute("INSERT INTO integration_attempts (job_id, attempt_number, outcome, finished_at) "
                        "VALUES (%s, 1, 'unknown', now())", (job_id,))
        expected = "wrong_job_type"
    elif case == "not_dead":
        _, job_id = make_project(db_conn)
        expected = "not_dead"
    elif case == "not_unknown":
        _, job_id = make_project(db_conn)
        fake.behavior = lambda req: httpx.Response(404, text="no_service")
        run(adapter_for(pool, fake))
        expected = "outcome_not_unknown"
    else:
        _, job_id, _ = _unknown_dead(pool, db_conn, fake)
        expected = "confirmation_required"
    before = job(db_conn, job_id)
    with pytest.raises(JobNotResolvableError) as excinfo:
        resolve_slack_delivered(job_id, message_found_in_channel=(case != "no_confirmation"))
    assert excinfo.value.reason == expected
    assert job(db_conn, job_id) == before


# ------------------------------------------------------- admin endpoints
def test_admin_resolve_endpoint(db_conn, migrated_database, pool_cleanup, fake) -> None:
    with admin_app(migrated_database) as client:
        _, job_id, _ = _unknown_dead(migrated_database, db_conn, fake)
        assert client.post(f"/api/admin/integration-jobs/{job_id}/resolve-delivered",
                           json={"message_found_in_channel": True}, headers=XHR).status_code == 401
        logged_in(client)
        assert client.post(f"/api/admin/integration-jobs/{job_id}/resolve-delivered",
                           json={"message_found_in_channel": True}).status_code == 403  # CSRF header missing
        assert client.post(f"/api/admin/integration-jobs/{job_id}/resolve-delivered",
                           json={"message_found_in_channel": False}, headers=XHR).status_code == 422
        ok = client.post(f"/api/admin/integration-jobs/{job_id}/resolve-delivered",
                         json={"message_found_in_channel": True}, headers=XHR)
        assert ok.status_code == 200 and ok.json()["status"] == "succeeded"
        assert ok.json()["resolution"]["resolved_by"] == "admin@agency.example"
        again = client.post(f"/api/admin/integration-jobs/{job_id}/resolve-delivered",
                            json={"message_found_in_channel": True}, headers=XHR)
        assert (again.status_code, again.json()["code"]) == (409, "job_not_resolvable_not_dead")
        clickup = insert_job(db_conn, job_type="clickup.create_task", status="dead")
        wrong = client.post(f"/api/admin/integration-jobs/{clickup}/resolve-delivered",
                            json={"message_found_in_channel": True}, headers=XHR)
        assert (wrong.status_code, wrong.json()["code"]) == (409, "job_not_resolvable_wrong_job_type")


def test_status_endpoint_never_returns_the_webhook(db_conn, migrated_database, pool_cleanup) -> None:
    with admin_app(migrated_database, slack_webhook_url=WEBHOOK) as client:
        logged_in(client)
        response = client.get("/api/admin/integrations/status")
    slack = response.json()["slack"]
    assert (slack["implemented"], slack["credentials_present"], slack["configuration_valid"]) == (True, True, True)
    assert slack["live_verification"] == "not_verified"
    assert SECRET_PART not in response.text and "hooks.slack.com" not in response.text


# ----------------------------------------------------------- secret leakage
def test_webhook_url_never_leaks(pool, db_conn, fake, caplog) -> None:
    caplog.set_level(logging.DEBUG)  # every logger, including httpx and httpcore, at DEBUG
    for behavior in (lambda r: httpx.Response(200, text="ok"), lambda r: httpx.Response(404, text="no_service"),
                     lambda r: httpx.Response(429), _raise(httpx.ReadTimeout), _raise(httpx.ConnectError),
                     lambda r: httpx.Response(500, text=f"error at {WEBHOOK}")):
        make_project(db_conn)
        fake.behavior = behavior
        run(adapter_for(pool, fake))
    assert any(r.name == "httpx" for r in caplog.records)  # httpx did log requests…
    assert "<redacted-slack-webhook-url>" in caplog.text   # …with the URL redacted
    dump = " ".join(str(r) for t in ("integration_jobs", "integration_attempts", "activity_events")
                    for r in db_conn.execute(f"SELECT t::text FROM {t} t").fetchall())
    for leak in (SECRET_PART, "hooks.slack.com/services", "B0SYNTH02"):
        assert leak not in caplog.text, leak
        assert leak not in dump, leak


def test_worker_closes_http_clients_on_shutdown(pool, fake) -> None:
    adapter = adapter_for(pool, fake)
    adapter._http()
    registry = HandlerRegistry()
    registry.register(adapter.handler())
    registry.close()
    assert adapter._client is None


def test_end_to_end_onboarding_notifies_once(db_conn, migrated_database, pool_cleanup, fake) -> None:
    from tests.crm_helpers import webhook_client
    from tests.portal_helpers import new_invitation, submit

    with webhook_client(migrated_database) as client:
        submit(client, new_invitation(client)["token"])
    db.configure_pool(migrated_database)
    adapter = adapter_for(migrated_database, fake)
    while run(adapter) is not None:
        pass
    assert len(fake.requests) == 1
    rows = {(r["job_type"], r["status"]) for r in db_conn.execute(
        "SELECT DISTINCT job_type, status FROM integration_jobs").fetchall()}
    assert rows == {("slack.notify", "succeeded"), ("clickup.create_task", "pending"),
                    ("sheets.upsert_project", "pending")}


# ---------------------------------------------------------- live-check script
def test_live_check_logic_against_fake(db_conn, migrated_database, pool_cleanup, fake, capsys) -> None:
    """The live script's steps against the FAKE endpoint only (not a live test)."""
    from app.integrations.slack_live_check import live_check

    lines: list[str] = []
    live_check(make_settings(migrated_database, slack_webhook_url=WEBHOOK), transport=fake.transport(), out=lines.append)
    assert len(fake.requests) == 1
    assert any(line.startswith("1. Slack answered HTTP 200") for line in lines)
    assert any(line.startswith("3. re-running the worker sent nothing") for line in lines)
    assert all(SECRET_PART not in line and "hooks.slack.com" not in line for line in lines)


def test_live_check_refuses_other_pending_jobs(db_conn, migrated_database, pool_cleanup, fake) -> None:
    from app.integrations.slack_live_check import LiveCheckError, live_check

    make_project(db_conn)
    with pytest.raises(LiveCheckError, match="other Slack job"):
        live_check(make_settings(migrated_database, slack_webhook_url=WEBHOOK), transport=fake.transport(),
                   out=lambda _: None)
    assert fake.requests == []


def test_live_check_requires_explicit_switch(monkeypatch) -> None:
    from app.integrations import slack_live_check

    monkeypatch.delenv("SLACK_LIVE_TEST", raising=False)
    assert slack_live_check.main() == 2
