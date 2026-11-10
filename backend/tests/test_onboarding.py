"""Onboarding submission, template-driven project generation, outbox jobs,
progress, rollback and concurrency, against PostgreSQL ltd_test."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta

import pytest

from app import db
from app.errors import PortalAuthError
from app.schemas.onboarding import form_for
from app.schemas.portal import compute_progress
from app.schemas.templates import TemplateDefinition
from app.services import onboarding as onboarding_service
from app.services import portal
from app.store import activity as activity_store
from app.store import jobs as jobs_store
from app.store import onboarding as onboarding_store
from app.store import projects as projects_store
from tests.crm_helpers import webhook_client
from tests.portal_helpers import (
    EXPECTED_SHAPE,
    FORBIDDEN_KEYS,
    auth,
    counts,
    new_invitation,
    submit,
    valid_form,
    walk_keys,
)

ZERO = {t: 0 for t in ("onboarding_submissions", "projects", "milestones", "tasks", "integration_jobs")}


@pytest.fixture
def client(db_conn, migrated_database, pool_cleanup):
    with webhook_client(migrated_database, job_max_attempts=7) as c:
        yield c


def _definition(conn, key: str) -> TemplateDefinition:
    row = conn.execute("SELECT definition FROM project_templates WHERE key = %s", (key,)).fetchone()
    return TemplateDefinition.model_validate(row["definition"])


# ---------------------------------------------------------------- the form
@pytest.mark.parametrize(
    ("service_type", "detail_field"),
    [("website_build", "has_brand_assets"), ("seo_retainer", "priority_keywords"),
     ("marketing_automation_setup", "crm_platform")],
)
def test_form_definition_is_specific_to_the_invitation_service(client, db_conn, service_type, detail_field) -> None:
    inv = new_invitation(client, service_type=service_type)
    response = client.get("/api/portal/onboarding", headers=auth(inv["token"]))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending" and body["service_type"] == service_type
    assert body["prefill"] == {"project_name": inv["deal_name"]}
    schema = body["form_schema"]
    assert {"project_name", "business_goals", "communication", "service_details"} <= set(schema["properties"])
    details_ref = schema["properties"]["service_details"]["$ref"].split("/")[-1]
    assert detail_field in schema["$defs"][details_ref]["properties"]
    # Nothing the client could use to set internal state appears in the form.
    assert not {"status", "assignee", "project_id", "service_type", "template_id"} & set(schema["properties"])


# ------------------------------------------------------- project generation
@pytest.mark.parametrize("service_type", list(EXPECTED_SHAPE))
def test_submission_generates_project_from_template(client, db_conn, service_type) -> None:
    inv = new_invitation(client, service_type=service_type)
    response = submit(client, inv["token"], valid_form(service_type), service_type)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "submitted"
    definition = _definition(db_conn, service_type)
    n_milestones, n_tasks = EXPECTED_SHAPE[service_type]
    assert len(definition.milestones) == n_milestones
    assert sum(len(m.tasks) for m in definition.milestones) == n_tasks

    project = db_conn.execute("SELECT * FROM projects").fetchone()
    assert str(project["id"]) == body["project"]["id"]
    assert str(project["client_id"]) == inv["client_id"] and str(project["invitation_id"]) == inv["invitation_id"]
    assert project["name"] == "Acme Growth Project" and project["status"] == "active"
    start = project["start_date"]
    assert start == datetime.now(UTC).date()
    assert project["due_date"] == start + timedelta(days=definition.default_duration_days)
    template = db_conn.execute("SELECT id, version FROM project_templates WHERE key = %s", (service_type,)).fetchone()
    assert (project["template_id"], project["template_version"]) == (template["id"], template["version"])

    milestones = db_conn.execute(
        "SELECT * FROM milestones WHERE project_id = %s ORDER BY position", (project["id"],)).fetchall()
    assert [(m["template_milestone_key"], m["name"], m["position"], m["due_date"], m["status"]) for m in milestones] == [
        (m.key, m.name, i, start + timedelta(days=m.due_offset_days), "pending")
        for i, m in enumerate(definition.milestones)]

    tasks = db_conn.execute(
        "SELECT t.*, m.template_milestone_key FROM tasks t JOIN milestones m ON m.id = t.milestone_id "
        "WHERE t.project_id = %s ORDER BY t.position", (project["id"],)).fetchall()
    expected = [(m.key, t.key, t.title, start + timedelta(days=t.due_offset_days), t.client_visible)
                for m in definition.milestones for t in m.tasks]
    assert [(t["template_milestone_key"], t["template_task_key"], t["title"], t["due_date"], t["client_visible"])
            for t in tasks] == expected
    assert [t["position"] for t in tasks] == list(range(n_tasks))
    assert all(t["status"] == "todo" and t["completed_at"] is None and t["clickup_task_id"] is None for t in tasks)

    inv_row = db_conn.execute("SELECT status, submitted_at FROM onboarding_invitations").fetchone()
    assert inv_row["status"] == "submitted" and inv_row["submitted_at"] is not None
    sub = db_conn.execute("SELECT * FROM onboarding_submissions").fetchone()
    assert str(sub["invitation_id"]) == inv["invitation_id"] and str(sub["client_id"]) == inv["client_id"]
    assert sub["answers"]["project_description"] == "A new project to grow Acme's online presence."
    assert sub["answers"]["service_details"] == form_for(service_type).model_validate(
        valid_form(service_type)).model_dump(mode="json")["service_details"]


def test_template_change_is_reflected_without_code_changes(client, db_conn) -> None:
    db_conn.execute(
        """UPDATE project_templates SET definition = jsonb_set(definition, '{milestones,0,tasks}',
           (definition #> '{milestones,0,tasks}') || '[{"key": "extra_task", "title": "Extra task",
           "due_offset_days": 1, "client_visible": false}]'::jsonb) WHERE key = 'seo_retainer'""")
    try:
        inv = new_invitation(client, service_type="seo_retainer")
        assert submit(client, inv["token"], valid_form("seo_retainer")).status_code == 201
        keys = [r["template_task_key"] for r in db_conn.execute("SELECT template_task_key FROM tasks").fetchall()]
        assert "extra_task" in keys and len(keys) == 8
    finally:
        db_conn.execute(
            """UPDATE project_templates SET definition = jsonb_set(definition, '{milestones,0,tasks}',
               (SELECT jsonb_agg(t) FROM jsonb_array_elements(definition #> '{milestones,0,tasks}') t
                WHERE t->>'key' <> 'extra_task')) WHERE key = 'seo_retainer'""")


