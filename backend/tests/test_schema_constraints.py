"""The database itself enforces the duplicate-prevention and isolation rules."""

import uuid
from contextlib import contextmanager

import psycopg
import pytest
from psycopg import errors

from app.security import generate_token, hash_token


# ------------------------------------------------------------------ helpers
def client(conn, email: str | None = None, crm: tuple[str, str] | None = None, name: str = "Acme") -> uuid.UUID:
    source, external = crm if crm else (None, None)
    return conn.execute(
        "INSERT INTO clients (name, email, crm_source, crm_external_id) VALUES (%s, %s, %s, %s) RETURNING id",
        (name, email, source, external),
    ).fetchone()["id"]


def invitation(conn, client_id, deal: tuple[str, str] | None = None) -> uuid.UUID:
    source, deal_id = deal if deal else (None, None)
    return conn.execute(
        "INSERT INTO onboarding_invitations (client_id, crm_source, crm_deal_id, expires_at) "
        "VALUES (%s, %s, %s, now() + interval '30 days') RETURNING id",
        (client_id, source, deal_id),
    ).fetchone()["id"]


def project(conn, client_id, invitation_id=None) -> uuid.UUID:
    return conn.execute(
        "INSERT INTO projects (client_id, invitation_id, name) VALUES (%s, %s, 'Website') RETURNING id",
        (client_id, invitation_id),
    ).fetchone()["id"]


def task(conn, project_id, key: str | None = None, clickup_id: str | None = None) -> uuid.UUID:
    return conn.execute(
        "INSERT INTO tasks (project_id, template_task_key, title, clickup_task_id) "
        "VALUES (%s, %s, 'Do it', %s) RETURNING id",
        (project_id, key, clickup_id),
    ).fetchone()["id"]


def job(conn, key: str, coalesce_key: str | None = None, status: str = "pending") -> uuid.UUID:
    completed = "now()" if status in ("succeeded", "dead", "cancelled") else "NULL"
    lease = ("'w1'", "now() + interval '1 minute'") if status == "running" else ("NULL", "NULL")
    return conn.execute(
        f"INSERT INTO integration_jobs (job_type, idempotency_key, coalesce_key, subject_type, subject_id, "
        f"status, completed_at, locked_by, locked_until) "
        f"VALUES ('sheets.upsert_project', %s, %s, 'project', gen_random_uuid(), %s, {completed}, {lease[0]}, {lease[1]}) "
        f"RETURNING id",
        (key, coalesce_key, status),
    ).fetchone()["id"]


@contextmanager
def violates(constraint: str, exc_type: type[psycopg.Error] = errors.UniqueViolation):
    """Expect a violation of exactly this named constraint."""
    with pytest.raises(exc_type) as info:
        yield info
    assert info.value.diag.constraint_name == constraint


# ------------------------------------------------------------------ clients
def test_client_email_is_unique_case_insensitively(db_conn) -> None:
    client(db_conn, email="Owner@Acme.com")
    with violates("clients_email_lower_key"):
        client(db_conn, email="owner@acme.COM")


def test_clients_without_email_are_allowed(db_conn) -> None:
    client(db_conn, email=None)
    client(db_conn, email=None)
    assert db_conn.execute("SELECT count(*) AS n FROM clients WHERE email IS NULL").fetchone()["n"] == 2


def test_crm_identity_is_unique(db_conn) -> None:
    client(db_conn, email="a@x.com", crm=("hubspot", "c-1"))
    with violates("clients_crm_identity_key"):
        client(db_conn, email="b@x.com", crm=("hubspot", "c-1"))
    client(db_conn, email="c@x.com", crm=("gohighlevel", "c-1"))  # other source is fine


def test_crm_identity_must_be_complete_and_normalized(db_conn) -> None:
    with pytest.raises(errors.CheckViolation):
        client(db_conn, crm=("hubspot", None))  # type: ignore[arg-type]
    with pytest.raises(errors.CheckViolation):
        client(db_conn, crm=("HubSpot", "c-1"))  # mixed case would dodge the unique key


# -------------------------------------------------------- invitations/tokens
def test_one_invitation_per_crm_deal(db_conn) -> None:
    c1 = client(db_conn, email="a@x.com")
    c2 = client(db_conn, email="b@x.com")
    invitation(db_conn, c1, deal=("generic", "deal-1"))
    with violates("onboarding_invitations_crm_deal_key"):
        invitation(db_conn, c2, deal=("generic", "deal-1"))
    invitation(db_conn, c1)  # manual invitations (no deal) are unlimited
    invitation(db_conn, c1)


def test_access_token_hash_is_unique_and_hex(db_conn) -> None:
    c = client(db_conn, email="a@x.com")
    digest = hash_token(generate_token())
    insert = ("INSERT INTO client_access_tokens (client_id, token_hash, expires_at) "
              "VALUES (%s, %s, now() + interval '1 day')")
    db_conn.execute(insert, (c, digest))
    with violates("client_access_tokens_token_hash_key"):
        db_conn.execute(insert, (c, digest))
    with pytest.raises(errors.CheckViolation):
        db_conn.execute(insert, (c, "raw-token-not-a-hash"))


