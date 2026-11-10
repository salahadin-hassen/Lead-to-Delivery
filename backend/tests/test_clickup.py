"""ClickUp adapter: configuration, payload, due dates, persistence, fencing,
idempotency/reconciliation and error classification (fake ClickUp via
httpx.MockTransport; PostgreSQL ltd_test)."""

import json
import logging
import time
from datetime import date, datetime, timezone

import httpx
import pytest

from app import db
from app.errors import ReconciliationRequiredError
from app.integrations import clickup
from app.integrations.clickup import ClickUpAdapter, due_date_ms, marker, retry_after_from_headers, task_name
from app.jobs import runner
from app.jobs.handlers import build_registry
from app.jobs.registry import HandlerRegistry
from app.services.job_admin import retry_dead_job
from tests.conftest import make_settings
from tests.fake_clickup import LIST_ID, TOKEN, FakeClickUp
from tests.job_helpers import attempts, expire_lease, insert_job, job

LEASE = 120


# ------------------------------------------------------------------ setup
@pytest.fixture
def pool(db_conn, migrated_database, pool_cleanup):
    db.configure_pool(migrated_database)
    yield migrated_database


@pytest.fixture
def fake() -> FakeClickUp:
    return FakeClickUp()


def settings_for(url: str, **overrides):
    values = {"clickup_api_token": TOKEN, "clickup_list_id": LIST_ID, "clickup_timezone": "UTC",
              "http_timeout_seconds": 5, **overrides}
    return make_settings(url, **values)


def adapter_for(pool_url, fake, **overrides) -> ClickUpAdapter:
    max_pages = overrides.pop("max_scan_pages", clickup.DEFAULT_MAX_SCAN_PAGES)
    return ClickUpAdapter(settings_for(pool_url, **overrides), transport=fake.transport(), max_scan_pages=max_pages)


def registry_for(adapter: ClickUpAdapter) -> HandlerRegistry:
    registry = HandlerRegistry()
    registry.register(adapter.handler())
    return registry


def run(adapter, worker="w1"):
    return runner.run_one(registry_for(adapter), worker_id=worker, lease_seconds=LEASE)


def make_task(conn, *, title="Kickoff call", description="Confirm goals and scope.", due=date(2026, 11, 20),
              with_job=True, clickup_task_id=None):
    # Customer contact data that must never reach ClickUp (unique email per call).
    client = conn.execute("INSERT INTO clients (name, email, phone) VALUES ('Jane Client', "
                          "'jane+' || gen_random_uuid() || '@client.example', '+15550000') RETURNING id"
                          ).fetchone()["id"]
    project = conn.execute("INSERT INTO projects (client_id, name) VALUES (%s, 'Acme Website') RETURNING id",
                           (client,)).fetchone()["id"]
    milestone = conn.execute("INSERT INTO milestones (project_id, name, position) VALUES (%s, 'Discovery', 0) "
                             "RETURNING id", (project,)).fetchone()["id"]
    task = conn.execute(
        "INSERT INTO tasks (project_id, milestone_id, template_task_key, title, description, due_date, clickup_task_id) "
        "VALUES (%s, %s, 'kickoff_call', %s, %s, %s, %s) RETURNING id",
        (project, milestone, title, description, due, clickup_task_id)).fetchone()["id"]
    job_id = None
    if with_job:
        job_id = insert_job(conn, job_type="clickup.create_task", key=f"clickup.create_task:{task}",
                            subject_type="task", subject_id=task, max_attempts=5,
                            payload={"project_id": str(project), "assignee_role": "account_manager"})
    return task, job_id


def task_row(conn, task_id):
    return conn.execute("SELECT clickup_task_id, clickup_url FROM tasks WHERE id = %s", (task_id,)).fetchone()


def last_attempt(conn, job_id):
    return attempts(conn, job_id)[-1]


