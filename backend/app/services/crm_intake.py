"""CRM deal-won intake: one PostgreSQL transaction per delivery.

Inside a single transaction:

1. Claim the event: INSERT ... ON CONFLICT (source, external_event_id) DO NOTHING.
   * Already stored with a different payload -> EventIdReusedError (HTTP 409),
     nothing written.
   * Already handled (processed, or rejected as duplicate_deal) -> count the
     redelivery, return "duplicate".
   * Previously rejected for an identity conflict -> re-evaluate it now.
2. Look up the active template for deal.service_type (unknown -> rollback, 422).
3. In a SAVEPOINT: resolve or create the client, create the invitation and the
   hashed access token, record activity.
   * Deal already has an invitation -> roll back to the savepoint (so nothing
     the losing request did survives) and mark the event duplicate_deal.
   * Identity conflict -> roll back to the savepoint and mark the event
     rejected with the conflict code (HTTP 409).
4. Mark the event processed. Commit.

Event and business records therefore always commit together; an event can
never be stuck "received". Races are resolved by the unique constraints
(webhook_events, clients, onboarding_invitations); a transaction that still
loses a race (unique violation in an UPDATE, deadlock) is retried whole.

No external HTTP calls are made here.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import psycopg

from app import db
from app.errors import EventIdReusedError, IntakeRetryExhaustedError, UnknownServiceTypeError
from app.schemas.crm import CrmContact, DealWonEvent
from app.security import generate_token, hash_token
from app.store import activity, clients, onboarding, webhook_events

logger = logging.getLogger(__name__)

INVITATION_TTL_DAYS = 30
MAX_TRANSACTION_ATTEMPTS = 3

IntakeStatus = Literal["created", "duplicate", "duplicate_deal", "identity_conflict"]


@dataclass(frozen=True)
class IntakeResult:
    status: IntakeStatus
    event_id: str
    client_id: UUID | None = None
    invitation_id: UUID | None = None
    onboarding_url: str | None = None  # contains the raw token: return once, never log
    expires_at: datetime | None = None
    conflict_code: str | None = None
    conflict_message: str | None = None


class _DuplicateDeal(Exception):
    pass


class _IdentityConflict(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def process_deal_won(event: DealWonEvent, *, app_base_url: str) -> IntakeResult:
    payload = event.normalized_payload()
    for attempt in range(1, MAX_TRANSACTION_ATTEMPTS + 1):
        try:
            return _process_once(event, payload, app_base_url)
        except (psycopg.errors.UniqueViolation, psycopg.errors.TransactionRollback) as exc:
            # The transaction was rolled back as a whole; nothing was committed.
            logger.info(
                "crm intake attempt %d for event %s lost a race (%s); retrying",
                attempt, event.event_id, type(exc).__name__,
            )
            if attempt == MAX_TRANSACTION_ATTEMPTS:
                raise IntakeRetryExhaustedError(event.event_id) from exc
    raise AssertionError("unreachable")


def _process_once(event: DealWonEvent, payload: dict[str, Any], app_base_url: str) -> IntakeResult:
    with db.transaction() as conn:
        event_row_id = webhook_events.claim(
            conn,
            source=event.source,
            external_event_id=event.event_id,
            event_type=event.event_type,
            payload=payload,
        )
        if event_row_id is None:
            existing = webhook_events.lock_existing(
                conn, source=event.source, external_event_id=event.event_id, payload=payload
            )
            if existing is None:  # pragma: no cover - the conflicting row was committed
                raise psycopg.errors.SerializationFailure("event row vanished")
            if not existing["same_payload"]:
                raise EventIdReusedError(event.event_id)
            webhook_events.record_redelivery(conn, existing["id"])
            if existing["status"] == "processed" or existing["error_code"] == "duplicate_deal":
                return IntakeResult(status="duplicate", event_id=event.event_id)
            # Previously rejected for an identity conflict: evaluate again, the
            # conflicting data may have been corrected since.
            event_row_id = existing["id"]

        template = onboarding.get_active_template(conn, event.deal.service_type)
        if template is None:
            raise UnknownServiceTypeError(event.deal.service_type)

        try:
            with conn.transaction():  # SAVEPOINT
                result = _intake(conn, event, template, app_base_url)
        except _DuplicateDeal:
            webhook_events.mark(conn, event_row_id, status="rejected", error_code="duplicate_deal")
            return IntakeResult(status="duplicate_deal", event_id=event.event_id)
        except _IdentityConflict as conflict:
            webhook_events.mark(conn, event_row_id, status="rejected", error_code=conflict.code)
            return IntakeResult(
                status="identity_conflict",
                event_id=event.event_id,
                conflict_code=conflict.code,
                conflict_message=conflict.message,
            )
        webhook_events.mark(conn, event_row_id, status="processed", error_code=None)
        return result


def _intake(conn: db.Connection, event: DealWonEvent, template: dict[str, Any], app_base_url: str) -> IntakeResult:
    deal = event.deal
    if onboarding.find_invitation_by_deal(conn, event.source, deal.external_id) is not None:
        raise _DuplicateDeal()

    client, matched_by = _resolve_client(conn, event.contact, event.source)
    client_id = client["id"]
    common = {"source": event.source, "event_id": event.event_id, "deal_id": deal.external_id}

    activity.record(
        conn, client_id=client_id, actor_type="crm", event_type="crm.deal_received",
        summary=f"Deal won in CRM: {deal.name or deal.external_id}",
        data={**common, "service_type": deal.service_type, "deal_name": deal.name,
              "value_cents": deal.value_cents, "currency": deal.currency},
    )
    if matched_by == "created":
        activity.record(
            conn, client_id=client_id, actor_type="system", event_type="client.created",
            summary=f"Client created: {client['name']}", data=common,
        )
    else:
        activity.record(
            conn, client_id=client_id, actor_type="system", event_type="client.matched",
            summary=f"Existing client matched by {matched_by.replace('_', ' ')}",
            data={**common, "matched_by": matched_by},
        )

    invitation = onboarding.insert_invitation(
        conn,
        client_id=client_id,
        crm_source=event.source,
        crm_deal_id=deal.external_id,
        deal_name=deal.name,
        deal_value_cents=deal.value_cents,
        currency=deal.currency,
        service_type=deal.service_type,
        template_id=template["id"],
        ttl_days=INVITATION_TTL_DAYS,
    )
    if invitation is None:
        # A concurrent delivery of the same deal committed first.
        raise _DuplicateDeal()

    raw_token = generate_token()
    onboarding.insert_access_token(
        conn,
        client_id=client_id,
        invitation_id=invitation["id"],
        token_hash=hash_token(raw_token),
        expires_at=invitation["expires_at"],
    )
    activity.record(
        conn, client_id=client_id, actor_type="system", event_type="onboarding.invitation_created",
        summary=f"Onboarding invitation created ({template['name']})",
        data={**common, "invitation_id": str(invitation["id"]), "template_key": template["key"],
              "expires_at": invitation["expires_at"].isoformat()},
    )
    return IntakeResult(
        status="created",
        event_id=event.event_id,
        client_id=client_id,
        invitation_id=invitation["id"],
        onboarding_url=f"{app_base_url}/portal#t={raw_token}",
        expires_at=invitation["expires_at"],
    )


# --------------------------------------------------------- client identity
def _normalize_company(value: str | None) -> str | None:
    return re.sub(r"\s+", " ", value).strip().casefold() if value else None


def _lookup(conn: db.Connection, contact: CrmContact, source: str) -> tuple[dict | None, str | None]:
    by_crm = clients.find_by_crm_identity(conn, source, contact.external_id) if contact.external_id else None
    by_email = clients.find_by_email(conn, contact.email) if contact.email else None
    if by_crm and by_email and by_crm["id"] != by_email["id"]:
        raise _IdentityConflict(
            "identity_mismatch",
            "The contact's CRM ID and email belong to two different clients.",
        )
    if by_crm:
        return by_crm, "crm_external_id"
    if by_email:
        return by_email, "email"
    return None, None


def _resolve_client(conn: db.Connection, contact: CrmContact, source: str) -> tuple[dict[str, Any], str]:
    """Match by (source, CRM ID), then by email, else create. Never by name."""
    match, matched_by = _lookup(conn, contact, source)
    if match is None:
        created = clients.insert(
            conn,
            name=contact.name,
            email=contact.email,
            company=contact.company,
            phone=contact.phone,
            crm_source=source if contact.external_id else None,
            crm_external_id=contact.external_id,
        )
        if created is not None:
            return created, "created"
        # Lost an insert race to a committed client with the same identity.
        match, matched_by = _lookup(conn, contact, source)
        if match is None:  # pragma: no cover - conflict row must be visible now
            raise psycopg.errors.SerializationFailure("client identity race")

    _check_conflicts(match, contact, source)

    fill: dict[str, Any] = {}
    if contact.email and match["email"] is None:
        fill["email"] = contact.email
    if contact.company and match["company"] is None:
        fill["company"] = contact.company
    if contact.phone and match["phone"] is None:
        fill["phone"] = contact.phone
    # Attach a CRM identity only when the client has none yet. A client that
    # already belongs to another CRM keeps that identity (one identity slot).
    if contact.external_id and match["crm_source"] is None:
        fill["crm_source"] = source
        fill["crm_external_id"] = contact.external_id
    return clients.fill_missing(conn, match["id"], fill), matched_by


def _check_conflicts(client: dict[str, Any], contact: CrmContact, source: str) -> None:
    # Policy: an archived client is never reactivated as a side effect of a CRM
    # deal. The delivery is rejected (stored, re-evaluated on redelivery) until
    # an administrator restores the client.
    if client["status"] == "archived":
        raise _IdentityConflict(
            "client_archived",
            "The matching client is archived; an administrator must restore it before new deals are accepted.",
        )
    if (
        contact.external_id
        and client["crm_source"] == source
        and client["crm_external_id"] != contact.external_id
    ):
        raise _IdentityConflict(
            "crm_identity_conflict",
            "The email belongs to a client linked to a different contact in the same CRM.",
        )
    existing_company = _normalize_company(client["company"])
    incoming_company = _normalize_company(contact.company)
    if existing_company and incoming_company and existing_company != incoming_company:
        raise _IdentityConflict(
            "company_conflict",
            "The matched client belongs to a different company.",
        )