def test_invalid_template_definition_blocks_submission_safely(client, db_conn) -> None:
    inv = new_invitation(client, service_type="seo_retainer")
    original = db_conn.execute("SELECT definition FROM project_templates WHERE key = 'seo_retainer'").fetchone()
    db_conn.execute("UPDATE project_templates SET definition = '{\"schema_version\": 99}' WHERE key = 'seo_retainer'")
    try:
        response = submit(client, inv["token"], valid_form("seo_retainer"))
        assert (response.status_code, response.json()["code"]) == (503, "template_unavailable")
        current = counts(db_conn)
        assert {k: current[k] for k in ZERO} == ZERO
        assert db_conn.execute("SELECT status FROM onboarding_invitations").fetchone()["status"] == "pending"
    finally:
        from psycopg.types.json import Jsonb
        db_conn.execute("UPDATE project_templates SET definition = %s WHERE key = 'seo_retainer'",
                        (Jsonb(original["definition"]),))


# ---------------------------------------------------------- outbox jobs
def test_integration_jobs_are_queued_with_operation_specific_keys(client, db_conn) -> None:
    inv = new_invitation(client)
    project_id = submit(client, inv["token"]).json()["project"]["id"]
    jobs = db_conn.execute("SELECT * FROM integration_jobs ORDER BY job_type, idempotency_key").fetchall()
    task_ids = {str(r["id"]) for r in db_conn.execute("SELECT id FROM tasks").fetchall()}

    clickup = [j for j in jobs if j["job_type"] == "clickup.create_task"]
    assert len(clickup) == len(task_ids) == 10
    assert {str(j["subject_id"]) for j in clickup} == task_ids
    for j in clickup:
        assert j["idempotency_key"] == f"clickup.create_task:{j['subject_id']}"
        assert j["subject_type"] == "task" and j["coalesce_key"] is None
        assert j["payload"]["project_id"] == project_id and "assignee_role" in j["payload"]

    sheets = [j for j in jobs if j["job_type"] == "sheets.upsert_project"]
    assert len(sheets) == 1
    assert sheets[0]["idempotency_key"] == f"sheets.upsert_project:{project_id}:1"
    assert sheets[0]["coalesce_key"] == f"sheets.upsert_project:{project_id}"
    assert (sheets[0]["subject_type"], str(sheets[0]["subject_id"])) == ("project", project_id)
    assert sheets[0]["payload"] == {"sync_version": 1}

    slack = [j for j in jobs if j["job_type"] == "slack.notify"]
    assert len(slack) == 1
    assert slack[0]["idempotency_key"] == f"slack.notify:project_created:{project_id}"
    assert (slack[0]["subject_type"], str(slack[0]["subject_id"])) == ("project", project_id)

    assert len(jobs) == 12
    assert all(j["status"] == "pending" and j["attempts"] == 0 and j["max_attempts"] == 7
               and j["locked_by"] is None and j["completed_at"] is None for j in jobs)


