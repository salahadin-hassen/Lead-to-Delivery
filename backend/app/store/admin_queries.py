"""Read-only SQL for the administrator console. Never selects token hashes,
raw webhook payload contact data or job payloads beyond safe fields."""

from typing import Any
from uuid import UUID

from app.db import Connection

INVITATION_STATUS_SQL = """CASE WHEN i.status = 'pending' AND i.expires_at <= now() THEN 'expired' ELSE i.status END"""

PROGRESS_SQL = """
    COALESCE((SELECT count(*) FROM tasks t WHERE t.project_id = p.id), 0) AS total_tasks,
    COALESCE((SELECT count(*) FROM tasks t WHERE t.project_id = p.id AND t.status = 'done'), 0) AS completed_tasks
"""


def _paged(conn: Connection, sql: str, params: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """Run a query that selects ``count(*) OVER () AS _total`` and ends with
    LIMIT %(limit)s OFFSET %(offset)s; return (rows, total). A page past the end
    is re-queried at offset 0 so the total is still correct."""
    rows = conn.execute(sql, params).fetchall()
    if rows:
        total = rows[0]["_total"]
    elif params.get("offset"):
        probe = conn.execute(sql, {**params, "offset": 0, "limit": 1}).fetchone()
        total = probe["_total"] if probe else 0
    else:
        total = 0
    return [{k: v for k, v in r.items() if k != "_total"} for r in rows], total


# ----------------------------------------------------------------- dashboard
def dashboard(conn: Connection) -> dict[str, Any]:
    counts = conn.execute(
        """
        SELECT
          (SELECT count(*) FROM clients) AS total_clients,
          (SELECT count(*) FROM clients WHERE status = 'active') AS active_clients,
          (SELECT count(*) FROM clients WHERE status = 'archived') AS archived_clients,
          (SELECT count(*) FROM onboarding_invitations WHERE status = 'pending' AND expires_at > now())
              AS pending_invitations,
          (SELECT count(*) FROM projects WHERE status = 'active') AS active_projects,
          (SELECT count(*) FROM projects WHERE status = 'completed') AS completed_projects,
          (SELECT count(*) FROM tasks WHERE status <> 'done') AS open_tasks,
          (SELECT count(*) FROM tasks WHERE status = 'done') AS completed_tasks,
          (SELECT count(*) FROM integration_jobs WHERE status = 'pending') AS pending_jobs,
          (SELECT count(*) FROM integration_jobs WHERE status = 'running') AS running_jobs,
          (SELECT count(*) FROM integration_jobs WHERE status = 'dead') AS dead_jobs
        """
    ).fetchone()
    active = conn.execute(
        f"""
        SELECT p.id, p.name, p.status, p.due_date, p.created_at, c.id AS client_id, c.name AS client_name,
               {PROGRESS_SQL}
        FROM projects p JOIN clients c ON c.id = p.client_id
        WHERE p.status = 'active' ORDER BY p.created_at DESC, p.id LIMIT 5
        """
    ).fetchall()
    dead = conn.execute(
        """
        SELECT id, job_type, subject_type, subject_id, attempts, last_error_code, updated_at
        FROM integration_jobs WHERE status = 'dead' ORDER BY updated_at DESC, id LIMIT 5
        """
    ).fetchall()
    return {"counts": counts, "active_projects": active, "dead_jobs": dead,
            "recent_activity": list_activity(conn, limit=10, offset=0)[0]}


# ------------------------------------------------------------------- clients
def list_clients(conn: Connection, *, q: str | None, status: str | None, sort: str, limit: int, offset: int):
    order = "ASC" if sort == "created_asc" else "DESC"
    return _paged(
        conn,
        f"""
        SELECT c.id, c.name, c.company, c.email, c.phone, c.status, c.crm_source, c.created_at,
               (SELECT count(*) FROM projects p WHERE p.client_id = c.id) AS project_count,
               (SELECT count(*) FROM onboarding_invitations i WHERE i.client_id = c.id
                  AND i.status = 'pending' AND i.expires_at > now()) AS pending_invitations,
               count(*) OVER () AS _total
        FROM clients c
        WHERE (%(status)s::text IS NULL OR c.status = %(status)s)
          AND (%(q)s::text IS NULL
               OR position(lower(%(q)s) IN lower(c.name)) > 0
               OR position(lower(%(q)s) IN lower(coalesce(c.company, ''))) > 0
               OR position(lower(%(q)s) IN lower(coalesce(c.email, ''))) > 0)
        ORDER BY c.created_at {order}, c.id {order}
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        {"q": q, "status": status, "limit": limit, "offset": offset},
    )


def get_client(conn: Connection, client_id: UUID) -> dict[str, Any] | None:
    client = conn.execute(
        "SELECT id, name, company, email, phone, status, crm_source, crm_external_id, created_at, updated_at "
        "FROM clients WHERE id = %s", (client_id,)).fetchone()
    if client is None:
        return None
    client["invitations"] = conn.execute(
        f"""
        SELECT i.id, {INVITATION_STATUS_SQL} AS status, i.service_type, i.deal_name, i.crm_source, i.crm_deal_id,
               i.deal_value_cents, i.currency, i.expires_at, i.submitted_at, i.created_at,
               (SELECT max(t.expires_at) FROM client_access_tokens t WHERE t.invitation_id = i.id
                  AND t.revoked_at IS NULL AND t.expires_at > now()) AS access_link_expires_at,
               (SELECT p.id FROM projects p WHERE p.invitation_id = i.id) AS project_id
        FROM onboarding_invitations i WHERE i.client_id = %s ORDER BY i.created_at DESC, i.id
        """, (client_id,)).fetchall()
    client["projects"] = conn.execute(
        f"""
        SELECT p.id, p.name, p.status, p.start_date, p.due_date, p.created_at, {PROGRESS_SQL}
        FROM projects p WHERE p.client_id = %s ORDER BY p.created_at DESC, p.id
        """, (client_id,)).fetchall()
    client["activity"] = list_activity(conn, client_id=client_id, limit=50, offset=0)[0]
    return client


# ------------------------------------------------------------------ projects
def list_projects(conn: Connection, *, q: str | None, status: str | None, client_id: UUID | None,
                  limit: int, offset: int):
    return _paged(
        conn,
        f"""
        SELECT p.id, p.name, p.status, p.start_date, p.due_date, p.created_at,
               c.id AS client_id, c.name AS client_name, c.company AS client_company,
               pt.name AS template_name, {PROGRESS_SQL}, count(*) OVER () AS _total
        FROM projects p JOIN clients c ON c.id = p.client_id
        LEFT JOIN project_templates pt ON pt.id = p.template_id
        WHERE (%(status)s::text IS NULL OR p.status = %(status)s)
          AND (%(client)s::uuid IS NULL OR p.client_id = %(client)s)
          AND (%(q)s::text IS NULL OR position(lower(%(q)s) IN lower(p.name)) > 0
               OR position(lower(%(q)s) IN lower(c.name)) > 0
               OR position(lower(%(q)s) IN lower(coalesce(c.company, ''))) > 0)
        ORDER BY p.created_at DESC, p.id
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        {"q": q, "status": status, "client": client_id, "limit": limit, "offset": offset},
    )


def get_project(conn: Connection, project_id: UUID) -> dict[str, Any] | None:
    project = conn.execute(
        f"""
        SELECT p.id, p.name, p.status, p.start_date, p.due_date, p.sync_version, p.created_at, p.updated_at,
               p.invitation_id, s.answers ->> 'project_description' AS description,
               json_build_object('id', c.id, 'name', c.name, 'company', c.company, 'email', c.email,
                                 'status', c.status) AS client,
               CASE WHEN pt.id IS NULL THEN NULL ELSE
                 json_build_object('key', pt.key, 'name', pt.name, 'version', p.template_version) END AS template,
               {PROGRESS_SQL}
        FROM projects p JOIN clients c ON c.id = p.client_id
        LEFT JOIN project_templates pt ON pt.id = p.template_id
        LEFT JOIN onboarding_submissions s ON s.invitation_id = p.invitation_id
        WHERE p.id = %s
        """, (project_id,)).fetchone()
    if project is None:
        return None
    project["milestones"] = conn.execute(
        "SELECT id, name, position, status, due_date FROM milestones WHERE project_id = %s ORDER BY position",
        (project_id,)).fetchall()
    project["tasks"] = conn.execute(
        """
        SELECT t.id, t.milestone_id, t.title, t.status, t.position, t.due_date, t.client_visible,
               t.assignee_name, t.assignee_email, t.completed_at, t.clickup_task_id, t.clickup_url,
               j.status AS clickup_job_status, j.last_error_code AS clickup_job_error, j.id AS clickup_job_id
        FROM tasks t
        LEFT JOIN LATERAL (
            SELECT id, status, last_error_code FROM integration_jobs
            WHERE subject_type = 'task' AND subject_id = t.id AND job_type = 'clickup.create_task'
            ORDER BY created_at DESC LIMIT 1) j ON true
        WHERE t.project_id = %s ORDER BY t.position, t.id
        """, (project_id,)).fetchall()
    project["integration_jobs"] = conn.execute(
        """
        SELECT job_type, status, count(*) AS n FROM integration_jobs
        WHERE (subject_type = 'project' AND subject_id = %(p)s)
           OR (subject_type = 'task' AND subject_id IN (SELECT id FROM tasks WHERE project_id = %(p)s))
        GROUP BY job_type, status ORDER BY job_type, status
        """, {"p": project_id}).fetchall()
    project["activity"] = list_activity(conn, project_id=project_id, limit=50, offset=0)[0]
    return project


# ------------------------------------------------------------------ activity
def list_activity(conn: Connection, *, client_id: UUID | None = None, project_id: UUID | None = None,
                  event_prefix: str | None = None, limit: int = 50, offset: int = 0):
    return _paged(
        conn,
        """
        SELECT a.id, a.event_type, a.summary, a.actor_type, a.client_visible, a.created_at, a.data,
               a.client_id, a.project_id, c.name AS client_name, p.name AS project_name,
               count(*) OVER () AS _total
        FROM activity_events a
        LEFT JOIN clients c ON c.id = a.client_id
        LEFT JOIN projects p ON p.id = a.project_id
        WHERE (%(client)s::uuid IS NULL OR a.client_id = %(client)s)
          AND (%(project)s::uuid IS NULL OR a.project_id = %(project)s)
          AND (%(prefix)s::text IS NULL OR starts_with(a.event_type, %(prefix)s))
        ORDER BY a.created_at DESC, a.id DESC
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        {"client": client_id, "project": project_id, "prefix": event_prefix, "limit": limit, "offset": offset},
    )


# -------------------------------------------------------------- integrations
JOB_SCOPE_SQL = """
    LEFT JOIN tasks jt ON j.subject_type = 'task' AND jt.id = j.subject_id
    LEFT JOIN projects jp ON jp.id = CASE WHEN j.subject_type = 'project' THEN j.subject_id ELSE jt.project_id END
"""


def list_jobs(conn: Connection, *, job_type: str | None, status: str | None, project_id: UUID | None,
              created_from: Any, created_to: Any, limit: int, offset: int):
    return _paged(
        conn,
        f"""
        SELECT j.id, j.job_type, j.subject_type, j.subject_id, j.status, j.attempts, j.max_attempts,
               j.next_run_at, j.last_error_code, j.created_at, j.completed_at, j.updated_at,
               jp.id AS project_id, jp.name AS project_name, jt.title AS task_title,
               count(*) OVER () AS _total
        FROM integration_jobs j {JOB_SCOPE_SQL}
        WHERE (%(type)s::text IS NULL OR j.job_type = %(type)s)
          AND (%(status)s::text IS NULL OR j.status = %(status)s)
          AND (%(project)s::uuid IS NULL OR jp.id = %(project)s)
          AND (%(from)s::date IS NULL OR j.created_at >= %(from)s::date)
          AND (%(to)s::date IS NULL OR j.created_at < %(to)s::date + 1)
        ORDER BY j.created_at DESC, j.id
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        {"type": job_type, "status": status, "project": project_id, "from": created_from, "to": created_to,
         "limit": limit, "offset": offset},
    )


def get_job(conn: Connection, job_id: UUID) -> dict[str, Any] | None:
    job = conn.execute(
        f"""
        SELECT j.id, j.job_type, j.idempotency_key, j.subject_type, j.subject_id, j.status, j.attempts,
               j.max_attempts, j.next_run_at, j.locked_by, j.locked_until, j.last_error_code, j.last_error,
               j.created_at, j.completed_at, j.updated_at, j.payload -> 'reconciliation' AS reconciliation,
               j.payload -> 'resolution' AS resolution,
               jp.id AS project_id, jp.name AS project_name, jt.title AS task_title
        FROM integration_jobs j {JOB_SCOPE_SQL}
        WHERE j.id = %s
        """, (job_id,)).fetchone()
    if job is None:
        return None
    job["attempt_history"] = conn.execute(
        """
        SELECT attempt_number, outcome, worker_id, started_at, finished_at, http_status, error_code,
               error_message, external_id, duration_ms
        FROM integration_attempts WHERE job_id = %s ORDER BY attempt_number
        """, (job_id,)).fetchall()
    last = job["attempt_history"][-1]["outcome"] if job["attempt_history"] else None
    job["requires_reconciliation"] = job["status"] == "dead" and (
        last == "unknown" or job["last_error_code"] in ("UNKNOWN_OUTCOME_REQUIRES_RECONCILIATION", "LEASE_EXPIRED"))
    return job


def job_counts(conn: Connection) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT job_type, status, count(*) AS n FROM integration_jobs GROUP BY job_type, status ORDER BY 1, 2"
    ).fetchall()


