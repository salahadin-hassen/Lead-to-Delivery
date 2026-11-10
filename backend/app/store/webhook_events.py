"""SQL for webhook_events. All functions run on the caller's transaction.

Status usage (CHECK allows received/processed/rejected/failed):
  processed                          intake created the client/invitation
  rejected + error_code=duplicate_deal   deal already had an invitation
  rejected + error_code=<identity conflict code>   re-evaluated on redelivery
'received' only exists inside the intake transaction and is never committed.
"""

from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db import Connection


def claim(
    conn: Connection, *, source: str, external_event_id: str, event_type: str, payload: dict[str, Any]
) -> UUID | None:
    """Insert the event; None if (source, external_event_id) already exists.

    A concurrent transaction inserting the same key makes this statement wait
    until that transaction commits (then None) or rolls back (then inserted).
    """
    row = conn.execute(
        """
        INSERT INTO webhook_events (source, external_event_id, event_type, payload, status)
        VALUES (%s, %s, %s, %s, 'received')
        ON CONFLICT (source, external_event_id) DO NOTHING
        RETURNING id
        """,
        (source, external_event_id, event_type, Jsonb(payload)),
    ).fetchone()
    return row["id"] if row else None


def lock_existing(
    conn: Connection, *, source: str, external_event_id: str, payload: dict[str, Any]
) -> dict[str, Any] | None:
    """Lock the stored event and report whether its payload equals this one."""
    return conn.execute(
        """
        SELECT id, status, error_code, payload = %s AS same_payload
        FROM webhook_events
        WHERE source = %s AND external_event_id = %s
        FOR UPDATE
        """,
        (Jsonb(payload), source, external_event_id),
    ).fetchone()


def record_redelivery(conn: Connection, event_row_id: UUID) -> None:
    conn.execute(
        """
        UPDATE webhook_events
        SET duplicate_count = duplicate_count + 1, last_duplicate_at = now()
        WHERE id = %s
        """,
        (event_row_id,),
    )


def mark(conn: Connection, event_row_id: UUID, *, status: str, error_code: str | None) -> None:
    conn.execute(
        "UPDATE webhook_events SET status = %s, error_code = %s, processed_at = now() WHERE id = %s",
        (status, error_code, event_row_id),
    )
