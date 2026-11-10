"""Administrator management API: dashboard, clients, access links, projects, tasks."""

import logging

import pytest

from app.security import hash_token
from tests.admin_helpers import XHR, admin_app, logged_in, patch, post
from tests.crm_helpers import encode, payload, signed_headers
from tests.portal_helpers import auth, new_invitation, submit


@pytest.fixture
def client(db_conn, migrated_database, pool_cleanup):
    with admin_app(migrated_database) as c:
        yield logged_in(c)


def _project(client, **kw):
    inv = new_invitation(client, **kw)
    project = submit(client, inv["token"]).json()["project"]
    return inv, project


# ----------------------------------------------------------------- dashboard
def test_dashboard_on_empty_database(client, db_conn) -> None:
    data = client.get("/api/admin/dashboard").json()
    assert set(data["counts"].values()) == {0}
    assert data["active_projects"] == [] and data["dead_jobs"] == []
    # login itself adds no activity
    assert data["recent_activity"] == []


def test_dashboard_reflects_real_records(client, db_conn) -> None:
    _project(client, email="a@example.com", company="A Co")
    _project(client, email="b@example.com", company="B Co")
    new_invitation(client, email="c@example.com", company="C Co")  # pending onboarding
    db_conn.execute("UPDATE clients SET status = 'archived' WHERE email = 'c@example.com'")
    db_conn.execute("UPDATE tasks SET status = 'done' WHERE position < 3")
    db_conn.execute("UPDATE projects SET status = 'completed' WHERE id = (SELECT min(id::text)::uuid FROM projects)")
    db_conn.execute("UPDATE integration_jobs SET status = 'dead', completed_at = now(), last_error_code = 'X' "
                    "WHERE job_type = 'slack.notify'")
    expected = db_conn.execute(
        """SELECT (SELECT count(*) FROM tasks WHERE status <> 'done') AS open_tasks,
                  (SELECT count(*) FROM tasks WHERE status = 'done') AS completed_tasks""").fetchone()
    counts = client.get("/api/admin/dashboard").json()["counts"]
    assert counts == {
        "total_clients": 3, "active_clients": 2, "archived_clients": 1, "pending_invitations": 1,
        "active_projects": 1, "completed_projects": 1, "open_tasks": expected["open_tasks"],
        "completed_tasks": expected["completed_tasks"], "pending_jobs": 22, "running_jobs": 0, "dead_jobs": 2,
    }
    assert expected == {"open_tasks": 14, "completed_tasks": 6}
    data = client.get("/api/admin/dashboard").json()
    assert len(data["dead_jobs"]) == 2 and len(data["active_projects"]) == 1
    assert data["active_projects"][0]["progress"]["total_tasks"] == 10
    assert len(data["recent_activity"]) == 10


# ------------------------------------------------------------------- clients
def test_client_list_search_pagination(client, db_conn) -> None:
    for i in range(7):
        assert post(client, "/api/admin/clients", {"name": f"Client {i}", "company": f"Co {i % 2}",
                                                   "email": f"c{i}@x.example"}).status_code == 201
    first = client.get("/api/admin/clients", params={"limit": 3}).json()
    assert (first["total"], len(first["items"]), first["limit"], first["offset"]) == (7, 3, 3, 0)
    assert [c["name"] for c in first["items"]] == ["Client 6", "Client 5", "Client 4"]  # newest first
    oldest = client.get("/api/admin/clients", params={"limit": 2, "sort": "created_asc"}).json()
    assert [c["name"] for c in oldest["items"]] == ["Client 0", "Client 1"]
    beyond = client.get("/api/admin/clients", params={"limit": 3, "offset": 30}).json()
    assert (beyond["items"], beyond["total"]) == ([], 7)
    assert client.get("/api/admin/clients", params={"q": "co 1"}).json()["total"] == 3
    assert client.get("/api/admin/clients", params={"q": "C4@X"}).json()["total"] == 1
    assert client.get("/api/admin/clients", params={"q": "%"}).json()["total"] == 0  # no wildcard injection
    assert all("token_hash" not in c for c in first["items"])