# --------------------------------------------------------------- helpers
@pytest.mark.parametrize(("day", "tz", "expected_utc"), [
    (date(2026, 11, 20), "UTC", "2026-11-20T12:00:00+00:00"),
    (date(2026, 11, 20), "Pacific/Auckland", "2026-11-19T23:00:00+00:00"),       # NZDT +13
    (date(2026, 11, 20), "America/Los_Angeles", "2026-11-20T20:00:00+00:00"),    # PST -8
    (date(2026, 3, 8), "America/Los_Angeles", "2026-03-08T19:00:00+00:00"),      # US DST starts that night
    (date(2026, 11, 1), "America/Los_Angeles", "2026-11-01T20:00:00+00:00"),     # US DST ends that night
    (date(2026, 4, 5), "Pacific/Auckland", "2026-04-05T00:00:00+00:00"),         # NZ DST ends that night (+12)
    (date(2026, 12, 31), "Asia/Kolkata", "2026-12-31T06:30:00+00:00"),
])
def test_due_date_is_noon_in_agency_zone(day, tz, expected_utc) -> None:
    ms = due_date_ms(day, tz)
    assert ms % 1000 == 0
    assert datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat() == expected_utc
    from zoneinfo import ZoneInfo
    assert datetime.fromtimestamp(ms / 1000, tz=ZoneInfo(tz)).date() == day  # same date in the agency zone
    if tz == "UTC":  # and for viewers up to 11 hours either side of it
        for viewer in ("Etc/GMT+11", "Etc/GMT-11"):
            assert datetime.fromtimestamp(ms / 1000, tz=ZoneInfo(viewer)).date() == day


def test_due_date_none_and_deterministic() -> None:
    assert due_date_ms(None, "UTC") is None
    assert due_date_ms(date(2026, 1, 1), "UTC") == due_date_ms(date(2026, 1, 1), "UTC") == 1767268800000


def test_task_name_keeps_marker_and_truncates_title() -> None:
    tid = "550e8400-e29b-41d4-a716-446655440000"
    assert task_name("Kickoff  call", tid) == f"Kickoff call [LTD-TASK:{tid}]"
    long_name = task_name("x" * 400, tid)
    assert long_name.endswith(marker(tid)) and len(long_name) <= clickup.MAX_TITLE_CHARS + 1 + len(marker(tid))
    assert "…" in long_name


def test_rate_limit_header_parsing() -> None:
    now = 1_800_000_000.0
    assert retry_after_from_headers(httpx.Headers({"x-ratelimit-reset": str(int(now) + 42)}), now, 900) == 42
    assert retry_after_from_headers(httpx.Headers({"retry-after": "17"}), now, 900) == 17
    assert retry_after_from_headers(httpx.Headers({"x-ratelimit-reset": str(int(now) + 99999)}), now, 600) == 600
    assert retry_after_from_headers(httpx.Headers({"x-ratelimit-reset": str(int(now) - 30)}), now, 900) == 1
    assert retry_after_from_headers(httpx.Headers({"retry-after": "garbage"}), now, 900) is None
    assert retry_after_from_headers(httpx.Headers({}), now, 900) is None


# ---------------------------------------------------------- configuration
@pytest.mark.parametrize(("overrides", "code"), [
    ({"clickup_api_token": None}, "CLICKUP_NOT_CONFIGURED"),
    ({"clickup_list_id": None}, "CLICKUP_NOT_CONFIGURED"),
    ({"clickup_list_id": "list-abc"}, "CLICKUP_INVALID_LIST_ID"),
    ({"clickup_api_token": "pk_has spaces in it"}, "CLICKUP_INVALID_TOKEN_FORMAT"),
    ({"clickup_timezone": "Mars/Olympus"}, "CLICKUP_INVALID_TIMEZONE"),
])
def test_invalid_configuration_is_permanent_and_makes_no_request(pool, db_conn, fake, overrides, code) -> None:
    task, job_id = make_task(db_conn)
    report = run(adapter_for(pool, fake, **overrides))
    assert report.final_state == "dead"
    assert job(db_conn, job_id)["last_error_code"] == code
    assert fake.requests == [] and task_row(db_conn, task)["clickup_task_id"] is None


