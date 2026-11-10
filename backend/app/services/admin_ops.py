"""Administrator write operations. Each runs in one transaction and records
activity; nothing here stores or logs a token."""

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from app import db
from app.errors import ConflictError, NotFoundError
from app.schemas.admin import ClientCreate, ClientUpdate, ProjectUpdate, TaskUpdate
from app.security import generate_token, hash_token
from app.store import activity, jobs, onboarding

logger = logging.getLogger("app.admin")

PROJECT_ACCESS_DAYS = 180

_CLIENT_COLUMNS = "id, name, company, email, phone, status, crm_source, crm_external_id, created_at, updated_at"


def _email_conflict(exc: psycopg.errors.UniqueViolation) -> ConflictError:
    if exc.diag.constraint_name == "clients_email_lower_key":
        return ConflictError("email_taken", "Another client already uses this email address.")
    return ConflictError("conflict", "The change conflicts with an existing record.")


# ------------------------------------------------------------------ clients
def create_client(data: ClientCreate) -> dict[str, Any]:
    try:
        with db.transaction() as conn:
            client = conn.execute(
                f"INSERT INTO clients (name, email, company, phone) VALUES (%s, %s, %s, %s) RETURNING {_CLIENT_COLUMNS}",
                (data.name, data.email, data.company, data.phone)).fetchone()
            activity.record(conn, client_id=client["id"], actor_type="admin", event_type="client.created",
                            summary=f"Client created by administrator: {client['name']}")
    except psycopg.errors.UniqueViolation as exc:
        raise _email_conflict(exc) from exc
    return client


def update_client(client_id: UUID, data: ClientUpdate) -> dict[str, Any]:
    requested = {k: getattr(data, k) for k in data.model_fields_set}
    if "name" in requested and requested["name"] is None:
        raise ConflictError("name_required", "Name cannot be empty.")
    try:
        with db.transaction() as conn:
            current = conn.execute(f"SELECT {_CLIENT_COLUMNS} FROM clients WHERE id = %s FOR UPDATE",
                                   (client_id,)).fetchone()
            if current is None:
                raise NotFoundError()
            changes = {k: v for k, v in requested.items() if current[k] != v}
            if not changes:
                return current
            assignments = ", ".join(f"{col} = %({col})s" for col in sorted(changes))
            updated = conn.execute(f"UPDATE clients SET {assignments} WHERE id = %(id)s RETURNING {_CLIENT_COLUMNS}",
                                   {**changes, "id": client_id}).fetchone()
            activity.record(conn, client_id=client_id, actor_type="admin", event_type="client.updated",
                            summary=f"Client details edited ({', '.join(sorted(changes))})",
                            data={"fields": sorted(changes)})
    except psycopg.errors.UniqueViolation as exc:
        raise _email_conflict(exc) from exc
    return updated


def set_client_status(client_id: UUID, status: str) -> dict[str, Any]:
    """archive / restore. Never deletes projects, tasks, submissions or history."""
    with db.transaction() as conn:
        current = conn.execute("SELECT status, name FROM clients WHERE id = %s FOR UPDATE", (client_id,)).fetchone()
        if current is None:
            raise NotFoundError()
        if current["status"] != status:
            conn.execute("UPDATE clients SET status = %s WHERE id = %s", (status, client_id))
            verb = "archived" if status == "archived" else "restored"
            activity.record(conn, client_id=client_id, actor_type="admin", event_type=f"client.{verb}",
                            summary=f"Client {verb}: {current['name']}")
        return conn.execute(f"SELECT {_CLIENT_COLUMNS} FROM clients WHERE id = %s", (client_id,)).fetchone()


# ------------------------------------------------------- access links
def regenerate_access_link(invitation_id: UUID, *, app_base_url: str) -> tuple[str, datetime, str]:
    """Revoke every active token of the invitation and issue one new token.

    * pending, not expired  -> onboarding link, expires with the invitation
      (an expired invitation is never silently reopened);
    * submitted             -> project-access link valid for 180 days,
      independent of the original onboarding deadline;
    * expired / revoked     -> ConflictError.

    Returns (url, expires_at, kind). The raw token exists only in the URL.
    """
    with db.transaction() as conn:
        inv = conn.execute(
            "SELECT id, client_id, status, expires_at, expires_at <= now() AS is_expired "
            "FROM onboarding_invitations WHERE id = %s FOR UPDATE", (invitation_id,)).fetchone()
        if inv is None:
            raise NotFoundError()
        if inv["status"] == "revoked":
            raise ConflictError("invitation_revoked", "This invitation was revoked.")
        if inv["status"] == "submitted":
            kind = "project_access"
            expires_at = conn.execute("SELECT now() + make_interval(days => %s) AS e",
                                      (PROJECT_ACCESS_DAYS,)).fetchone()["e"]
        elif inv["status"] == "pending" and not inv["is_expired"]:
            kind = "onboarding"
            expires_at = inv["expires_at"]
        else:
            raise ConflictError("invitation_expired",
                                "This onboarding invitation has expired; it cannot be reopened by regenerating the link.")
        revoked = conn.execute(
            "UPDATE client_access_tokens SET revoked_at = now() WHERE invitation_id = %s AND revoked_at IS NULL",
            (invitation_id,)).rowcount
        raw = generate_token()
        onboarding.insert_access_token(conn, client_id=inv["client_id"], invitation_id=invitation_id,
                                       token_hash=hash_token(raw), expires_at=expires_at)
        project = conn.execute("SELECT id FROM projects WHERE invitation_id = %s", (invitation_id,)).fetchone()
        activity.record(
            conn, client_id=inv["client_id"], project_id=project["id"] if project else None, actor_type="admin",
            event_type="invitation.access_link_regenerated",
            summary=("Project access link regenerated" if kind == "project_access"
                     else "Onboarding link regenerated"),
            data={"invitation_id": str(invitation_id), "kind": kind, "revoked_tokens": revoked,
                  "expires_at": expires_at.isoformat()},
        )
    logger.info("access link regenerated: kind=%s revoked_tokens=%d", kind, revoked)
    return f"{app_base_url}/portal#t={raw}", expires_at, kind