def test_create_update_and_conflicts(client, db_conn) -> None:
    created = post(client, "/api/admin/clients", {"name": " Jane ", "email": " Jane@Example.COM ", "phone": "+1 555"})
    assert created.status_code == 201
    c = created.json()
    assert (c["name"], c["email"], c["crm_source"]) == ("Jane", "jane@example.com", None)
    post(client, "/api/admin/clients", {"name": "Other", "email": "other@example.com"})
    dup = patch(client, f"/api/admin/clients/{c['id']}", {"email": "OTHER@example.com"})
    assert (dup.status_code, dup.json()["code"]) == (409, "email_taken")
    dup2 = post(client, "/api/admin/clients", {"name": "Again", "email": "jane@example.com"})
    assert (dup2.status_code, dup2.json()["code"]) == (409, "email_taken")
    updated = patch(client, f"/api/admin/clients/{c['id']}", {"company": "Jane Co", "phone": None})
    assert (updated.json()["company"], updated.json()["phone"]) == ("Jane Co", None)
    assert patch(client, f"/api/admin/clients/{c['id']}", {"crm_external_id": "x"}).status_code == 422
    assert patch(client, f"/api/admin/clients/{c['id']}", {"status": "archived"}).status_code == 422
    assert patch(client, f"/api/admin/clients/{c['id']}", {"name": None}).status_code == 409
    events = [r["event_type"] for r in db_conn.execute(
        "SELECT event_type FROM activity_events WHERE client_id = %s ORDER BY created_at", (c["id"],)).fetchall()]
    assert events == ["client.created", "client.updated"]


def test_no_op_update_records_nothing(client, db_conn) -> None:
    c = post(client, "/api/admin/clients", {"name": "Same", "company": "Co"}).json()
    patch(client, f"/api/admin/clients/{c['id']}", {"name": "Same", "company": "Co"})
    assert db_conn.execute("SELECT count(*) AS n FROM activity_events WHERE event_type = 'client.updated'").fetchone()["n"] == 0


def test_crm_identity_is_never_changed_by_admin_edits(client, db_conn) -> None:
    inv, _ = _project(client)
    before = db_conn.execute("SELECT crm_source, crm_external_id FROM clients").fetchone()
    patch(client, f"/api/admin/clients/{inv['client_id']}", {"name": "Renamed", "email": "new@example.com"})
    assert db_conn.execute("SELECT crm_source, crm_external_id FROM clients").fetchone() == before


def test_client_detail(client, db_conn) -> None:
    inv, project = _project(client)
    detail = client.get(f"/api/admin/clients/{inv['client_id']}").json()
    assert detail["name"] == "Demo Client" and detail["crm_source"] == "generic"
    (invitation,) = detail["invitations"]
    assert invitation["status"] == "submitted" and invitation["project_id"] == project["id"]
    assert invitation["access_link_expires_at"] is not None
    assert detail["projects"][0]["progress"]["total_tasks"] == 10
    assert {a["event_type"] for a in detail["activity"]} >= {"crm.deal_received", "onboarding.submitted"}
    assert "token_hash" not in str(detail)
    assert client.get("/api/admin/clients/00000000-0000-0000-0000-000000000000").status_code == 404


def test_archive_and_restore_keep_history(client, db_conn) -> None:
    inv, project = _project(client)
    before = db_conn.execute("SELECT (SELECT count(*) FROM projects) p, (SELECT count(*) FROM tasks) t, "
                             "(SELECT count(*) FROM onboarding_submissions) s").fetchone()
    assert post(client, f"/api/admin/clients/{inv['client_id']}/archive").json()["status"] == "archived"
    assert post(client, f"/api/admin/clients/{inv['client_id']}/archive").json()["status"] == "archived"  # idempotent
    assert db_conn.execute("SELECT (SELECT count(*) FROM projects) p, (SELECT count(*) FROM tasks) t, "
                           "(SELECT count(*) FROM onboarding_submissions) s").fetchone() == before
    assert post(client, f"/api/admin/clients/{inv['client_id']}/restore").json()["status"] == "active"
    types = [r["event_type"] for r in db_conn.execute(
        "SELECT event_type FROM activity_events WHERE event_type LIKE 'client.%' ORDER BY created_at").fetchall()]
    assert types == ["client.created", "client.archived", "client.restored"]