def test_app_still_starts_without_clickup_configuration(pool) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    with TestClient(create_app(make_settings(pool, clickup_list_id="not-numeric"))) as c:
        assert c.get("/api/health").status_code == 200


def test_production_registry_registers_all_adapters(pool) -> None:
    # Updated in Task 7 (Slack) and Task 8 (Sheets).
    registry = build_registry(settings_for(pool))
    assert registry.job_types() == ["clickup.create_task", "sheets.upsert_project", "slack.notify"]
    registry.close()


# ----------------------------------------------------------- task creation
def test_creates_task_with_correct_request_and_stores_mapping(pool, db_conn, fake) -> None:
    task, job_id = make_task(db_conn)
    report = run(adapter_for(pool, fake))
    assert report.final_state == "succeeded"
    (post,) = fake.posts
    assert post.url == f"https://api.clickup.com/api/v2/list/{LIST_ID}/task"
    assert post.headers["authorization"] == TOKEN  # no "Bearer" prefix
    assert "token" not in str(post.url).lower() and TOKEN not in str(post.url)
    body = json.loads(post.content)
    assert set(body) == {"name", "description", "due_date", "due_date_time"}  # no status, no assignees
    assert body["name"] == f"Kickoff call [LTD-TASK:{task}]"
    assert body["due_date"] == due_date_ms(date(2026, 11, 20), "UTC") and body["due_date_time"] is False
    desc = body["description"]
    for expected in ("Project: Acme Website", "Milestone: Discovery", "Due: 2026-11-20",
                     "Suggested role: account manager (unassigned)", "Confirm goals and scope.", marker(task)):
        assert expected in desc
    for private in ("@client.example", "+15550000", "Jane Client"):
        assert private not in desc and private not in body["name"]

    created = fake.tasks[0]
    assert task_row(db_conn, task) == {"clickup_task_id": created["id"], "clickup_url": created["url"]}
    a = last_attempt(db_conn, job_id)
    assert (a["outcome"], a["external_id"], a["http_status"]) == ("success", created["id"], 200)


def test_missing_due_date_sends_no_due_fields(pool, db_conn, fake) -> None:
    make_task(db_conn, due=None, description=None)
    run(adapter_for(pool, fake))
    body = json.loads(fake.posts[0].content)
    assert "due_date" not in body and "due_date_time" not in body


def test_no_database_connection_held_during_http(pool, db_conn, fake) -> None:
    _, job_id = make_task(db_conn)
    adapter = adapter_for(pool, fake)
    run(adapter)
    # Also exercise the reconciliation GETs.
    _, job2 = make_task(db_conn)
    runner.claim_next(registry_for(adapter), worker_id="crashed", lease_seconds=LEASE)
    expire_lease(db_conn, job2)
    run(adapter)
    assert fake.gets and fake.posts
    assert all(c == {"held": False, "in_use": 0} for c in fake.connection_checks), fake.connection_checks


# -------------------------------------------------------- duplicate protection
def test_existing_local_mapping_causes_no_request(pool, db_conn, fake) -> None:
    task, job_id = make_task(db_conn, clickup_task_id="86existing")
    assert run(adapter_for(pool, fake)).final_state == "succeeded"
    assert fake.requests == []
    assert task_row(db_conn, task)["clickup_task_id"] == "86existing"


def test_repeated_runs_create_one_task(pool, db_conn, fake) -> None:
    make_task(db_conn)
    adapter = adapter_for(pool, fake)
    assert run(adapter).final_state == "succeeded"
    for _ in range(3):
        assert run(adapter) is None
    assert len(fake.posts) == 1 and len(fake.tasks) == 1


