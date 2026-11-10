"""SQL for projects, milestones and tasks. All functions run on the caller's transaction.

Every portal read takes BOTH the project id and the client id (or the
invitation id), so a query can never return another client's rows even if an
id is guessed.
"""

from datetime import date
from typing import Any
from uuid import UUID

from app.db import Connection


def insert_project(
    conn: Connection,
    *,
    client_id: UUID,
    invitation_id: UUID,
    template_id: UUID,
    template_version: int,
    name: str,
    start_date: date,
    due_date: date,
) -> dict[str, Any]:
    return conn.execute(
        """
        INSERT INTO projects (client_id, invitation_id, template_id, template_version, name, status,
                              start_date, due_date)
        VALUES (%s, %s, %s, %s, %s, 'active', %s, %s)
        RETURNING id, sync_version
        """,
        (client_id, invitation_id, template_id, template_version, name, start_date, due_date),
    ).fetchone()


def insert_milestone(
    conn: Connection, *, project_id: UUID, key: str, name: str, position: int, due_date: date
) -> UUID:
    return conn.execute(
        """
        INSERT INTO milestones (project_id, template_milestone_key, name, position, due_date, status)
        VALUES (%s, %s, %s, %s, %s, 'pending')
        RETURNING id
        """,
        (project_id, key, name, position, due_date),
    ).fetchone()["id"]


def insert_task(
    conn: Connection,
    *,
    project_id: UUID,
    milestone_id: UUID,
    key: str,
    title: str,
    description: str | None,
    position: int,
    due_date: date,
    client_visible: bool,
) -> UUID:
    return conn.execute(
        """
        INSERT INTO tasks (project_id, milestone_id, template_task_key, title, description, status,
                           position, due_date, client_visible)
        VALUES (%s, %s, %s, %s, %s, 'todo', %s, %s, %s)
        RETURNING id
        """,
        (project_id, milestone_id, key, title, description, position, due_date, client_visible),
    ).fetchone()["id"]


# ------------------------------------------------------------ portal reads
def get_project_for_invitation(conn: Connection, *, invitation_id: UUID, client_id: UUID) -> dict[str, Any] | None:
    return conn.execute(
        """
        SELECT p.id, p.name, p.status, p.start_date, p.due_date, p.created_at,
               s.answers ->> 'project_description' AS description
        FROM projects p
        LEFT JOIN onboarding_submissions s ON s.invitation_id = p.invitation_id AND s.client_id = p.client_id
        WHERE p.invitation_id = %s AND p.client_id = %s
        """,
        (invitation_id, client_id),
    ).fetchone()


def task_counts(conn: Connection, *, project_id: UUID, client_id: UUID) -> dict[str, int]:
    return conn.execute(
        """
        SELECT count(*) AS total, count(*) FILTER (WHERE t.status = 'done') AS completed
        FROM tasks t JOIN projects p ON p.id = t.project_id
        WHERE t.project_id = %s AND p.client_id = %s
        """,
        (project_id, client_id),
    ).fetchone()


def list_milestones(conn: Connection, *, project_id: UUID, client_id: UUID) -> list[dict[str, Any]]:
    return conn.execute(
        """
        SELECT m.id, m.name, m.position, m.due_date, m.status
        FROM milestones m JOIN projects p ON p.id = m.project_id
        WHERE m.project_id = %s AND p.client_id = %s
        ORDER BY m.position
        """,
        (project_id, client_id),
    ).fetchall()


def list_client_visible_tasks(conn: Connection, *, project_id: UUID, client_id: UUID) -> list[dict[str, Any]]:
    return conn.execute(
        """
        SELECT t.milestone_id, t.title, t.description, t.status, t.due_date, t.completed_at
        FROM tasks t JOIN projects p ON p.id = t.project_id
        WHERE t.project_id = %s AND p.client_id = %s AND t.client_visible
        ORDER BY t.position
        """,
        (project_id, client_id),
    ).fetchall()


def list_client_visible_activity(
    conn: Connection, *, project_id: UUID, client_id: UUID, limit: int = 50
) -> list[dict[str, Any]]:
    return conn.execute(
        """
        SELECT event_type, summary, created_at
        FROM activity_events
        WHERE project_id = %s AND client_id = %s AND client_visible
        ORDER BY created_at DESC, id DESC
        LIMIT %s
        """,
        (project_id, client_id, limit),
    ).fetchall()
