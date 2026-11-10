"""Google Sheets adapter: service-account auth, configuration, worksheet
validation, data mapping, upsert, duplicate protection, unknown-append
reconciliation, classification and secret protection (fake Google via
httpx.MockTransport; PostgreSQL ltd_test)."""

import json
import logging
import time
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app import db
from app.errors import ReconciliationRequiredError
from app.integrations.google_auth import SHEETS_SCOPE, CredentialError, load_service_account_info
from app.integrations.sheets import HEADER, SheetsAdapter
from app.jobs import runner
from app.jobs.registry import HandlerRegistry
from app.services.job_admin import retry_dead_job
from tests.conftest import make_settings
from tests.fake_google import ACCESS_TOKEN, SERVICE_EMAIL, SPREADSHEET_ID, FakeGoogle, write_key_file
from tests.job_helpers import attempts, expire_lease, job


@pytest.fixture(scope="session")
def private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


@pytest.fixture
def key_file(tmp_path, private_key_pem) -> Path:
    return write_key_file(tmp_path, private_key_pem)


@pytest.fixture
def pool(db_conn, migrated_database, pool_cleanup):
    db.configure_pool(migrated_database)
    yield migrated_database


@pytest.fixture
def fake() -> FakeGoogle:
    return FakeGoogle()


def adapter_for(url, fake, key_file, *, max_scan_rows=10_000, **overrides) -> SheetsAdapter:
    values = {"google_service_account_file": key_file, "google_sheets_spreadsheet_id": SPREADSHEET_ID,
              "http_timeout_seconds": 5, **overrides}
    return SheetsAdapter(make_settings(url, **values), transport=fake.transport(), max_scan_rows=max_scan_rows)


def run(adapter, worker="w1"):
    registry = HandlerRegistry()
    registry.register(adapter.handler())
    return runner.run_one(registry, worker_id=worker, lease_seconds=120)


def make_project(conn, *, name="Website Development", company="Example Agency", tasks=4, done=1,
                 with_job=True, version=1):
    client = conn.execute(
        "INSERT INTO clients (name, email, phone, company) VALUES ('Jane Client', "
        "'jane+' || gen_random_uuid() || '@client.example', '+15550100', %s) RETURNING id", (company,)).fetchone()["id"]
    template = conn.execute("SELECT id FROM project_templates WHERE key = 'website_build'").fetchone()["id"]
    project = conn.execute(
        "INSERT INTO projects (client_id, template_id, template_version, name, start_date, due_date) "
        "VALUES (%s, %s, 1, %s, '2026-11-08', '2026-12-20') RETURNING id", (client, template, name)).fetchone()["id"]
    conn.execute("INSERT INTO onboarding_submissions (invitation_id, client_id, answers) SELECT i.id, %s, "
                 "'{\"business_goals\": \"SECRET-ANSWER\"}' FROM (SELECT NULL::uuid AS id) i WHERE false", (client,))
    for i in range(tasks):
        conn.execute("INSERT INTO tasks (project_id, title, position, status) VALUES (%s, %s, %s, %s)",
                     (project, f"Task {i}", i, "done" if i < done else "todo"))
    job_id = queue(conn, project, version) if with_job else None
    return project, job_id


def queue(conn, project, version):
    return conn.execute(
        "INSERT INTO integration_jobs (job_type, idempotency_key, coalesce_key, subject_type, subject_id, payload, max_attempts) "
        "VALUES ('sheets.upsert_project', %s, %s, 'project', %s, %s, 5) RETURNING id",
        (f"sheets.upsert_project:{project}:{version}", f"sheets.upsert_project:{project}", project,
         json.dumps({"sync_version": version}))).fetchone()["id"]


def last_attempt(conn, job_id):
    return attempts(conn, job_id)[-1]


