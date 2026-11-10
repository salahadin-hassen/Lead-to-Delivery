"""SQL for clients. All functions run on the caller's transaction."""

from typing import Any
from uuid import UUID

from app.db import Connection

_COLUMNS = "id, name, company, email, phone, crm_source, crm_external_id, status"


def find_by_crm_identity(conn: Connection, source: str, external_id: str) -> dict[str, Any] | None:
    return conn.execute(
        f"SELECT {_COLUMNS} FROM clients WHERE crm_source = %s AND crm_external_id = %s FOR UPDATE",
        (source, external_id),
    ).fetchone()


def find_by_email(conn: Connection, email: str) -> dict[str, Any] | None:
    # Served by the unique index on lower(email).
    return conn.execute(
        f"SELECT {_COLUMNS} FROM clients WHERE lower(email) = lower(%s) FOR UPDATE", (email,)
    ).fetchone()


def insert(
    conn: Connection,
    *,
    name: str,
    email: str | None,
    company: str | None,
    phone: str | None,
    crm_source: str | None,
    crm_external_id: str | None,
) -> dict[str, Any] | None:
    """Create a client; None if the email or CRM identity is already taken.

    ON CONFLICT DO NOTHING (no target) covers both unique indexes, so a
    concurrent insert of the same identity waits, then yields None instead of
    an error; the caller re-resolves and finds the committed row.
    """
    return conn.execute(
        f"""
        INSERT INTO clients (name, email, company, phone, crm_source, crm_external_id)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        RETURNING {_COLUMNS}
        """,
        (name, email, company, phone, crm_source, crm_external_id),
    ).fetchone()


def fill_missing(conn: Connection, client_id: UUID, fields: dict[str, Any]) -> dict[str, Any]:
    """Set only the given columns, and only where they are currently NULL.

    ``fields`` keys must come from the fixed set below (never from input).
    """
    allowed = {"email", "company", "phone", "crm_source", "crm_external_id"}
    if not fields:
        return conn.execute(f"SELECT {_COLUMNS} FROM clients WHERE id = %s", (client_id,)).fetchone()
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unexpected client fields: {sorted(unknown)}")
    assignments = ", ".join(f"{col} = COALESCE({col}, %({col})s)" for col in sorted(fields))
    return conn.execute(
        f"UPDATE clients SET {assignments} WHERE id = %(id)s RETURNING {_COLUMNS}",
        {**fields, "id": client_id},
    ).fetchone()