def last_success(conn: Connection, job_type: str):
    return conn.execute(
        "SELECT max(a.finished_at) AS at FROM integration_attempts a JOIN integration_jobs j ON j.id = a.job_id "
        "WHERE j.job_type = %s AND a.outcome = 'success'", (job_type,)).fetchone()["at"]


def list_webhook_events(conn: Connection, *, status: str | None, source: str | None, limit: int, offset: int):
    """Event history without contact data. The client/invitation link comes from
    the invitation for the event's (source, deal id), since webhook_events has
    no foreign keys to them."""
    return _paged(
        conn,
        """
        SELECT e.id, e.source, e.external_event_id, e.event_type, e.status, e.error_code, e.duplicate_count,
               e.last_duplicate_at, e.processed_at, e.created_at,
               e.payload -> 'deal' ->> 'external_id' AS deal_id,
               e.payload -> 'deal' ->> 'service_type' AS service_type,
               i.id AS invitation_id, i.client_id, c.name AS client_name,
               count(*) OVER () AS _total
        FROM webhook_events e
        LEFT JOIN onboarding_invitations i
               ON i.crm_source = e.source AND i.crm_deal_id = e.payload -> 'deal' ->> 'external_id'
        LEFT JOIN clients c ON c.id = i.client_id
        WHERE (%(status)s::text IS NULL OR e.status = %(status)s)
          AND (%(source)s::text IS NULL OR e.source = %(source)s)
        ORDER BY e.created_at DESC, e.id
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        {"status": status, "source": source, "limit": limit, "offset": offset},
    )
