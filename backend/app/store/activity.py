"""SQL for the append-only activity_events timeline."""

from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db import Connection


def record(
    conn: Connection,
    *,
    event_type: str,
    summary: str,
    actor_type: str,
    client_id: UUID | None = None,
    project_id: UUID | None = None,
    data: dict[str, Any] | None = None,
    client_visible: bool = False,
) -> UUID:
    return conn.execute(
        """
        INSERT INTO activity_events (client_id, project_id, actor_type, event_type, summary, data, client_visible)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (client_id, project_id, actor_type, event_type, summary, Jsonb(data or {}), client_visible),
    ).fetchone()["id"]