def test_job_enqueue_prevents_duplicates_and_respects_coalescing(client, db_conn) -> None:
    inv = new_invitation(client)
    project_id = submit(client, inv["token"]).json()["project"]["id"]
    with db.transaction() as conn:
        # Same permanent key again: not queued.
        assert jobs_store.enqueue(conn, job_type="slack.notify", idempotency_key=f"slack.notify:project_created:{project_id}",
                                  subject_type="project", subject_id=project_id, max_attempts=5) is None
        # A newer Sheets version while version 1 is still pending: coalesced away.
        assert jobs_store.enqueue(conn, job_type="sheets.upsert_project", idempotency_key=f"sheets.upsert_project:{project_id}:2",
                                  coalesce_key=f"sheets.upsert_project:{project_id}", subject_type="project",
                                  subject_id=project_id, max_attempts=5) is None
    db_conn.execute("UPDATE integration_jobs SET status = 'succeeded', completed_at = now() "
                    "WHERE job_type = 'sheets.upsert_project'")
    with db.transaction() as conn:
        # Once version 1 has run, version 2 is queued normally.
        assert jobs_store.enqueue(conn, job_type="sheets.upsert_project", idempotency_key=f"sheets.upsert_project:{project_id}:2",
                                  coalesce_key=f"sheets.upsert_project:{project_id}", subject_type="project",
                                  subject_id=project_id, max_attempts=5) is not None


# ----------------------------------------------------- client project view
def test_project_view_is_safe_and_complete(client, db_conn) -> None:
    inv = new_invitation(client)
    body = submit(client, inv["token"]).json()["project"]
    again = client.get("/api/portal/project", headers=auth(inv["token"]))
    assert again.status_code == 200 and again.json() == body
    assert not walk_keys(body) & FORBIDDEN_KEYS

    definition = _definition(db_conn, "website_build")
    visible = [t.title for m in definition.milestones for t in m.tasks if t.client_visible]
    hidden = [t.title for m in definition.milestones for t in m.tasks if not t.client_visible]
    shown = [t["title"] for m in body["milestones"] for t in m["tasks"]] + [t["title"] for t in body["unassigned_tasks"]]
    assert shown == visible and hidden and not set(hidden) & set(shown)
    assert [m["name"] for m in body["milestones"]] == [m.name for m in definition.milestones]
    assert body["description"] == "A new project to grow Acme's online presence."
    assert body["progress"] == {"total_tasks": 10, "completed_tasks": 0, "open_tasks": 10, "percent": 0}
    # Only client-visible activity; internal intake/job activity is excluded.
    assert sorted(a["type"] for a in body["activity"]) == ["onboarding.submitted", "project.created"]
    assert db_conn.execute("SELECT count(*) AS n FROM activity_events WHERE NOT client_visible").fetchone()["n"] >= 4


def test_progress_counts_completed_tasks(client, db_conn) -> None:
    inv = new_invitation(client)
    submit(client, inv["token"])
    db_conn.execute("UPDATE tasks SET status = 'done', completed_at = now() WHERE position < 3")
    db_conn.execute("UPDATE tasks SET status = 'in_progress' WHERE position = 3")
    progress = client.get("/api/portal/project", headers=auth(inv["token"])).json()["progress"]
    assert progress == {"total_tasks": 10, "completed_tasks": 3, "open_tasks": 7, "percent": 30}
    db_conn.execute("UPDATE tasks SET status = 'done', completed_at = now() WHERE position < 9")
    assert client.get("/api/portal/project", headers=auth(inv["token"])).json()["progress"]["percent"] == 90
    db_conn.execute("UPDATE tasks SET status = 'done', completed_at = now()")
    assert client.get("/api/portal/project", headers=auth(inv["token"])).json()["progress"]["percent"] == 100


def test_progress_only_counts_this_projects_tasks(client, db_conn) -> None:
    a = new_invitation(client, email="a@example.com", company="A Co")
    b = new_invitation(client, email="b@example.com", company="B Co")
    submit(client, a["token"])
    submit(client, b["token"], valid_form("website_build"))
    db_conn.execute("UPDATE tasks SET status = 'done', completed_at = now() "
                    "WHERE project_id = (SELECT id FROM projects WHERE client_id = %s)", (b["client_id"],))
    assert client.get("/api/portal/project", headers=auth(a["token"])).json()["progress"]["completed_tasks"] == 0