def test_lease_expiry_after_create_adopts_existing_task_without_post(pool, db_conn, fake) -> None:
    task, job_id = make_task(db_conn)
    adapter = adapter_for(pool, fake)
    runner.claim_next(registry_for(adapter), worker_id="crashed", lease_seconds=LEASE)
    created = fake.add_task(task_name("Kickoff call", task), marker(task))  # crashed worker had created it
    expire_lease(db_conn, job_id)
    report = run(adapter, "w2")
    assert (report.attempt_number, report.final_state) == (2, "succeeded")
    assert fake.posts == [] and len(fake.gets) == 1
    get = fake.gets[0]
    assert get.url.params["include_closed"] == "true" and get.url.params["page"] == "0"
    assert int(get.url.params["date_created_gt"]) > 0
    assert task_row(db_conn, task) == {"clickup_task_id": created["id"], "clickup_url": created["url"]}
    act = db_conn.execute("SELECT data FROM activity_events WHERE event_type = 'integration.clickup_reconciled'"
                          ).fetchone()["data"]
    assert act["decision"] == "adopted_existing" and act["clickup_task_id"] == created["id"]


def test_create_then_timeout_is_unknown_then_adopted_after_confirmation(pool, db_conn, fake) -> None:
    task, job_id = make_task(db_conn)
    adapter = adapter_for(pool, fake)
    fake.create_then(httpx.ReadTimeout)
    report = run(adapter)
    assert (report.outcome, report.final_state) == ("unknown", "dead")
    assert job(db_conn, job_id)["last_error_code"] == "UNKNOWN_OUTCOME_REQUIRES_RECONCILIATION"
    assert last_attempt(db_conn, job_id)["error_code"] == "CLICKUP_CREATE_OUTCOME_UNKNOWN"
    assert len(fake.tasks) == 1  # it WAS created
    with pytest.raises(ReconciliationRequiredError):
        retry_dead_job(job_id, additional_attempts=2)
    retry_dead_job(job_id, additional_attempts=2, reconciliation_confirmed=True)
    fake.post_behavior = None
    assert run(adapter).final_state == "succeeded"
    assert len(fake.posts) == 1 and len(fake.tasks) == 1  # adopted, no second POST
    assert task_row(db_conn, task)["clickup_task_id"] == fake.tasks[0]["id"]


def test_unknown_with_no_match_refuses_to_create(pool, db_conn, fake) -> None:
    task, job_id = make_task(db_conn)
    adapter = adapter_for(pool, fake)
    runner.claim_next(registry_for(adapter), worker_id="crashed", lease_seconds=LEASE)
    expire_lease(db_conn, job_id)
    report = run(adapter, "w2")
    assert (report.outcome, report.final_state) == ("unknown", "dead")
    assert last_attempt(db_conn, job_id)["error_code"] == "CLICKUP_RECONCILIATION_REQUIRED"
    assert fake.posts == [] and task_row(db_conn, task)["clickup_task_id"] is None
    act = db_conn.execute("SELECT data FROM activity_events WHERE event_type = 'integration.job_dead'").fetchone()["data"]
    assert act["result_error_code"] == "CLICKUP_RECONCILIATION_REQUIRED"


def test_confirmed_reconciliation_with_complete_scan_creates(pool, db_conn, fake) -> None:
    task, job_id = make_task(db_conn)
    adapter = adapter_for(pool, fake)
    runner.claim_next(registry_for(adapter), worker_id="crashed", lease_seconds=LEASE)
    expire_lease(db_conn, job_id)
    assert run(adapter, "w2").final_state == "dead"
    retry_dead_job(job_id, additional_attempts=2, reconciliation_confirmed=True, actor="ops@agency")
    payload = job(db_conn, job_id)["payload"]
    assert payload["reconciliation"]["confirmed_after_attempt"] == 2
    assert payload["reconciliation"]["confirmed_by"] == "ops@agency"
    # A brand-new adapter instance (i.e. after a worker restart) still honours it.
    fresh = adapter_for(pool, fake)
    assert run(fresh, "w3").final_state == "succeeded"
    assert len(fake.posts) == 1 and len(fake.gets) == 2
    act = db_conn.execute("SELECT data FROM activity_events WHERE event_type = 'integration.clickup_reconciled'"
                          ).fetchone()["data"]
    assert act["decision"] == "created_after_reconciliation"


