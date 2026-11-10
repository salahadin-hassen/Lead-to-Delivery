"""SQL for project templates, onboarding invitations and client access tokens."""

from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db import Connection


def get_active_template(conn: Connection, key: str) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT id, key, name, version FROM project_templates WHERE key = %s AND active",
        (key,),
    ).fetchone()


def find_invitation_by_deal(conn: Connection, crm_source: str, crm_deal_id: str) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT id, client_id FROM onboarding_invitations WHERE crm_source = %s AND crm_deal_id = %s",
        (crm_source, crm_deal_id),
    ).fetchone()


def insert_invitation(
    conn: Connection,
    *,
    client_id: UUID,
    crm_source: str,
    crm_deal_id: str,
    deal_name: str | None,
    deal_value_cents: int | None,
    currency: str | None,
    service_type: str,
    template_id: UUID,
    ttl_days: int,
) -> dict[str, Any] | None:
    """Create a pending invitation; None if this CRM deal already has one."""
    return conn.execute(
        """
        INSERT INTO onboarding_invitations
            (client_id, crm_source, crm_deal_id, deal_name, deal_value_cents, currency,
             service_type, template_id, status, expires_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', now() + make_interval(days => %s))
        ON CONFLICT (crm_source, crm_deal_id) DO NOTHING
        RETURNING id, client_id, expires_at
        """,
        (client_id, crm_source, crm_deal_id, deal_name, deal_value_cents, currency,
         service_type, template_id, ttl_days),
    ).fetchone()


def insert_access_token(
    conn: Connection, *, client_id: UUID, invitation_id: UUID, token_hash: str, expires_at: datetime
) -> UUID:
    """Store the SHA-256 hash of a portal token. The raw token never reaches SQL."""
    return conn.execute(
        """
        INSERT INTO client_access_tokens (client_id, invitation_id, token_hash, expires_at)
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (client_id, invitation_id, token_hash, expires_at),
    ).fetchone()["id"]


# ------------------------------------------------------------ client portal
def get_token_context(conn: Connection, token_hash: str) -> dict[str, Any] | None:
    """Token + its invitation + client, by SHA-256 hash. Read-only.

    The join repeats client_id so a token can only ever resolve to its own
    invitation's client (also guaranteed by the composite foreign key).
    """
    return conn.execute(
        """
        SELECT t.id AS token_id, t.client_id, t.invitation_id, t.expires_at AS token_expires_at,
               t.revoked_at, i.status AS invitation_status, i.expires_at AS invitation_expires_at,
               i.submitted_at, i.service_type, i.deal_name, i.template_id,
               c.name AS client_name, c.company AS client_company,
               pt.name AS template_name,
               now() AS db_now
        FROM client_access_tokens t
        JOIN onboarding_invitations i ON i.id = t.invitation_id AND i.client_id = t.client_id
        JOIN clients c ON c.id = t.client_id
        LEFT JOIN project_templates pt ON pt.id = i.template_id
        WHERE t.token_hash = %s
        """,
        (token_hash,),
    ).fetchone()


def token_still_valid(conn: Connection, token_id: UUID) -> bool:
    """Re-check inside a write transaction (the token may have been revoked since auth)."""
    row = conn.execute(
        "SELECT 1 FROM client_access_tokens WHERE id = %s AND revoked_at IS NULL AND expires_at > now() FOR SHARE",
        (token_id,),
    ).fetchone()
    return row is not None


def lock_invitation(conn: Connection, invitation_id: UUID, client_id: UUID) -> dict[str, Any] | None:
    return conn.execute(
        """
        SELECT id, client_id, status, expires_at, expires_at <= now() AS is_expired,
               template_id, service_type
        FROM onboarding_invitations
        WHERE id = %s AND client_id = %s
        FOR UPDATE
        """,
        (invitation_id, client_id),
    ).fetchone()


def get_template_by_id(conn: Connection, template_id: UUID) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT id, key, name, version, definition FROM project_templates WHERE id = %s",
        (template_id,),
    ).fetchone()


def insert_submission(conn: Connection, *, invitation_id: UUID, client_id: UUID, answers: dict[str, Any]) -> UUID:
    return conn.execute(
        "INSERT INTO onboarding_submissions (invitation_id, client_id, answers) VALUES (%s, %s, %s) RETURNING id",
        (invitation_id, client_id, Jsonb(answers)),
    ).fetchone()["id"]


def mark_invitation_submitted(conn: Connection, invitation_id: UUID) -> None:
    updated = conn.execute(
        "UPDATE onboarding_invitations SET status = 'submitted', submitted_at = now() "
        "WHERE id = %s AND status = 'pending'",
        (invitation_id,),
    ).rowcount
    if updated != 1:
        raise RuntimeError("invitation was not pending at submission time")