# ------------------------------------------------------------ authentication
def test_service_account_token_refresh_and_scope(pool, db_conn, fake, key_file) -> None:
    make_project(db_conn)
    adapter = adapter_for(pool, fake, key_file)
    assert run(adapter).final_state == "succeeded"
    (token_call,) = fake.token_requests
    assert token_call["form"]["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    assert token_call["claims"]["scope"] == SHEETS_SCOPE
    assert token_call["claims"]["iss"] == SERVICE_EMAIL
    assert all(r.headers.get("authorization") == f"Bearer {ACCESS_TOKEN}" for r in fake.requests if fake.kind(r) != "token")
    make_project(db_conn)
    run(adapter)
    assert len(fake.token_requests) == 1  # token cached in memory and reused


@pytest.mark.parametrize(("mutate", "code"), [
    (lambda d, pem: None, "SHEETS_NOT_CONFIGURED"),
    (lambda d, pem: d / "missing.json", "SHEETS_CREDENTIALS_FILE_NOT_FOUND"),
    (lambda d, pem: _write(d, "not json {"), "SHEETS_INVALID_CREDENTIALS"),
    (lambda d, pem: _write(d, "[1, 2]"), "SHEETS_INVALID_CREDENTIALS"),
    (lambda d, pem: write_key_file(d, pem, type="authorized_user"), "SHEETS_INVALID_CREDENTIALS"),
    (lambda d, pem: write_key_file(d, pem, private_key=None), "SHEETS_INVALID_CREDENTIALS"),
    (lambda d, pem: write_key_file(d, pem, client_email="someone@gmail.com"), "SHEETS_INVALID_CREDENTIALS"),
    (lambda d, pem: write_key_file(d, pem, token_uri="https://evil.example/token"), "SHEETS_INVALID_CREDENTIALS"),
    (lambda d, pem: write_key_file(d, "-----BEGIN PRIVATE KEY-----\nnot a key\n-----END PRIVATE KEY-----\n"),
     "SHEETS_INVALID_CREDENTIALS"),
])
def test_invalid_credentials_are_permanent_without_requests(pool, db_conn, fake, tmp_path, private_key_pem, mutate, code):
    _, job_id = make_project(db_conn)
    key = mutate(tmp_path, private_key_pem)
    assert run(adapter_for(pool, fake, key)).final_state == "dead"
    assert job(db_conn, job_id)["last_error_code"] == code
    assert fake.requests == []


def _write(directory: Path, text: str) -> Path:
    path = directory / "bad.json"
    path.write_text(text)
    return path


def test_credential_errors_never_echo_file_contents(tmp_path, private_key_pem) -> None:
    path = write_key_file(tmp_path, private_key_pem, type="authorized_user")
    with pytest.raises(CredentialError) as excinfo:
        load_service_account_info(path)
    assert "PRIVATE KEY" not in str(excinfo.value) and str(path) not in str(excinfo.value)
    assert SERVICE_EMAIL not in str(excinfo.value)


def test_token_refresh_network_failure_is_retryable(pool, db_conn, fake, key_file) -> None:
    _, job_id = make_project(db_conn)
    fake.token_behavior = lambda req: (_ for _ in ()).throw(httpx.ConnectError("down", request=req))
    assert run(adapter_for(pool, fake, key_file)).final_state == "retry_scheduled"
    assert last_attempt(db_conn, job_id)["error_code"] == "SHEETS_TOKEN_REFRESH_FAILED"
    assert fake.calls("read") == []


def test_token_rejected_is_permanent(pool, db_conn, fake, key_file) -> None:
    _, job_id = make_project(db_conn)
    fake.token_behavior = lambda req: httpx.Response(400, json={"error": "invalid_grant",
                                                                "error_description": "Invalid JWT Signature."})
    assert run(adapter_for(pool, fake, key_file)).final_state == "dead"
    assert job(db_conn, job_id)["last_error_code"] == "SHEETS_INVALID_CREDENTIALS"


# ------------------------------------------------------------ configuration
@pytest.mark.parametrize(("overrides", "code"), [
    ({"google_sheets_spreadsheet_id": None}, "SHEETS_NOT_CONFIGURED"),
    ({"google_sheets_spreadsheet_id": "short"}, "SHEETS_INVALID_SPREADSHEET_ID"),
    ({"google_sheets_spreadsheet_id": "https://docs.google.com/spreadsheets/d/x"}, "SHEETS_INVALID_SPREADSHEET_ID"),
    ({"google_sheets_worksheet": ""}, "SHEETS_INVALID_WORKSHEET_NAME"),
    ({"google_sheets_worksheet": "Projects'!A1:Z9"}, "SHEETS_INVALID_WORKSHEET_NAME"),
])
def test_invalid_configuration(pool, db_conn, fake, key_file, overrides, code) -> None:
    _, job_id = make_project(db_conn)
    assert run(adapter_for(pool, fake, key_file, **overrides)).final_state == "dead"
    assert job(db_conn, job_id)["last_error_code"] == code
    assert fake.requests == []


def test_api_starts_without_sheets_configuration(pool) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    with TestClient(create_app(make_settings(pool, google_service_account_file="/nonexistent.json"))) as c:
        assert c.get("/api/health").status_code == 200


# ---------------------------------------------------------- worksheet setup
@pytest.mark.parametrize(("header", "code"), [
    ([], "SHEETS_SCHEMA_MISMATCH"),
    (HEADER[:12], "SHEETS_SCHEMA_MISMATCH"),
    ([HEADER[1], HEADER[0]] + HEADER[2:], "SHEETS_SCHEMA_MISMATCH"),
    (["Project ID"] + HEADER[1:], "SHEETS_SCHEMA_MISMATCH"),
])
def test_wrong_header_refuses_to_write(pool, db_conn, key_file, header, code) -> None:
    fake = FakeGoogle(header=header)
    _, job_id = make_project(db_conn)
    assert run(adapter_for(pool, fake, key_file)).final_state == "dead"
    assert job(db_conn, job_id)["last_error_code"] == code
    assert fake.calls("append") == [] and fake.calls("update") == []


def test_header_with_extra_spaces_is_accepted(pool, db_conn, key_file) -> None:
    fake = FakeGoogle(header=[f" {h} " for h in HEADER])
    make_project(db_conn)
    assert run(adapter_for(pool, fake, key_file)).final_state == "succeeded"


def test_missing_worksheet(pool, db_conn, key_file) -> None:
    fake = FakeGoogle(sheet="Other")
    _, job_id = make_project(db_conn)
    assert run(adapter_for(pool, fake, key_file)).final_state == "dead"
    assert job(db_conn, job_id)["last_error_code"] == "SHEETS_WORKSHEET_NOT_FOUND"


def test_worksheet_name_with_space_is_quoted(pool, db_conn, key_file) -> None:
    fake = FakeGoogle(sheet="Agency Projects")
    make_project(db_conn)
    assert run(adapter_for(pool, fake, key_file, google_sheets_worksheet="Agency Projects")).final_state == "succeeded"
    assert fake.calls("read")[0].url.params.get_list("ranges")[0] == "'Agency Projects'!A1:M1"


def test_large_but_complete_column_and_incomplete_scan(pool, db_conn, fake, key_file) -> None:
    for i in range(50):
        fake.rows.append([f"other-{i}"] + [""] * 12)
    project, job_id = make_project(db_conn)
    assert run(adapter_for(pool, fake, key_file, max_scan_rows=50)).final_state == "succeeded"  # exactly at the limit
    assert len(fake.find(str(project))) == 1
    project2, job2 = make_project(db_conn)  # now 51 rows > limit 50
    assert run(adapter_for(pool, fake, key_file, max_scan_rows=50)).final_state == "dead"
    assert job(db_conn, job2)["last_error_code"] == "SHEETS_SCAN_LIMIT_REACHED"
    assert fake.find(str(project2)) == []


# ------------------------------------------------------------- data mapping
def test_row_values_are_correct_and_minimal(pool, db_conn, fake, key_file) -> None:
    project, _ = make_project(db_conn, tasks=4, done=1)
    db_conn.execute("UPDATE tasks SET clickup_url = 'https://app.clickup.com/t/86first' "
                    "WHERE project_id = %s AND position = 0", (project,))
    run(adapter_for(pool, fake, key_file))
    (row,) = fake.find(str(project))
    values = dict(zip(HEADER, row))
    assert values["project_id"] == str(project)
    assert (values["client_name"], values["company_name"], values["project_name"]) == (
        "Jane Client", "Example Agency", "Website Development")
    assert (values["template_name"], values["project_status"]) == ("Website Build", "active")
    assert (values["progress_percent"], values["completed_tasks"], values["total_tasks"]) == (25, 1, 4)
    assert (values["start_date"], values["due_date"]) == ("2026-11-08", "2026-12-20")
    assert values["clickup_url"] == "https://app.clickup.com/t/86first"
    assert values["updated_at"].endswith("Z") and len(values["updated_at"]) == 20
    sent = " ".join(r.content.decode() for r in fake.requests if r.content)
    for private in ("@client.example", "+15550100", "SECRET-ANSWER"):
        assert private not in sent


def test_no_tasks_and_no_clickup(pool, db_conn, fake, key_file) -> None:
    project, _ = make_project(db_conn, tasks=0, done=0, company=None)
    run(adapter_for(pool, fake, key_file))
    values = dict(zip(HEADER, fake.find(str(project))[0]))
    assert (values["progress_percent"], values["completed_tasks"], values["total_tasks"]) == (0, 0, 0)
    assert values["clickup_url"] == "" and values["company_name"] == ""


def test_formula_like_values_are_sent_raw(pool, db_conn, fake, key_file) -> None:
    project, _ = make_project(db_conn, name='=IMPORTXML("https://evil.example","//a")', company="+cmd|' /C calc'!A0")
    run(adapter_for(pool, fake, key_file))
    append = fake.calls("append")[0]
    assert append.url.params["valueInputOption"] == "RAW"
    body = json.loads(append.content)["values"][0]
    assert body[3] == '=IMPORTXML("https://evil.example","//a")'  # stored as text, not a formula
    assert fake.find(str(project))[0][3].startswith("=IMPORTXML")


# ------------------------------------------------------------------- upsert
def test_append_then_update_one_row(pool, db_conn, fake, key_file) -> None:
    project, _ = make_project(db_conn, tasks=4, done=1)
    adapter = adapter_for(pool, fake, key_file)
    assert run(adapter).final_state == "succeeded"
    assert len(fake.calls("append")) == 1
    db_conn.execute("UPDATE tasks SET status = 'done' WHERE project_id = %s", (project,))
    db_conn.execute("UPDATE projects SET status = 'on_hold' WHERE id = %s", (project,))
    queue(db_conn, project, 2)
    assert run(adapter).final_state == "succeeded"
    assert len(fake.calls("append")) == 1 and len(fake.calls("update")) == 1
    (row,) = fake.find(str(project))
    values = dict(zip(HEADER, row))
    assert (values["project_status"], values["progress_percent"], values["completed_tasks"]) == ("on_hold", 100, 4)
    acts = [r["data"]["action"] for r in db_conn.execute(
        "SELECT data FROM activity_events WHERE event_type = 'integration.sheets_synced' ORDER BY created_at").fetchall()]
    assert acts == ["appended", "updated"]


def test_clickup_url_added_later_updates_existing_row(pool, db_conn, fake, key_file) -> None:
    project, _ = make_project(db_conn)
    adapter = adapter_for(pool, fake, key_file)
    run(adapter)
    db_conn.execute("UPDATE tasks SET clickup_url = 'https://app.clickup.com/t/86late' WHERE project_id = %s", (project,))
    queue(db_conn, project, 2)
    run(adapter)
    assert len(fake.find(str(project))) == 1
    assert dict(zip(HEADER, fake.find(str(project))[0]))["clickup_url"] == "https://app.clickup.com/t/86late"


def test_reordered_rows_are_found_again(pool, db_conn, fake, key_file) -> None:
    p1, _ = make_project(db_conn, name="First")
    p2, _ = make_project(db_conn, name="Second")
    adapter = adapter_for(pool, fake, key_file)
    run(adapter)
    run(adapter)
    fake.rows[1:] = [[f"x{i}"] + [""] * 12 for i in range(3)] + list(reversed(fake.rows[1:]))  # sort/move rows
    db_conn.execute("UPDATE projects SET status = 'completed' WHERE id = %s", (p1,))
    queue(db_conn, p1, 2)
    run(adapter)
    update = fake.calls("update")[-1]
    row_number = int(update.url.path.split("!A")[1].split(":")[0])
    assert str(fake.rows[row_number - 1][0]) == str(p1)
    assert dict(zip(HEADER, fake.find(str(p1))[0]))["project_status"] == "completed"
    assert dict(zip(HEADER, fake.find(str(p2))[0]))["project_status"] == "active"
    assert len(fake.ids()) == 5


def test_repeated_runs_keep_one_row(pool, db_conn, fake, key_file) -> None:
    project, _ = make_project(db_conn)
    adapter = adapter_for(pool, fake, key_file)
    for version in range(1, 5):
        if version > 1:
            queue(db_conn, project, version)
        assert run(adapter).final_state == "succeeded"
    assert len(fake.find(str(project))) == 1 and len(fake.calls("append")) == 1


def test_duplicate_rows_refuse_to_write(pool, db_conn, fake, key_file) -> None:
    project, job_id = make_project(db_conn)
    fake.rows += [[str(project)] + ["a"] * 12, [str(project)] + ["b"] * 12]
    assert run(adapter_for(pool, fake, key_file)).final_state == "dead"
    assert job(db_conn, job_id)["last_error_code"] == "SHEETS_DUPLICATE_PROJECT_ROWS"
    assert fake.calls("append") == [] and fake.calls("update") == []
    assert [r[1] for r in fake.find(str(project))] == ["a", "b"]  # untouched


# ------------------------------------------------------- append reconciliation
def test_append_timeout_is_unknown_and_never_auto_appended(pool, db_conn, fake, key_file) -> None:
    project, job_id = make_project(db_conn)
    adapter = adapter_for(pool, fake, key_file)

    def append_then_timeout(kind, request):
        if kind == "append":
            fake._append(request)
            raise httpx.ReadTimeout("lost", request=request)
    fake.behavior = append_then_timeout
    report = run(adapter)
    assert (report.outcome, report.final_state) == ("unknown", "dead")
    assert job(db_conn, job_id)["last_error_code"] == "UNKNOWN_OUTCOME_REQUIRES_RECONCILIATION"
    assert run(adapter) is None and len(fake.calls("append")) == 1


def test_unknown_append_then_found_row_is_adopted(pool, db_conn, fake, key_file) -> None:
    project, job_id = make_project(db_conn)
    adapter = adapter_for(pool, fake, key_file)
    fake.behavior = lambda kind, req: (fake._append(req), (_ for _ in ()).throw(httpx.WriteTimeout("x", request=req)))[1] if kind == "append" else None
    run(adapter)
    fake.behavior = None
    with pytest.raises(ReconciliationRequiredError):
        retry_dead_job(job_id, additional_attempts=2)
    retry_dead_job(job_id, additional_attempts=2, reconciliation_confirmed=True)
    assert run(adapter).final_state == "succeeded"
    assert len(fake.calls("append")) == 1 and len(fake.find(str(project))) == 1
    act = db_conn.execute("SELECT data FROM activity_events WHERE event_type = 'integration.sheets_synced'").fetchone()
    assert act["data"]["action"] == "adopted"


def test_newer_version_does_not_append_while_older_append_is_unknown(pool, db_conn, fake, key_file) -> None:
    project, v1 = make_project(db_conn)
    adapter = adapter_for(pool, fake, key_file)
    fake.behavior = lambda kind, req: httpx.Response(503) if kind == "append" else None  # nothing written
    assert run(adapter).final_state == "dead"
    fake.behavior = None
    v2 = queue(db_conn, project, 2)
    report = run(adapter)  # different job, same project: must not append blindly
    assert (report.job_id, report.final_state) == (v2, "dead")
    assert last_attempt(db_conn, v2)["error_code"] == "SHEETS_RECONCILIATION_REQUIRED"
    assert len(fake.calls("append")) == 1 and fake.find(str(project)) == []
    # Confirming absence on the newer job allows one append after a fresh scan.
    retry_dead_job(v2, additional_attempts=1, reconciliation_confirmed=True)
    fresh = adapter_for(pool, fake, key_file)  # new instance: confirmation comes from PostgreSQL
    assert run(fresh).final_state == "succeeded"
    assert len(fake.find(str(project))) == 1 and len(fake.calls("append")) == 2


def test_confirmed_absence_with_row_present_updates_instead(pool, db_conn, fake, key_file) -> None:
    project, job_id = make_project(db_conn)
    adapter = adapter_for(pool, fake, key_file)
    fake.behavior = lambda kind, req: httpx.Response(500) if kind == "append" else None
    run(adapter)
    fake.behavior = None
    fake.rows.append([str(project)] + ["stale"] * 12)  # appeared after all (or added by hand)
    retry_dead_job(job_id, additional_attempts=1, reconciliation_confirmed=True)
    assert run(adapter).final_state == "succeeded"
    assert len(fake.calls("append")) == 1 and len(fake.find(str(project))) == 1
    assert fake.find(str(project))[0][3] == "Website Development"


def test_confirmed_but_incomplete_scan_refuses(pool, db_conn, fake, key_file) -> None:
    project, job_id = make_project(db_conn)
    adapter = adapter_for(pool, fake, key_file, max_scan_rows=3)
    fake.behavior = lambda kind, req: httpx.Response(502) if kind == "append" else None
    run(adapter)
    fake.behavior = None
    for i in range(5):
        fake.rows.append([f"other-{i}"] + [""] * 12)
    retry_dead_job(job_id, additional_attempts=1, reconciliation_confirmed=True)
    assert run(adapter).final_state == "dead"
    assert last_attempt(db_conn, job_id)["error_code"] == "SHEETS_SCAN_LIMIT_REACHED"
    assert len(fake.calls("append")) == 1


@pytest.mark.parametrize("body", [{}, {"updates": {"updatedRows": 0}}, {"updates": {"updatedRange": "x", "updatedRows": 1}},
                                  {"updates": {"updatedRange": "'Projects'!A2:M3", "updatedRows": 1, "updatedColumns": 13}}])
def test_malformed_append_response_is_unknown(pool, db_conn, fake, key_file, body) -> None:
    _, job_id = make_project(db_conn)
    fake.behavior = lambda kind, req: httpx.Response(200, json=body) if kind == "append" else None
    report = run(adapter_for(pool, fake, key_file))
    assert (report.outcome, report.final_state) == ("unknown", "dead")
    assert last_attempt(db_conn, job_id)["error_code"] == "SHEETS_APPEND_OUTCOME_UNKNOWN"


def test_finalisation_failure_after_append_is_reconciled_without_second_append(pool, db_conn, fake, key_file) -> None:
    project, job_id = make_project(db_conn)
    adapter = adapter_for(pool, fake, key_file)
    registry = HandlerRegistry()
    registry.register(adapter.handler())
    claim = runner.claim_next(registry, worker_id="w1", lease_seconds=120)
    result = runner.execute(claim)  # appended in the sheet
    assert result.outcome == "success" and len(fake.find(str(project))) == 1
    # The worker dies before finalising; the lease expires.
    expire_lease(db_conn, job_id)
    report = run(adapter, "w2")
    assert (report.attempt_number, report.final_state) == (2, "succeeded")
    assert len(fake.calls("append")) == 1 and len(fake.find(str(project))) == 1
    assert [a["outcome"] for a in attempts(db_conn, job_id)] == ["unknown", "success"]


# --------------------------------------------------------------- update retry
def test_update_failure_is_retryable_and_relocates_row(pool, db_conn, fake, key_file) -> None:
    project, _ = make_project(db_conn)
    adapter = adapter_for(pool, fake, key_file)
    run(adapter)
    v2 = queue(db_conn, project, 2)
    fake.behavior = lambda kind, req: (_ for _ in ()).throw(httpx.ReadTimeout("x", request=req)) if kind == "update" else None
    assert run(adapter).final_state == "retry_scheduled"
    assert last_attempt(db_conn, v2)["error_code"] == "SHEETS_TRANSPORT_ERROR"
    fake.behavior = None
    fake.rows.insert(1, ["inserted-above"] + [""] * 12)  # the row moved down meanwhile
    db_conn.execute("UPDATE integration_jobs SET next_run_at = now() WHERE id = %s", (v2,))
    assert run(adapter).final_state == "succeeded"
    assert len(fake.find(str(project))) == 1 and len(fake.calls("read")) == 3
    rows = [int(u.url.path.split("!A")[1].split(":")[0]) for u in fake.calls("update")]
    assert rows == [2, 3]  # re-located, not reused


@pytest.mark.parametrize("status", [500, 503, 408])
def test_update_5xx_is_retryable(pool, db_conn, fake, key_file, status) -> None:
    project, _ = make_project(db_conn)
    adapter = adapter_for(pool, fake, key_file)
    run(adapter)
    v2 = queue(db_conn, project, 2)
    fake.behavior = lambda kind, req: httpx.Response(status) if kind == "update" else None
    assert run(adapter).final_state == "retry_scheduled"
    assert last_attempt(db_conn, v2)["outcome"] == "retryable"


# ---------------------------------------------------------- classification
def _err(status, reason=None, message="error", state="FAILED"):
    body = {"error": {"code": status, "message": message, "status": state}}
    if reason:
        body["error"]["errors"] = [{"reason": reason}]
    return httpx.Response(status, json=body)


@pytest.mark.parametrize(("response", "code", "outcome"), [
    (_err(400, "badRequest", "Invalid values"), "SHEETS_BAD_REQUEST", "permanent"),
    (_err(401, None, "Request had invalid authentication credentials.", "UNAUTHENTICATED"), "SHEETS_INVALID_CREDENTIALS", "permanent"),
    (_err(403, "forbidden", "The caller does not have permission", "PERMISSION_DENIED"), "SHEETS_PERMISSION_DENIED", "permanent"),
    (_err(404, None, "Requested entity was not found.", "NOT_FOUND"), "SHEETS_NOT_FOUND", "permanent"),
    (_err(403, "rateLimitExceeded"), "SHEETS_RATE_LIMITED", "retryable"),
    (_err(403, "userRateLimitExceeded"), "SHEETS_RATE_LIMITED", "retryable"),
    (_err(429, None, "Quota exceeded", "RESOURCE_EXHAUSTED"), "SHEETS_RATE_LIMITED", "retryable"),
    (_err(500), "SHEETS_HTTP_500", "retryable"),  # during the read (GET): safe to retry
])
def test_read_errors(pool, db_conn, fake, key_file, response, code, outcome) -> None:
    _, job_id = make_project(db_conn)
    fake.behavior = lambda kind, req: response if kind == "read" else None
    run(adapter_for(pool, fake, key_file))
    a = last_attempt(db_conn, job_id)
    assert (a["error_code"], a["outcome"]) == (code, outcome)
    assert "permission" not in (a["error_message"] or "").lower() or code == "SHEETS_PERMISSION_DENIED"


def test_real_spreadsheet_not_found(pool, db_conn, fake, key_file) -> None:
    _, job_id = make_project(db_conn)
    run(adapter_for(pool, fake, key_file, google_sheets_spreadsheet_id="1OtherSpreadsheetId_abcdefghijklmnopq"))
    assert job(db_conn, job_id)["last_error_code"] == "SHEETS_NOT_FOUND"


def test_429_retry_after_and_cap(pool, db_conn, fake, key_file) -> None:
    _, job_id = make_project(db_conn)
    fake.behavior = lambda kind, req: httpx.Response(429, headers={"Retry-After": "21"}) if kind == "read" else None
    assert run(adapter_for(pool, fake, key_file)).final_state == "retry_scheduled"
    wait = db_conn.execute("SELECT EXTRACT(EPOCH FROM next_run_at - now()) AS s FROM integration_jobs WHERE id = %s",
                           (job_id,)).fetchone()["s"]
    assert 17 <= wait <= 21
    _, job2 = make_project(db_conn)
    db_conn.execute("UPDATE integration_jobs SET next_run_at = now() + interval '1 hour' WHERE id = %s", (job_id,))
    fake.behavior = lambda kind, req: httpx.Response(429, headers={"Retry-After": "99999"}) if kind == "read" else None
    run(adapter_for(pool, fake, key_file, worker_max_retry_delay_seconds=90))
    wait2 = db_conn.execute("SELECT EXTRACT(EPOCH FROM next_run_at - now()) AS s FROM integration_jobs WHERE id = %s",
                            (job2,)).fetchone()["s"]
    assert 85 <= wait2 <= 90


def test_429_without_retry_after_uses_backoff(pool, db_conn, fake, key_file) -> None:
    _, job_id = make_project(db_conn)
    fake.behavior = lambda kind, req: httpx.Response(429) if kind == "read" else None
    run(adapter_for(pool, fake, key_file))
    wait = db_conn.execute("SELECT EXTRACT(EPOCH FROM next_run_at - now()) AS s FROM integration_jobs WHERE id = %s",
                           (job_id,)).fetchone()["s"]
    assert 27 <= wait <= 30


@pytest.mark.parametrize("exc", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
def test_connect_failures_are_retryable(pool, db_conn, fake, key_file, exc) -> None:
    _, job_id = make_project(db_conn)
    fake.behavior = lambda kind, req: (_ for _ in ()).throw(exc("x", request=req)) if kind == "append" else None
    assert run(adapter_for(pool, fake, key_file)).final_state == "retry_scheduled"
    assert last_attempt(db_conn, job_id)["error_code"] == "SHEETS_CONNECT_FAILED"


@pytest.mark.parametrize("failure", ["read_timeout", "write_timeout", "protocol", "500", "503", "408"])
def test_append_failures_after_dispatch_are_unknown(pool, db_conn, fake, key_file, failure) -> None:
    _, job_id = make_project(db_conn)
    excs = {"read_timeout": httpx.ReadTimeout, "write_timeout": httpx.WriteTimeout, "protocol": httpx.RemoteProtocolError}

    def behaviour(kind, req):
        if kind != "append":
            return None
        if failure in excs:
            raise excs[failure]("x", request=req)
        return httpx.Response(int(failure))
    fake.behavior = behaviour
    report = run(adapter_for(pool, fake, key_file))
    assert (report.outcome, report.final_state) == ("unknown", "dead")
    assert last_attempt(db_conn, job_id)["error_code"] == "SHEETS_APPEND_OUTCOME_UNKNOWN"


# ------------------------------------------------- security and lifecycle
def test_no_secrets_in_logs_or_database(pool, db_conn, fake, key_file, private_key_pem, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    for behavior in (None, lambda k, r: _err(403, "forbidden", f"no access for {SERVICE_EMAIL}") if k == "read" else None,
                     lambda k, r: httpx.Response(500, text=ACCESS_TOKEN) if k == "append" else None):
        make_project(db_conn)
        fake.behavior = behavior
        run(adapter_for(pool, fake, key_file))
    dump = " ".join(str(r) for t in ("integration_jobs", "integration_attempts", "activity_events")
                    for r in db_conn.execute(f"SELECT t::text FROM {t} t").fetchall())
    key_body = private_key_pem.splitlines()[1]
    for secret in (ACCESS_TOKEN, key_body, SERVICE_EMAIL, str(key_file)):
        assert secret not in caplog.text, secret[:20]
        assert secret not in dump, secret[:20]


def test_no_connection_held_during_token_refresh_or_api_calls(pool, db_conn, fake, key_file) -> None:
    make_project(db_conn)
    run(adapter_for(pool, fake, key_file))
    assert {fake.kind(r) for r in fake.requests} == {"token", "read", "append"}
    assert all(c == {"held": False, "in_use": 0} for c in fake.checks), fake.checks


def test_status_endpoint_hides_paths_and_reports_validity(db_conn, migrated_database, pool_cleanup, key_file) -> None:
    from tests.admin_helpers import admin_app, logged_in

    with admin_app(migrated_database, google_service_account_file=key_file,
                   google_sheets_spreadsheet_id=SPREADSHEET_ID) as client:
        logged_in(client)
        response = client.get("/api/admin/integrations/status")
    sheets = response.json()["google_sheets"]
    assert sheets["implemented"] is True and sheets["configuration_valid"] is True
    assert (sheets["service_account_file_configured"], sheets["spreadsheet_id_configured"],
            sheets["worksheet_configured"]) == (True, True, True)
    assert sheets["live_verification"] == "not_verified"
    for secret in (str(key_file), "sa.json", SERVICE_EMAIL, "PRIVATE KEY"):
        assert secret not in response.text


def test_http_client_closes_and_tls_on(pool, fake, key_file) -> None:
    adapter = SheetsAdapter(make_settings(pool, google_service_account_file=key_file,
                                          google_sheets_spreadsheet_id=SPREADSHEET_ID))
    assert adapter._client.follow_redirects is False
    assert adapter._client._transport._pool._ssl_context.verify_mode.name == "CERT_REQUIRED"
    registry = HandlerRegistry()
    registry.register(adapter.handler())
    registry.close()
    assert adapter._client.is_closed


def test_worker_survives_sheets_failures(db_conn, migrated_database, pool_cleanup, fake, key_file) -> None:
    import threading

    from app.jobs import worker
    _, job_id = make_project(db_conn)
    fake.behavior = lambda kind, req: (_ for _ in ()).throw(RuntimeError("unexpected")) if kind == "read" else None
    adapter = adapter_for(migrated_database, fake, key_file)
    registry = HandlerRegistry()
    registry.register(adapter.handler())
    stop = threading.Event()
    thread = threading.Thread(target=worker.run, args=(make_settings(migrated_database, worker_poll_seconds=0.05),
                                                       registry, stop), kwargs={"worker_id": "w"})
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and job(db_conn, job_id)["status"] != "dead":
        time.sleep(0.05)
    stop.set()
    thread.join(timeout=10)
    assert job(db_conn, job_id)["last_error_code"] == "UNKNOWN_OUTCOME_REQUIRES_RECONCILIATION"  # HANDLER_EXCEPTION
    assert adapter._client.is_closed  # closed on shutdown


def test_end_to_end_onboarding_to_sheet(db_conn, migrated_database, pool_cleanup, fake, key_file) -> None:
    from tests.crm_helpers import webhook_client
    from tests.portal_helpers import new_invitation, submit

    with webhook_client(migrated_database) as client:
        submit(client, new_invitation(client)["token"])
    db.configure_pool(migrated_database)
    adapter = adapter_for(migrated_database, fake, key_file)
    while run(adapter) is not None:
        pass
    project = db_conn.execute("SELECT id FROM projects").fetchone()["id"]
    values = dict(zip(HEADER, fake.find(str(project))[0]))
    assert (values["template_name"], values["total_tasks"], values["progress_percent"]) == ("Website Build", 10, 0)
    rows = {(r["job_type"], r["status"]) for r in db_conn.execute("SELECT DISTINCT job_type, status FROM integration_jobs").fetchall()}
    assert ("sheets.upsert_project", "succeeded") in rows


# ---------------------------------------------------------- live-check script
def test_live_check_logic_against_fake(db_conn, migrated_database, pool_cleanup, fake, key_file) -> None:
    """The live script's 12 steps against the FAKE API only (not a live test)."""
    from app.integrations.sheets_live_check import live_check

    lines: list[str] = []
    settings = make_settings(migrated_database, google_service_account_file=key_file,
                             google_sheets_test_spreadsheet_id=SPREADSHEET_ID)
    live_check(settings, transport=fake.transport(), out=lines.append)
    assert len(fake.calls("append")) == 1 and len(fake.calls("update")) == 2
    assert fake.ids() == []  # cleaned up
    assert any(line.startswith("12. synthetic row deleted") for line in lines)
    assert all(ACCESS_TOKEN not in line and str(key_file) not in line for line in lines)


def test_live_check_refuses_reporting_spreadsheet_and_pending_jobs(db_conn, migrated_database, pool_cleanup, fake,
                                                                  key_file) -> None:
    from app.integrations.sheets_live_check import LiveCheckError, live_check

    same = make_settings(migrated_database, google_service_account_file=key_file,
                         google_sheets_spreadsheet_id=SPREADSHEET_ID, google_sheets_test_spreadsheet_id=SPREADSHEET_ID)
    with pytest.raises(LiveCheckError, match="must not be the reporting"):
        live_check(same, transport=fake.transport(), out=lambda _: None)
    make_project(db_conn)
    other = make_settings(migrated_database, google_service_account_file=key_file,
                          google_sheets_test_spreadsheet_id=SPREADSHEET_ID)
    with pytest.raises(LiveCheckError, match="other Sheets jobs"):
        live_check(other, transport=fake.transport(), out=lambda _: None)
    assert fake.calls("append") == []


def test_live_check_requires_explicit_switch(monkeypatch) -> None:
    from app.integrations import sheets_live_check

    monkeypatch.delenv("GOOGLE_SHEETS_LIVE_TEST", raising=False)
    assert sheets_live_check.main() == 2