def test_incomplete_scan_refuses_to_create_even_when_confirmed(pool, db_conn, fake) -> None:
    task, job_id = make_task(db_conn)
    fake.page_size = 2
    for i in range(5):
        fake.add_task(f"Unrelated task {i}")  # 5 tasks, 2 per page -> 3 pages needed
    adapter = adapter_for(pool, fake, max_scan_pages=2)
    fake.post_behavior = lambda req: (_ for _ in ()).throw(httpx.WriteTimeout("lost", request=req))
    assert run(adapter).final_state == "dead"  # uncertain POST
    retry_dead_job(job_id, additional_attempts=2, reconciliation_confirmed=True)
    report = run(adapter)
    assert report.final_state == "dead"
    assert last_attempt(db_conn, job_id)["error_code"] == "CLICKUP_RECONCILIATION_INCOMPLETE"
    assert len(fake.gets) == 2 and len(fake.posts) == 1  # page limit hit; no second POST


def test_multiple_markers_refuse_to_create(pool, db_conn, fake) -> None:
    task, job_id = make_task(db_conn)
    adapter = adapter_for(pool, fake)
    runner.claim_next(registry_for(adapter), worker_id="crashed", lease_seconds=LEASE)
    fake.add_task(task_name("Kickoff call", task))
    fake.add_task("copy", f"duplicated by hand {marker(task)}")
    expire_lease(db_conn, job_id)
    report = run(adapter, "w2")
    assert report.final_state == "dead"
    assert last_attempt(db_conn, job_id)["error_code"] == "CLICKUP_DUPLICATE_MARKER"
    assert fake.posts == [] and task_row(db_conn, task)["clickup_task_id"] is None


def test_reconciliation_get_failure_is_retryable_and_next_attempt_still_reconciles(pool, db_conn, fake) -> None:
    task, job_id = make_task(db_conn)
    adapter = adapter_for(pool, fake)
    runner.claim_next(registry_for(adapter), worker_id="crashed", lease_seconds=LEASE)
    expire_lease(db_conn, job_id)
    fake.get_behavior = lambda req: httpx.Response(503)
    assert run(adapter, "w2").final_state == "retry_scheduled"
    assert last_attempt(db_conn, job_id)["error_code"] == "CLICKUP_HTTP_503"
    # Attempt 3's immediate predecessor was "retryable", yet the earlier unknown
    # still forces reconciliation: no blind POST.
    fake.get_behavior = None
    db_conn.execute("UPDATE integration_jobs SET next_run_at = now() WHERE id = %s", (job_id,))
    assert run(adapter, "w3").final_state == "dead"
    assert last_attempt(db_conn, job_id)["error_code"] == "CLICKUP_RECONCILIATION_REQUIRED"
    assert fake.posts == []


def test_marker_matched_in_description_or_markdown(pool, db_conn, fake) -> None:
    task, job_id = make_task(db_conn)
    adapter = adapter_for(pool, fake)
    runner.claim_next(registry_for(adapter), worker_id="crashed", lease_seconds=LEASE)
    t = fake.add_task("Renamed by a human", "")
    t["markdown_description"] = f"notes\n{marker(task)}"
    expire_lease(db_conn, job_id)
    assert run(adapter, "w2").final_state == "succeeded"
    assert task_row(db_conn, task)["clickup_task_id"] == t["id"]