def test_crm_deal_for_archived_client_is_rejected_until_restored(client, db_conn) -> None:
    inv = new_invitation(client, email="arch@example.com", contact_id="arch-1")
    post(client, f"/api/admin/clients/{inv['client_id']}/archive")
    data = payload(event_id="evt_arch", deal={"external_id": "deal_arch"},
                   contact={"email": "arch@example.com", "external_id": "arch-1"})
    body = encode(data)
    response = client.post("/api/webhooks/crm/deal-won", content=body, headers=signed_headers(body))
    assert (response.status_code, response.json()["code"]) == (409, "client_archived")
    assert db_conn.execute("SELECT status FROM clients").fetchone()["status"] == "archived"  # not reactivated
    post(client, f"/api/admin/clients/{inv['client_id']}/restore")
    retry = client.post("/api/webhooks/crm/deal-won", content=body, headers=signed_headers(body))
    assert retry.status_code == 201


# ------------------------------------------------------- access-link regen
def test_regenerate_pending_onboarding_link(client, db_conn, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    inv = new_invitation(client)
    response = post(client, f"/api/admin/invitations/{inv['invitation_id']}/regenerate-access-link")
    assert response.status_code == 201
    body = response.json()
    assert body["kind"] == "onboarding" and body["access_url"].startswith("https://hub.example.test/portal#t=")
    new_token = body["access_url"].split("#t=")[1]
    # Old link stops working immediately; new one works for the same invitation.
    assert client.get("/api/portal/me", headers=auth(inv["token"])).status_code == 401
    me = client.get("/api/portal/me", headers=auth(new_token))
    assert me.status_code == 200 and me.json()["invitation"]["status"] == "pending"
    tokens = db_conn.execute("SELECT * FROM client_access_tokens ORDER BY created_at").fetchall()
    assert [t["revoked_at"] is not None for t in tokens] == [True, False]
    assert tokens[1]["token_hash"] == hash_token(new_token)
    assert str(tokens[1]["invitation_id"]) == inv["invitation_id"] and str(tokens[1]["client_id"]) == inv["client_id"]
    inv_exp = db_conn.execute("SELECT expires_at FROM onboarding_invitations").fetchone()["expires_at"]
    assert tokens[1]["expires_at"] == inv_exp  # never beyond the onboarding deadline
    dump = " ".join(str(r) for t in ("client_access_tokens", "activity_events", "webhook_events")
                    for r in db_conn.execute(f"SELECT t::text FROM {t} t").fetchall())
    assert new_token not in dump and new_token not in caplog.text
    act = db_conn.execute("SELECT data FROM activity_events WHERE event_type = 'invitation.access_link_regenerated'"
                          ).fetchone()["data"]
    assert act["kind"] == "onboarding" and act["revoked_tokens"] == 1


def test_regenerate_submitted_gives_180_day_project_access(client, db_conn) -> None:
    inv, project = _project(client)
    db_conn.execute("UPDATE onboarding_invitations SET expires_at = now() - interval '1 day'")  # onboarding deadline passed
    body = post(client, f"/api/admin/invitations/{inv['invitation_id']}/regenerate-access-link").json()
    assert body["kind"] == "project_access"
    token = body["access_url"].split("#t=")[1]
    days = db_conn.execute("SELECT EXTRACT(EPOCH FROM expires_at - now()) / 86400 AS d FROM client_access_tokens "
                           "WHERE revoked_at IS NULL").fetchone()["d"]
    assert 179.9 < days <= 180
    view = client.get("/api/portal/project", headers=auth(token))
    assert view.status_code == 200 and view.json()["id"] == project["id"]
    assert db_conn.execute("SELECT status FROM onboarding_invitations").fetchone()["status"] == "submitted"
    assert db_conn.execute("SELECT count(*) AS n FROM onboarding_submissions").fetchone()["n"] == 1


def test_regenerated_project_link_is_scoped_to_its_invitation(client, db_conn) -> None:
    first, project_a = _project(client, email="same@example.com", contact_id="same")
    second = new_invitation(client, email="same@example.com", contact_id="same", service_type="seo_retainer")
    token = post(client, f"/api/admin/invitations/{second['invitation_id']}/regenerate-access-link"
                 ).json()["access_url"].split("#t=")[1]
    assert client.get("/api/portal/project", headers=auth(token)).status_code == 404
    assert client.get(f"/api/portal/projects/{project_a['id']}", headers=auth(token)).status_code == 404


def test_expired_pending_invitation_is_not_reopened(client, db_conn) -> None:
    inv = new_invitation(client)
    db_conn.execute("UPDATE onboarding_invitations SET expires_at = now() - interval '1 minute'")
    response = post(client, f"/api/admin/invitations/{inv['invitation_id']}/regenerate-access-link")
    assert (response.status_code, response.json()["code"]) == (409, "invitation_expired")
    assert db_conn.execute("SELECT count(*) AS n FROM client_access_tokens WHERE revoked_at IS NULL").fetchone()["n"] == 1


def test_repeated_regeneration_keeps_exactly_one_active_link(client, db_conn) -> None:
    inv = new_invitation(client)
    tokens = [post(client, f"/api/admin/invitations/{inv['invitation_id']}/regenerate-access-link"
                   ).json()["access_url"].split("#t=")[1] for _ in range(3)]
    assert len(set(tokens)) == 3
    assert [client.get("/api/portal/me", headers=auth(t)).status_code for t in tokens] == [401, 401, 200]
    assert db_conn.execute("SELECT count(*) AS n FROM client_access_tokens WHERE revoked_at IS NULL").fetchone()["n"] == 1


def test_regenerate_unknown_invitation_is_404(client, db_conn) -> None:
    assert post(client, "/api/admin/invitations/00000000-0000-0000-0000-000000000000/regenerate-access-link"
                ).status_code == 404


# ------------------------------------------------------------------ projects
def test_project_list_and_detail(client, db_conn) -> None:
    inv, project = _project(client)
    listing = client.get("/api/admin/projects").json()
    assert listing["total"] == 1 and listing["items"][0]["client_name"] == "Demo Client"
    assert listing["items"][0]["template_name"] == "Website Build"
    detail = client.get(f"/api/admin/projects/{project['id']}").json()
    assert detail["client"]["id"] == inv["client_id"] and detail["template"]["key"] == "website_build"
    assert len(detail["milestones"]) == 4 and len(detail["tasks"]) == 10  # internal tasks included for admins
    assert any(not t["client_visible"] for t in detail["tasks"])
    assert all(t["clickup_job_status"] == "pending" for t in detail["tasks"])
    assert detail["progress"] == {"total_tasks": 10, "completed_tasks": 0, "open_tasks": 10, "percent": 0}
    assert client.get("/api/admin/projects", params={"q": "nomatch"}).json()["total"] == 0


def test_task_status_update_is_atomic_with_sync_job_and_activity(client, db_conn) -> None:
    _, project = _project(client)
    task = client.get(f"/api/admin/projects/{project['id']}").json()["tasks"][0]
    # The initial Sheets job (version 1) is still pending: the new version coalesces into it.
    response = patch(client, f"/api/admin/projects/{project['id']}/tasks/{task['id']}", {"status": "done"})
    assert response.status_code == 200 and response.json()["changed"] is True
    p = response.json()["project"]
    assert p["progress"]["completed_tasks"] == 1 and p["sync_version"] == 2
    row = db_conn.execute("SELECT status, completed_at FROM tasks WHERE id = %s", (task["id"],)).fetchone()
    assert row["status"] == "done" and row["completed_at"] is not None
    assert db_conn.execute("SELECT count(*) AS n FROM integration_jobs WHERE job_type = 'sheets.upsert_project'"
                           ).fetchone()["n"] == 1
    # Once version 1 has run, the next change queues a new version.
    db_conn.execute("UPDATE integration_jobs SET status = 'succeeded', completed_at = now() "
                    "WHERE job_type = 'sheets.upsert_project'")
    patch(client, f"/api/admin/projects/{project['id']}/tasks/{task['id']}", {"status": "in_progress"})
    keys = [r["idempotency_key"].rsplit(":", 1)[1] for r in db_conn.execute(
        "SELECT idempotency_key FROM integration_jobs WHERE job_type = 'sheets.upsert_project' ORDER BY created_at"
    ).fetchall()]
    assert keys == ["1", "3"]
    assert db_conn.execute("SELECT completed_at FROM tasks WHERE id = %s", (task["id"],)).fetchone()["completed_at"] is None
    changes = db_conn.execute("SELECT data FROM activity_events WHERE event_type = 'task.status_changed' "
                              "ORDER BY created_at").fetchall()
    assert [(c["data"]["from"], c["data"]["to"]) for c in changes] == [("todo", "done"), ("done", "in_progress")]
    assert not db_conn.execute("SELECT count(*) AS n FROM integration_jobs WHERE job_type LIKE 'clickup.update%%'"
                               ).fetchone()["n"]


def test_same_status_and_invalid_status(client, db_conn) -> None:
    _, project = _project(client)
    task = client.get(f"/api/admin/projects/{project['id']}").json()["tasks"][0]
    same = patch(client, f"/api/admin/projects/{project['id']}/tasks/{task['id']}", {"status": "todo"})
    assert same.json()["changed"] is False
    assert db_conn.execute("SELECT count(*) AS n FROM activity_events WHERE event_type LIKE 'task.%%'").fetchone()["n"] == 0
    bad = patch(client, f"/api/admin/projects/{project['id']}/tasks/{task['id']}", {"status": "blocked"})
    assert bad.status_code == 422
    other = patch(client, f"/api/admin/projects/00000000-0000-0000-0000-000000000000/tasks/{task['id']}",
                  {"status": "done"})
    assert other.status_code == 404  # task must belong to that project


def test_assignment_changes(client, db_conn) -> None:
    _, project = _project(client)
    task = client.get(f"/api/admin/projects/{project['id']}").json()["tasks"][0]
    response = patch(client, f"/api/admin/projects/{project['id']}/tasks/{task['id']}",
                     {"assignee_name": "Sam Lee", "assignee_email": "SAM@agency.example"})
    t = next(x for x in response.json()["project"]["tasks"] if x["id"] == task["id"])
    assert (t["assignee_name"], t["assignee_email"]) == ("Sam Lee", "sam@agency.example")
    assert db_conn.execute("SELECT count(*) AS n FROM activity_events WHERE event_type = 'task.assigned'").fetchone()["n"] == 1
    assert patch(client, f"/api/admin/projects/{project['id']}/tasks/{task['id']}",
                 {"assignee_email": "not-an-email"}).status_code == 422


def test_project_status_update_and_portal_visibility(client, db_conn) -> None:
    inv, project = _project(client)
    response = patch(client, f"/api/admin/projects/{project['id']}", {"status": "on_hold"})
    assert response.json()["project"]["status"] == "on_hold"
    portal = client.get("/api/portal/project", headers=auth(inv["token"])).json()
    assert portal["status"] == "on_hold"
    assert "project.status_changed" in [a["type"] for a in portal["activity"]]
    assert patch(client, f"/api/admin/projects/{project['id']}", {"status": "archived"}).status_code == 422


def test_activity_endpoint_filters(client, db_conn) -> None:
    inv, project = _project(client)
    all_ = client.get("/api/admin/activity").json()
    by_project = client.get("/api/admin/activity", params={"project_id": project["id"]}).json()
    crm = client.get("/api/admin/activity", params={"event_prefix": "crm."}).json()
    assert all_["total"] > by_project["total"] > 0
    assert {a["event_type"] for a in crm["items"]} == {"crm.deal_received"}
    assert client.get("/api/admin/activity", params={"event_prefix": "x' OR 1=1"}).status_code == 422