def test_token_cannot_reference_another_clients_invitation(db_conn) -> None:
    alice = client(db_conn, email="alice@x.com")
    bob = client(db_conn, email="bob@x.com")
    bobs_invitation = invitation(db_conn, bob)
    with violates("client_access_tokens_invitation_same_client", errors.ForeignKeyViolation):
        db_conn.execute(
            "INSERT INTO client_access_tokens (client_id, invitation_id, token_hash, expires_at) "
            "VALUES (%s, %s, %s, now() + interval '1 day')",
            (alice, bobs_invitation, hash_token(generate_token())),
        )


# ------------------------------------------------------ submissions/projects
def test_one_submission_per_invitation(db_conn) -> None:
    c = client(db_conn, email="a@x.com")
    inv = invitation(db_conn, c)
    insert = "INSERT INTO onboarding_submissions (invitation_id, client_id, answers) VALUES (%s, %s, '{}')"
    db_conn.execute(insert, (inv, c))
    with violates("onboarding_submissions_invitation_key"):
        db_conn.execute(insert, (inv, c))


def test_submission_cannot_cross_clients(db_conn) -> None:
    alice = client(db_conn, email="alice@x.com")
    bob_inv = invitation(db_conn, client(db_conn, email="bob@x.com"))
    with violates("onboarding_submissions_invitation_same_client", errors.ForeignKeyViolation):
        db_conn.execute(
            "INSERT INTO onboarding_submissions (invitation_id, client_id, answers) VALUES (%s, %s, '{}')",
            (bob_inv, alice),
        )


def test_one_project_per_invitation(db_conn) -> None:
    c = client(db_conn, email="a@x.com")
    inv = invitation(db_conn, c)
    project(db_conn, c, inv)
    with violates("projects_invitation_key"):
        project(db_conn, c, inv)
    project(db_conn, c)  # manual projects without invitation are allowed
    project(db_conn, c)


def test_project_cannot_use_another_clients_invitation(db_conn) -> None:
    alice = client(db_conn, email="alice@x.com")
    bob_inv = invitation(db_conn, client(db_conn, email="bob@x.com"))
    with violates("projects_invitation_same_client", errors.ForeignKeyViolation):
        project(db_conn, alice, bob_inv)


def test_submitted_invitation_requires_timestamp(db_conn) -> None:
    inv = invitation(db_conn, client(db_conn, email="a@x.com"))
    with violates("onboarding_invitations_submitted_consistent", errors.CheckViolation):
        db_conn.execute("UPDATE onboarding_invitations SET status = 'submitted' WHERE id = %s", (inv,))
    db_conn.execute(
        "UPDATE onboarding_invitations SET status = 'submitted', submitted_at = now() WHERE id = %s", (inv,)
    )


# ------------------------------------------------------------------- tasks
def test_template_task_key_is_unique_per_project(db_conn) -> None:
    c = client(db_conn, email="a@x.com")
    p1, p2 = project(db_conn, c), project(db_conn, c)
    task(db_conn, p1, key="kickoff_call")
    with violates("tasks_project_template_task_key"):
        task(db_conn, p1, key="kickoff_call")
    task(db_conn, p2, key="kickoff_call")  # same key in another project is fine
    task(db_conn, p1, key=None)  # manual tasks have no key
    task(db_conn, p1, key=None)


def test_clickup_task_id_is_unique_when_present(db_conn) -> None:
    p = project(db_conn, client(db_conn, email="a@x.com"))
    task(db_conn, p, clickup_id="86abc")
    with violates("tasks_clickup_task_id_key"):
        task(db_conn, p, clickup_id="86abc")
    task(db_conn, p)
    task(db_conn, p)


def test_task_milestone_must_belong_to_same_project(db_conn) -> None:
    c = client(db_conn, email="a@x.com")
    p1, p2 = project(db_conn, c), project(db_conn, c)
    m = db_conn.execute(
        "INSERT INTO milestones (project_id, name, position) VALUES (%s, 'Design', 0) RETURNING id", (p1,)
    ).fetchone()["id"]
    with violates("tasks_milestone_same_project", errors.ForeignKeyViolation):
        db_conn.execute(
            "INSERT INTO tasks (project_id, milestone_id, title) VALUES (%s, %s, 'x')", (p2, m)
        )
    t = db_conn.execute(
        "INSERT INTO tasks (project_id, milestone_id, title) VALUES (%s, %s, 'x') RETURNING id", (p1, m)
    ).fetchone()["id"]
    # Deleting the milestone un-assigns the task but keeps its project.
    db_conn.execute("DELETE FROM milestones WHERE id = %s", (m,))
    row = db_conn.execute("SELECT project_id, milestone_id FROM tasks WHERE id = %s", (t,)).fetchone()
    assert row == {"project_id": p1, "milestone_id": None}