@pytest.mark.parametrize(("total", "completed", "percent"), [(0, 0, 0), (3, 1, 33), (3, 2, 66), (7, 7, 100), (10, 0, 0)])
def test_progress_formula(total, completed, percent) -> None:
    progress = compute_progress(total, completed)
    assert (progress.percent, progress.open_tasks) == (percent, total - completed)


# ---------------------------------------------------------------- validation
@pytest.mark.parametrize(
    ("change", "loc"),
    [
        ({"project_name": None}, ["body", "project_name"]),
        ({"business_goals": "  "}, ["body", "business_goals"]),
        ({"desired_delivery_date": "2020-01-01"}, ["body", "desired_delivery_date"]),
        ({"desired_delivery_date": "2026-02-30"}, ["body", "desired_delivery_date"]),
        ({"desired_delivery_date": "next week"}, ["body", "desired_delivery_date"]),
        ({"desired_delivery_date": (date.today() + timedelta(days=900)).isoformat()}, ["body", "desired_delivery_date"]),
        ({"project_name": "x" * 121}, ["body", "project_name"]),
        ({"scope_requirements": "y" * 4001}, ["body", "scope_requirements"]),
        ({"project_name": "Bad\x00name"}, ["body", "project_name"]),
        ({"status": "done"}, ["body", "status"]),
        ({"project_id": "00000000-0000-0000-0000-000000000000"}, ["body", "project_id"]),
        ({"service_type": "seo_retainer"}, ["body", "service_type"]),
        ({"communication": {"preferred_channel": "carrier_pigeon"}}, ["body", "communication", "preferred_channel"]),
        ({"communication": {"preferred_channel": "email", "contact_email": "nope"}}, ["body", "communication", "contact_email"]),
        ({"service_details": {"has_brand_assets": True, "assignee": "me"}}, ["body", "service_details", "assignee"]),
        ({"service_details": {"has_brand_assets": True, "estimated_page_count": 0}}, ["body", "service_details", "estimated_page_count"]),
        ({"service_details": {"website_url": "https://x.example", "has_analytics_access": True}},
         ["body", "service_details", "has_brand_assets"]),
    ],
)
def test_invalid_submissions_are_422_and_write_nothing(client, db_conn, change, loc) -> None:
    inv = new_invitation(client)
    before = counts(db_conn)
    response = submit(client, inv["token"], {**valid_form("website_build"), **change})
    assert response.status_code == 422, response.text
    assert loc in [d["loc"] for d in response.json()["detail"]]
    assert all("input" not in d for d in response.json()["detail"])
    assert counts(db_conn) == before
    assert db_conn.execute("SELECT status FROM onboarding_invitations").fetchone()["status"] == "pending"


def test_malformed_json_wrong_type_and_oversize(client, db_conn) -> None:
    inv = new_invitation(client)
    headers = {**auth(inv["token"]), "Content-Type": "application/json"}
    assert client.post("/api/portal/onboarding", content=b"{not json", headers=headers).status_code == 422
    assert client.post("/api/portal/onboarding", content=b"[]", headers=headers).status_code == 422
    r = client.post("/api/portal/onboarding", content=b"project_name=x",
                    headers={**auth(inv["token"]), "Content-Type": "application/x-www-form-urlencoded"})
    assert (r.status_code, r.json()["code"]) == (415, "unsupported_media_type")
    big = client.post("/api/portal/onboarding", json=valid_form(additional_notes="z" * 40_000), headers=auth(inv["token"]))
    assert (big.status_code, big.json()["code"]) == (413, "payload_too_large")
    assert counts(db_conn)["onboarding_submissions"] == 0


def test_text_is_normalised(client, db_conn) -> None:
    inv = new_invitation(client)
    form = valid_form(project_name="  Spaced Name  ", business_goals="Line one\r\nLine two  ", target_audience="  ",
                      communication={"preferred_channel": "slack", "contact_email": "  PM@Example.COM "})
    assert submit(client, inv["token"], form).status_code == 201
    answers = db_conn.execute("SELECT answers FROM onboarding_submissions").fetchone()["answers"]
    assert answers["project_name"] == "Spaced Name"
    assert answers["business_goals"] == "Line one\nLine two"
    assert answers["target_audience"] is None
    assert answers["communication"]["contact_email"] == "pm@example.com"