# ----------------------------------------------------------- projects/tasks
def _queue_sheets_sync(conn: db.Connection, project_id: UUID, max_attempts: int) -> int:
    """Bump sync_version and queue a coalesced Sheets upsert (worker: Sheets
    adapter not implemented yet, so it stays pending)."""
    version = conn.execute("UPDATE projects SET sync_version = sync_version + 1 WHERE id = %s RETURNING sync_version",
                           (project_id,)).fetchone()["sync_version"]
    jobs.enqueue(conn, job_type="sheets.upsert_project", idempotency_key=f"sheets.upsert_project:{project_id}:{version}",
                 coalesce_key=f"sheets.upsert_project:{project_id}", subject_type="project", subject_id=project_id,
                 max_attempts=max_attempts, payload={"sync_version": version})
    return version


def update_project(project_id: UUID, data: ProjectUpdate, *, max_attempts: int) -> bool:
    """Returns True if something changed."""
    with db.transaction() as conn:
        current = conn.execute("SELECT status, client_id, name FROM projects WHERE id = %s FOR UPDATE",
                               (project_id,)).fetchone()
        if current is None:
            raise NotFoundError()
        if current["status"] == data.status:
            return False
        conn.execute("UPDATE projects SET status = %s WHERE id = %s", (data.status, project_id))
        _queue_sheets_sync(conn, project_id, max_attempts)
        activity.record(conn, client_id=current["client_id"], project_id=project_id, actor_type="admin",
                        event_type="project.status_changed", client_visible=True,
                        summary=f"Project status changed to {data.status.replace('_', ' ')}",
                        data={"from": current["status"], "to": data.status})
    return True


def update_task(project_id: UUID, task_id: UUID, data: TaskUpdate, *, max_attempts: int) -> bool:
    requested = {k: getattr(data, k) for k in data.model_fields_set}
    if "status" in requested and requested["status"] is None:
        raise ConflictError("status_required", "Status cannot be empty.")
    with db.transaction() as conn:
        task = conn.execute(
            "SELECT t.id, t.title, t.status, t.assignee_name, t.assignee_email, t.client_visible, p.client_id "
            "FROM tasks t JOIN projects p ON p.id = t.project_id "
            "WHERE t.id = %s AND t.project_id = %s FOR UPDATE OF t, p", (task_id, project_id)).fetchone()
        if task is None:
            raise NotFoundError()
        changes = {k: v for k, v in requested.items() if task[k] != v}
        if not changes:
            return False
        if "status" in changes:
            conn.execute(
                "UPDATE tasks SET status = %s, completed_at = CASE WHEN %s = 'done' THEN now() ELSE NULL END "
                "WHERE id = %s", (changes["status"], changes["status"], task_id))
            _queue_sheets_sync(conn, project_id, max_attempts)
            label = {"todo": "to do", "in_progress": "in progress", "done": "done"}[changes["status"]]
            activity.record(conn, client_id=task["client_id"], project_id=project_id, actor_type="admin",
                            event_type="task.status_changed", client_visible=task["client_visible"],
                            summary=f"Task '{task['title']}' marked {label}",
                            data={"task_id": str(task_id), "from": task["status"], "to": changes["status"]})
        assignment = {k: v for k, v in changes.items() if k in ("assignee_name", "assignee_email")}
        if assignment:
            conn.execute("UPDATE tasks SET assignee_name = %s, assignee_email = %s WHERE id = %s",
                         (assignment.get("assignee_name", task["assignee_name"]),
                          assignment.get("assignee_email", task["assignee_email"]), task_id))
            activity.record(conn, client_id=task["client_id"], project_id=project_id, actor_type="admin",
                            event_type="task.assigned", summary=f"Task '{task['title']}' assignment changed",
                            data={"task_id": str(task_id), "fields": sorted(assignment)})
    return True
