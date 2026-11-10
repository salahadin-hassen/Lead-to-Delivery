"""Client portal: token authentication and read-only views.

Authentication resolves a raw bearer token to a trusted PortalContext
(token, client AND invitation). Every portal read is scoped to that exact
invitation: a token never grants access to the client's other invitations or
projects. Reads do not modify the database (no last_used_at updates).
"""

import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app import db
from app.errors import PortalAuthError, ProjectNotFoundError
from app.schemas.onboarding import form_for
from app.schemas.portal import (
    ActivityView,
    InvitationStatus,
    MilestoneView,
    OnboardingFormResponse,
    PortalClient,
    PortalInvitation,
    PortalMe,
    ProjectView,
    TaskView,
    compute_progress,
)
from app.security import hash_token
from app.store import onboarding, projects

# secrets.token_urlsafe(32) gives 43 chars; allow some slack, reject junk early.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


@dataclass(frozen=True)
class PortalContext:
    token_id: UUID
    client_id: UUID
    invitation_id: UUID
    status: InvitationStatus
    service_type: str | None
    template_name: str | None
    deal_name: str | None
    client_name: str
    client_company: str | None
    invitation_expires_at: datetime
    submitted_at: datetime | None


def authenticate(raw_token: str | None) -> PortalContext:
    """Raise PortalAuthError(code) unless the token is valid right now.

    Codes: token_missing, token_invalid, token_expired, token_revoked.
    Only the SHA-256 hash of the token is used in SQL.
    """
    if not raw_token:
        raise PortalAuthError("token_missing")
    if not _TOKEN_RE.match(raw_token):
        raise PortalAuthError("token_invalid")
    with db.connection() as conn:
        row = onboarding.get_token_context(conn, hash_token(raw_token))
    if row is None:
        raise PortalAuthError("token_invalid")
    if row["revoked_at"] is not None or row["invitation_status"] == "revoked":
        raise PortalAuthError("token_revoked")
    if row["token_expires_at"] <= row["db_now"]:
        raise PortalAuthError("token_expired")

    status: InvitationStatus = row["invitation_status"]
    if status == "pending" and row["invitation_expires_at"] <= row["db_now"]:
        status = "expired"
    return PortalContext(
        token_id=row["token_id"],
        client_id=row["client_id"],
        invitation_id=row["invitation_id"],
        status=status,
        service_type=row["service_type"],
        template_name=row["template_name"],
        deal_name=row["deal_name"],
        client_name=row["client_name"],
        client_company=row["client_company"],
        invitation_expires_at=row["invitation_expires_at"],
        submitted_at=row["submitted_at"],
    )


def me(ctx: PortalContext) -> PortalMe:
    next_step = {"pending": "onboarding", "submitted": "project"}.get(ctx.status, "contact_agency")
    return PortalMe(
        client=PortalClient(name=ctx.client_name, company=ctx.client_company),
        invitation=PortalInvitation(
            status=ctx.status,
            service_type=ctx.service_type,
            template_name=ctx.template_name,
            deal_name=ctx.deal_name,
            expires_at=ctx.invitation_expires_at,
            submitted_at=ctx.submitted_at,
        ),
        next_step=next_step,
    )


def onboarding_form(ctx: PortalContext) -> OnboardingFormResponse:
    prefill = {}
    if ctx.status == "pending":
        prefill["project_name"] = ctx.deal_name or (
            f"{ctx.template_name} for {ctx.client_company or ctx.client_name}" if ctx.template_name else None
        )
        prefill = {k: v for k, v in prefill.items() if v}
    return OnboardingFormResponse(
        status=ctx.status,
        service_type=ctx.service_type,
        template_name=ctx.template_name,
        submitted_at=ctx.submitted_at,
        form_schema=form_for(ctx.service_type).model_json_schema(),
        prefill=prefill,
    )


def project_view(ctx: PortalContext, requested_project_id: UUID | None = None) -> ProjectView:
    """The project created from THIS invitation, or ProjectNotFoundError."""
    with db.connection() as conn:
        project = projects.get_project_for_invitation(
            conn, invitation_id=ctx.invitation_id, client_id=ctx.client_id
        )
        if project is None or (requested_project_id is not None and project["id"] != requested_project_id):
            raise ProjectNotFoundError()
        scope = {"project_id": project["id"], "client_id": ctx.client_id}
        counts = projects.task_counts(conn, **scope)
        milestones = projects.list_milestones(conn, **scope)
        tasks = projects.list_client_visible_tasks(conn, **scope)
        activity = projects.list_client_visible_activity(conn, **scope)

    def view(t: dict) -> TaskView:
        return TaskView(title=t["title"], description=t["description"], status=t["status"],
                        due_date=t["due_date"], completed_at=t["completed_at"])

    by_milestone: dict[UUID | None, list[TaskView]] = {}
    for task in tasks:
        by_milestone.setdefault(task["milestone_id"], []).append(view(task))
    return ProjectView(
        id=project["id"],
        name=project["name"],
        status=project["status"],
        description=project["description"],
        start_date=project["start_date"],
        due_date=project["due_date"],
        created_at=project["created_at"],
        progress=compute_progress(counts["total"], counts["completed"]),
        milestones=[
            MilestoneView(name=m["name"], position=m["position"], status=m["status"], due_date=m["due_date"],
                          tasks=by_milestone.get(m["id"], []))
            for m in milestones
        ],
        unassigned_tasks=by_milestone.get(None, []),
        activity=[ActivityView(type=a["event_type"], summary=a["summary"], created_at=a["created_at"])
                  for a in activity],
    )