# ------------------------------------------------------ callback / fencing
def test_callback_does_not_overwrite_a_different_mapping(pool, db_conn, fake) -> None:
    task, job_id = make_task(db_conn)
    adapter = adapter_for(pool, fake)
    claim = runner.claim_next(registry_for(adapter), worker_id="w1", lease_seconds=LEASE)
    result = runner.execute(claim)
    db_conn.execute("UPDATE tasks SET clickup_task_id = '86other' WHERE id = %s", (task,))
    with pytest.raises(RuntimeError, match="different ClickUp task"):
        runner.finalize(claim, result)
    assert task_row(db_conn, task)["clickup_task_id"] == "86other"
    assert job(db_conn, job_id)["status"] == "running"  # success not recorded


def test_callback_failure_rolls_back_mapping_and_success(pool, db_conn, fake) -> None:
    task_a, _ = make_task(db_conn, with_job=False, clickup_task_id="86ab0001")  # already owns the id the fake will return
    task_b, job_b = make_task(db_conn)
    with pytest.raises(Exception):
        run(adapter_for(pool, fake))  # UNIQUE(clickup_task_id) violation inside the callback
    assert task_row(db_conn, task_b)["clickup_task_id"] is None
    row = job(db_conn, job_b)
    assert row["status"] == "running" and row["completed_at"] is None
    assert last_attempt(db_conn, job_b)["outcome"] is None


def test_stale_worker_cannot_persist_mapping(pool, db_conn, fake) -> None:
    task, job_id = make_task(db_conn)
    adapter = adapter_for(pool, fake)
    stale = runner.claim_next(registry_for(adapter), worker_id="w1", lease_seconds=LEASE)
    stale_result = runner.execute(stale)  # creates the task in ClickUp
    expire_lease(db_conn, job_id)
    assert run(adapter, "w2").final_state == "succeeded"  # adopts the same task by marker
    assert runner.finalize(stale, stale_result) == "lease_lost"
    assert len(fake.tasks) == 1 and len(fake.posts) == 1
    assert task_row(db_conn, task)["clickup_task_id"] == fake.tasks[0]["id"]


# --------------------------------------------------------- error handling
def _respond(status, headers=None, body=None):
    return lambda req: httpx.Response(status, headers=headers or {}, json=body if body is not None else {
        "err": f"Detailed error mentioning secret {TOKEN} and jane@client.example", "ECODE": "SOME_CODE"})


@pytest.mark.parametrize(("status", "code"), [(400, "CLICKUP_BAD_REQUEST"), (401, "CLICKUP_UNAUTHORIZED"),
                                              (403, "CLICKUP_FORBIDDEN"), (404, "CLICKUP_LIST_NOT_FOUND"),
                                              (422, "CLICKUP_HTTP_422")])
def test_permanent_http_errors(pool, db_conn, fake, status, code) -> None:
    _, job_id = make_task(db_conn)
    fake.post_behavior = _respond(status)
    assert run(adapter_for(pool, fake)).final_state == "dead"
    a = last_attempt(db_conn, job_id)
    assert (a["outcome"], a["error_code"], a["http_status"]) == ("permanent", code, status)
    assert "SOME_CODE" in a["error_message"]
    assert TOKEN not in str(a) and "jane@client.example" not in str(a) and "Detailed error" not in str(a)


def test_wrong_token_is_401_permanent(pool, db_conn, fake) -> None:
    _, job_id = make_task(db_conn)
    fake.token = "pk_the_real_one_" + "z" * 20
    assert run(adapter_for(pool, fake)).final_state == "dead"
    assert job(db_conn, job_id)["last_error_code"] == "CLICKUP_UNAUTHORIZED"


def test_wrong_list_is_404_permanent(pool, db_conn, fake) -> None:
    _, job_id = make_task(db_conn)
    assert run(adapter_for(pool, fake, clickup_list_id="111")).final_state == "dead"
    assert job(db_conn, job_id)["last_error_code"] == "CLICKUP_LIST_NOT_FOUND"