# ---------------------------------------------------------- webhook events
def test_webhook_event_is_unique_per_source(db_conn) -> None:
    insert = ("INSERT INTO webhook_events (source, external_event_id, event_type, payload) "
              "VALUES (%s, %s, 'deal.won', '{}')")
    db_conn.execute(insert, ("generic", "evt-1"))
    with violates("webhook_events_source_event_key"):
        db_conn.execute(insert, ("generic", "evt-1"))
    db_conn.execute(insert, ("hubspot", "evt-1"))


# -------------------------------------------------------- integration jobs
def test_job_idempotency_key_is_unique(db_conn) -> None:
    job(db_conn, "clickup.create_task:t1")
    with violates("integration_jobs_idempotency_key_key"):
        job(db_conn, "clickup.create_task:t1")


def test_only_one_pending_job_per_coalesce_key(db_conn) -> None:
    pid = uuid.uuid4()
    coalesce = f"sheets.upsert_project:{pid}"
    job(db_conn, f"sheets.upsert_project:{pid}:1", coalesce)
    with violates("integration_jobs_pending_coalesce_key"):
        job(db_conn, f"sheets.upsert_project:{pid}:2", coalesce)


def test_later_sync_versions_are_not_blocked_by_finished_or_running_jobs(db_conn) -> None:
    pid = uuid.uuid4()
    coalesce = f"sheets.upsert_project:{pid}"
    job(db_conn, f"sheets.upsert_project:{pid}:1", coalesce, status="succeeded")
    job(db_conn, f"sheets.upsert_project:{pid}:2", coalesce, status="dead")
    job(db_conn, f"sheets.upsert_project:{pid}:3", coalesce, status="running")
    job(db_conn, f"sheets.upsert_project:{pid}:4", coalesce, status="pending")
    n = db_conn.execute(
        "SELECT count(*) AS n FROM integration_jobs WHERE coalesce_key = %s", (coalesce,)
    ).fetchone()["n"]
    assert n == 4


def test_coalescing_insert_pattern_does_nothing_while_one_is_pending(db_conn) -> None:
    pid = uuid.uuid4()
    coalesce = f"sheets.upsert_project:{pid}"
    job(db_conn, f"sheets.upsert_project:{pid}:1", coalesce)
    inserted = db_conn.execute(
        "INSERT INTO integration_jobs (job_type, idempotency_key, coalesce_key, subject_type, subject_id) "
        "VALUES ('sheets.upsert_project', %s, %s, 'project', %s) ON CONFLICT DO NOTHING RETURNING id",
        (f"sheets.upsert_project:{pid}:2", coalesce, pid),
    ).fetchone()
    assert inserted is None


def test_running_job_must_carry_a_lease(db_conn) -> None:
    jid = job(db_conn, "clickup.create_task:t9")
    with violates("integration_jobs_lease_consistent", errors.CheckViolation):
        db_conn.execute("UPDATE integration_jobs SET status = 'running' WHERE id = %s", (jid,))


def test_attempt_number_is_unique_per_job(db_conn) -> None:
    jid = job(db_conn, "slack.notify:project_created:p1")
    insert = "INSERT INTO integration_attempts (job_id, attempt_number) VALUES (%s, %s)"
    db_conn.execute(insert, (jid, 1))
    with violates("integration_attempts_job_attempt_key"):
        db_conn.execute(insert, (jid, 1))
    db_conn.execute(insert, (jid, 2))


# ------------------------------------------------------------- triggers
def test_updated_at_changes_on_update(db_conn) -> None:
    c = client(db_conn, email="a@x.com")
    before = db_conn.execute("SELECT created_at, updated_at FROM clients WHERE id = %s", (c,)).fetchone()
    # autocommit: each statement is its own transaction, so now() advances.
    db_conn.execute("SELECT pg_sleep(0.01)")
    db_conn.execute("UPDATE clients SET name = 'Acme Ltd' WHERE id = %s", (c,))
    after = db_conn.execute("SELECT created_at, updated_at FROM clients WHERE id = %s", (c,)).fetchone()
    assert after["created_at"] == before["created_at"]
    assert after["updated_at"] > before["updated_at"]


def test_every_mutable_table_has_an_updated_at_trigger(db_conn) -> None:
    rows = db_conn.execute(
        """
        SELECT c.table_name,
               EXISTS (SELECT 1 FROM pg_trigger t JOIN pg_class r ON r.oid = t.tgrelid
                       WHERE r.relname = c.table_name AND t.tgname = c.table_name || '_set_updated_at') AS has_trigger
        FROM information_schema.columns c
        WHERE c.table_schema = 'public' AND c.column_name = 'updated_at'
        """
    ).fetchall()
    assert len(rows) == 12
    assert all(r["has_trigger"] for r in rows), rows


def test_primary_keys_are_uuid_and_timestamps_are_timestamptz(db_conn) -> None:
    bad = db_conn.execute(
        """
        SELECT table_name, column_name, data_type FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name <> 'schema_migrations'
          AND ((column_name = 'id' AND data_type <> 'uuid')
            OR (column_name LIKE '%\\_at' AND data_type <> 'timestamp with time zone'))
        """
    ).fetchall()
    assert bad == []