# ------------------------------------------------------- duplicates/expiry
def test_repeated_submission_is_409_and_creates_nothing(client, db_conn) -> None:
    inv = new_invitation(client)
    first = submit(client, inv["token"])
    after_first = counts(db_conn)
    for _ in range(2):
        response = submit(client, inv["token"], valid_form(project_name="Different name"))
        assert (response.status_code, response.json()["code"]) == (409, "already_submitted")
    assert counts(db_conn) == after_first
    assert client.get("/api/portal/project", headers=auth(inv["token"])).json()["id"] == first.json()["project"]["id"]
    form = client.get("/api/portal/onboarding", headers=auth(inv["token"])).json()
    assert form["status"] == "submitted" and form["prefill"] == {}


def test_expired_invitation_cannot_be_submitted(client, db_conn) -> None:
    inv = new_invitation(client)
    db_conn.execute("UPDATE onboarding_invitations SET expires_at = now() - interval '1 minute'")
    response = submit(client, inv["token"])
    assert (response.status_code, response.json()["code"]) == (409, "invitation_expired")
    assert counts(db_conn)["projects"] == 0


def test_token_revoked_after_authentication_is_refused_inside_the_transaction(client, db_conn) -> None:
    inv = new_invitation(client)
    ctx = portal.authenticate(inv["token"])
    db_conn.execute("UPDATE client_access_tokens SET revoked_at = now()")
    form = form_for(ctx.service_type).model_validate(valid_form())
    with pytest.raises(PortalAuthError):
        onboarding_service.submit(ctx, form, max_attempts=5)
    assert counts(db_conn)["projects"] == 0


# --------------------------------------------------------------- rollback
FAILURE_STAGES = [
    (onboarding_store, "insert_submission", 1),
    (projects_store, "insert_project", 1),
    (projects_store, "insert_milestone", 1),
    (projects_store, "insert_milestone", 3),
    (projects_store, "insert_task", 1),
    (projects_store, "insert_task", 6),
    (jobs_store, "enqueue", 1),
    (jobs_store, "enqueue", 11),
    (activity_store, "record", 1),
    (activity_store, "record", 3),
    (onboarding_store, "mark_invitation_submitted", 1),
]


@pytest.mark.parametrize(("module", "name", "fail_on_call"), FAILURE_STAGES,
                         ids=[f"{m.__name__.rsplit('.', 1)[1]}.{n}#{c}" for m, n, c in FAILURE_STAGES])
def test_failure_at_any_stage_rolls_back_everything(client, db_conn, monkeypatch, module, name, fail_on_call) -> None:
    inv = new_invitation(client)
    before = counts(db_conn)
    original = getattr(module, name)
    calls = {"n": 0}

    def failing(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == fail_on_call:
            raise RuntimeError(f"injected failure in {name}")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, failing)
    response = submit(client, inv["token"])
    assert response.status_code == 500
    assert "injected" not in response.text
    assert calls["n"] == fail_on_call  # the failure really happened at that stage
    assert counts(db_conn) == before
    inv_row = db_conn.execute("SELECT status, submitted_at FROM onboarding_invitations").fetchone()
    assert (inv_row["status"], inv_row["submitted_at"]) == ("pending", None)

    monkeypatch.setattr(module, name, original)
    retry = submit(client, inv["token"])
    assert retry.status_code == 201
    after = counts(db_conn)
    assert (after["projects"], after["tasks"], after["integration_jobs"]) == (1, 10, 12)


# ------------------------------------------------------------- concurrency
def test_concurrent_submissions_create_exactly_one_project(client, db_conn) -> None:
    inv = new_invitation(client)
    with ThreadPoolExecutor(max_workers=10) as pool:
        responses = list(pool.map(lambda i: submit(client, inv["token"], valid_form(project_name=f"Attempt {i}")),
                                  range(10)))
    codes = sorted(r.status_code for r in responses)
    assert codes == [201] + [409] * 9, [r.text for r in responses if r.status_code not in (201, 409)]
    assert all(r.json()["code"] == "already_submitted" for r in responses if r.status_code == 409)
    c = counts(db_conn)
    assert (c["onboarding_submissions"], c["projects"], c["milestones"], c["tasks"], c["integration_jobs"]) == (1, 1, 4, 10, 12)
    winner = next(r for r in responses if r.status_code == 201).json()["project"]["name"]
    assert db_conn.execute("SELECT name FROM projects").fetchone()["name"] == winner


def test_concurrent_submissions_for_different_invitations_all_succeed(client, db_conn) -> None:
    invitations = [new_invitation(client, email=f"c{i}@example.com", company=f"Co {i}") for i in range(5)]
    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(pool.map(lambda inv: submit(client, inv["token"]), invitations))
    assert [r.status_code for r in responses] == [201] * 5
    c = counts(db_conn)
    assert (c["projects"], c["tasks"], c["integration_jobs"]) == (5, 50, 60)