def test_429_uses_rate_limit_reset_and_is_bounded(pool, db_conn, fake) -> None:
    _, job_id = make_task(db_conn)
    fake.post_behavior = _respond(429, headers={"X-RateLimit-Reset": str(int(time.time()) + 40),
                                                "X-RateLimit-Remaining": "0"})
    assert run(adapter_for(pool, fake)).final_state == "retry_scheduled"
    wait = db_conn.execute("SELECT EXTRACT(EPOCH FROM next_run_at - now()) AS s FROM integration_jobs WHERE id = %s",
                           (job_id,)).fetchone()["s"]
    assert 35 <= wait <= 42
    a = last_attempt(db_conn, job_id)
    assert (a["outcome"], a["error_code"], a["http_status"]) == ("retryable", "CLICKUP_RATE_LIMITED", 429)

    _, job2 = make_task(db_conn)
    db_conn.execute("UPDATE integration_jobs SET next_run_at = now() + interval '1 hour' WHERE id = %s", (job_id,))
    fake.post_behavior = _respond(429, headers={"X-RateLimit-Reset": str(int(time.time()) + 99999)})
    run(adapter_for(pool, fake, worker_max_retry_delay_seconds=300))
    wait2 = db_conn.execute("SELECT EXTRACT(EPOCH FROM next_run_at - now()) AS s FROM integration_jobs WHERE id = %s",
                            (job2,)).fetchone()["s"]
    assert 295 <= wait2 <= 300


def test_429_without_headers_falls_back_to_backoff(pool, db_conn, fake) -> None:
    _, job_id = make_task(db_conn)
    fake.post_behavior = _respond(429)
    run(adapter_for(pool, fake))
    wait = db_conn.execute("SELECT EXTRACT(EPOCH FROM next_run_at - now()) AS s FROM integration_jobs WHERE id = %s",
                           (job_id,)).fetchone()["s"]
    assert 27 <= wait <= 30


@pytest.mark.parametrize("exc", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
def test_failures_before_dispatch_are_retryable(pool, db_conn, fake, exc) -> None:
    _, job_id = make_task(db_conn)
    fake.post_behavior = lambda req: (_ for _ in ()).throw(exc("boom", request=req))
    assert run(adapter_for(pool, fake)).final_state == "retry_scheduled"
    assert last_attempt(db_conn, job_id)["error_code"] == "CLICKUP_CONNECT_FAILED"
    assert fake.tasks == []


@pytest.mark.parametrize("exc", [httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError, httpx.ReadError])
def test_failures_after_dispatch_are_unknown(pool, db_conn, fake, exc) -> None:
    _, job_id = make_task(db_conn)
    fake.post_behavior = lambda req: (_ for _ in ()).throw(exc("lost", request=req))
    report = run(adapter_for(pool, fake))
    assert (report.outcome, report.final_state) == ("unknown", "dead")
    assert last_attempt(db_conn, job_id)["error_code"] == "CLICKUP_CREATE_OUTCOME_UNKNOWN"


@pytest.mark.parametrize("status", [408, 500, 502, 503, 504])
def test_408_and_5xx_after_post_are_unknown(pool, db_conn, fake, status) -> None:
    _, job_id = make_task(db_conn)
    fake.post_behavior = _respond(status)
    report = run(adapter_for(pool, fake))
    assert (report.outcome, report.final_state) == ("unknown", "dead")
    a = last_attempt(db_conn, job_id)
    assert (a["error_code"], a["http_status"]) == ("CLICKUP_CREATE_OUTCOME_UNKNOWN", status)


@pytest.mark.parametrize("body", [b"not json", b'{"no_id": true}', b'{"id": "bad id with spaces"}',
                                  b'{"id": "86x", "url": "http://insecure.example"}'])
def test_malformed_success_is_unknown(pool, db_conn, fake, body) -> None:
    _, job_id = make_task(db_conn)
    fake.post_behavior = lambda req: httpx.Response(200, content=body)
    report = run(adapter_for(pool, fake))
    assert (report.outcome, report.final_state) == ("unknown", "dead")
    assert last_attempt(db_conn, job_id)["error_code"] == "CLICKUP_MALFORMED_RESPONSE"


def test_logs_never_contain_token_or_customer_data(pool, db_conn, fake, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    make_task(db_conn)
    run(adapter_for(pool, fake))
    make_task(db_conn)
    fake.post_behavior = _respond(401)
    run(adapter_for(pool, fake))
    for secret in (TOKEN, "@client.example", "+15550000", "Confirm goals and scope."):
        assert secret not in caplog.text


# ------------------------------------------------------- worker regression
def test_end_to_end_onboarding_to_clickup_leaves_slack_and_sheets_pending(db_conn, migrated_database,
                                                                          pool_cleanup, fake) -> None:
    from tests.crm_helpers import webhook_client
    from tests.portal_helpers import new_invitation, submit

    with webhook_client(migrated_database) as client:
        inv = new_invitation(client)
        assert submit(client, inv["token"]).status_code == 201
    db.configure_pool(migrated_database)
    adapter = adapter_for(migrated_database, fake)
    processed = 0
    while run(adapter) is not None:
        processed += 1
    assert processed == 10 and len(fake.posts) == 10 and len(fake.tasks) == 10
    rows = db_conn.execute("SELECT job_type, status, attempts FROM integration_jobs ORDER BY job_type").fetchall()
    assert {(r["job_type"], r["status"], r["attempts"]) for r in rows} == {
        ("clickup.create_task", "succeeded", 1), ("sheets.upsert_project", "pending", 0),
        ("slack.notify", "pending", 0)}
    mapped = db_conn.execute("SELECT count(*) AS n FROM tasks WHERE clickup_task_id IS NOT NULL "
                             "AND clickup_url LIKE 'https://app.clickup.com/t/%%'").fetchone()["n"]
    assert mapped == 10
    names = [json.loads(p.content)["name"] for p in fake.posts]
    task_ids = [str(r["id"]) for r in db_conn.execute("SELECT id FROM tasks").fetchall()]
    assert sorted(n.rsplit("[LTD-TASK:", 1)[1].rstrip("]") for n in names) == sorted(task_ids)
    # The project and client are untouched by integration processing.
    assert db_conn.execute("SELECT count(*) AS n FROM projects").fetchone()["n"] == 1


# ------------------------------------------------------- live-check script
def test_live_check_script_logic_against_fake(db_conn, migrated_database, pool_cleanup, fake) -> None:
    """Exercises the live script's steps against the FAKE server only. This
    does NOT count as a live ClickUp test."""
    from app.integrations.clickup_live_check import live_check

    lines: list[str] = []
    result = live_check(settings_for(migrated_database), transport=fake.transport(), out=lines.append)
    assert len(fake.posts) == 1 and result["clickup_task_id"] == fake.tasks[0]["id"]
    assert any(line.startswith("4. reconciliation found exactly one task") for line in lines)
    assert all(TOKEN not in line for line in lines)


def test_live_check_refuses_other_pending_jobs_and_wrong_db(db_conn, migrated_database, pool_cleanup, fake) -> None:
    from app.integrations.clickup_live_check import LiveCheckError, live_check

    make_task(db_conn)  # a pending ClickUp job that is not the live check's
    with pytest.raises(LiveCheckError, match="other ClickUp job"):
        live_check(settings_for(migrated_database), transport=fake.transport(), out=lambda _: None)
    assert fake.requests == []


def test_live_check_requires_explicit_switch(monkeypatch) -> None:
    from app.integrations import clickup_live_check

    monkeypatch.delenv("CLICKUP_LIVE_TEST", raising=False)
    assert clickup_live_check.main() == 2
