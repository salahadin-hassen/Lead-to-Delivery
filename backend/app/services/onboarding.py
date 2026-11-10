"""Onboarding submission -> project generation, in ONE PostgreSQL transaction.

Committed together or not at all:
  onboarding_submissions row, project, milestones, tasks, integration jobs
  (outbox), activity events, and the invitation's 'submitted' status.

Concurrency: the invitation row is locked (SELECT ... FOR UPDATE) before
anything else, so simultaneous submissions serialise; the loser sees
'submitted' and gets AlreadySubmittedError. UNIQUE(invitation_id) on
submissions and projects, and UNIQUE(project_id, template_task_key) on tasks,
back this up at the database level.

Milestones and tasks come only from project_templates.definition (validated
with TemplateDefinition); nothing is hard-coded per service type here.
No external API is called: ClickUp / Sheets / Slack work is queued as
integration_jobs for the worker (Task 4).
"""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import psycopg
from pydantic import ValidationError

from app import db
from app.errors import (
    AlreadySubmittedError,
    InvitationClosedError,
    PortalAuthError,
    TemplateUnavailableError,
)
from app.schemas.onboarding import OnboardingForm
from app.schemas.templates import TemplateDefinition
from app.services.portal import PortalContext
from app.store import activity, jobs, onboarding, projects

logger = logging.getLogger(__name__)

_DUPLICATE_CONSTRAINTS = {"onboarding_submissions_invitation_key", "projects_invitation_key"}


@dataclass(frozen=True)
class GenerationResult:
    project_id: UUID
    milestone_count: int
    task_count: int
    job_count: int


def _today() -> date:
    return datetime.now(UTC).date()


def submit(ctx: PortalContext, form: OnboardingForm, *, max_attempts: int) -> GenerationResult:
    try:
        return _submit(ctx, form, max_attempts=max_attempts)
    except psycopg.errors.UniqueViolation as exc:
        # Backstop only: the invitation lock normally prevents this.
        if exc.diag.constraint_name in _DUPLICATE_CONSTRAINTS:
            raise AlreadySubmittedError() from exc
        raise


def _submit(ctx: PortalContext, form: OnboardingForm, *, max_attempts: int) -> GenerationResult:
    with db.transaction() as conn:
        if not onboarding.token_still_valid(conn, ctx.token_id):
            raise PortalAuthError("token_invalid")
        invitation = onboarding.lock_invitation(conn, ctx.invitation_id, ctx.client_id)
        if invitation is None:
            raise PortalAuthError("token_invalid")
        if invitation["status"] == "submitted":
            raise AlreadySubmittedError()
        if invitation["status"] == "revoked":
            raise InvitationClosedError("invitation_revoked")
        if invitation["status"] != "pending" or invitation["is_expired"]:
            raise InvitationClosedError("invitation_expired")

        template = onboarding.get_template_by_id(conn, invitation["template_id"]) if invitation["template_id"] else None
        if template is None:
            raise TemplateUnavailableError("invitation has no project template")
        try:
            definition = TemplateDefinition.model_validate(template["definition"])
        except ValidationError as exc:
            raise TemplateUnavailableError(f"template {template['key']} has an invalid definition") from exc

        start = _today()
        onboarding.insert_submission(
            conn, invitation_id=ctx.invitation_id, client_id=ctx.client_id,
            answers=form.model_dump(mode="json"),
        )
        project = projects.insert_project(
            conn,
            client_id=ctx.client_id,
            invitation_id=ctx.invitation_id,
            template_id=template["id"],
            template_version=template["version"],
            name=form.project_name,
            start_date=start,
            due_date=start + timedelta(days=definition.default_duration_days),
        )
        project_id: UUID = project["id"]

        generated: list[tuple[UUID, str | None]] = []  # (task_id, assignee_role)
        position = 0
        for m_index, milestone in enumerate(definition.milestones):
            milestone_id = projects.insert_milestone(
                conn, project_id=project_id, key=milestone.key, name=milestone.name, position=m_index,
                due_date=start + timedelta(days=milestone.due_offset_days),
            )
            for task in milestone.tasks:
                task_id = projects.insert_task(
                    conn, project_id=project_id, milestone_id=milestone_id, key=task.key,
                    title=task.title, description=task.description, position=position,
                    due_date=start + timedelta(days=task.due_offset_days),
                    client_visible=task.client_visible,
                )
                generated.append((task_id, task.assignee_role))
                position += 1

        job_count = _queue_initial_jobs(conn, project_id, project["sync_version"], generated, max_attempts)

        activity.record(
            conn, client_id=ctx.client_id, project_id=project_id, actor_type="client",
            event_type="onboarding.submitted", summary="Onboarding form submitted", client_visible=True,
        )
        activity.record(
            conn, client_id=ctx.client_id, project_id=project_id, actor_type="system",
            event_type="project.created", summary=f"Project created: {form.project_name}", client_visible=True,
            data={"template_key": template["key"], "template_version": template["version"],
                  "milestones": len(definition.milestones), "tasks": len(generated)},
        )
        activity.record(
            conn, client_id=ctx.client_id, project_id=project_id, actor_type="system",
            event_type="integrations.jobs_queued", summary=f"{job_count} integration jobs queued",
            data={"clickup.create_task": len(generated), "sheets.upsert_project": 1, "slack.notify": 1},
        )
        onboarding.mark_invitation_submitted(conn, ctx.invitation_id)

    logger.info("onboarding submitted: project created with %d tasks, %d jobs queued", len(generated), job_count)
    return GenerationResult(project_id=project_id, milestone_count=len(definition.milestones),
                            task_count=len(generated), job_count=job_count)


def _queue_initial_jobs(
    conn: db.Connection, project_id: UUID, sync_version: int,
    generated: list[tuple[UUID, str | None]], max_attempts: int,
) -> int:
    def require(job_id: UUID | None, key: str) -> None:
        # Every key below is new in this transaction; a conflict means a bug.
        if job_id is None:
            raise RuntimeError(f"integration job unexpectedly already queued: {key.split(':')[0]}")

    for task_id, role in generated:
        key = f"clickup.create_task:{task_id}"
        require(jobs.enqueue(conn, job_type="clickup.create_task", idempotency_key=key, subject_type="task",
                             subject_id=task_id, max_attempts=max_attempts,
                             payload={"project_id": str(project_id), "assignee_role": role}), key)
    key = f"sheets.upsert_project:{project_id}:{sync_version}"
    require(jobs.enqueue(conn, job_type="sheets.upsert_project", idempotency_key=key,
                         coalesce_key=f"sheets.upsert_project:{project_id}", subject_type="project",
                         subject_id=project_id, max_attempts=max_attempts,
                         payload={"sync_version": sync_version}), key)
    key = f"slack.notify:project_created:{project_id}"
    require(jobs.enqueue(conn, job_type="slack.notify", idempotency_key=key, subject_type="project",
                         subject_id=project_id, max_attempts=max_attempts,
                         payload={"event": "project_created"}), key)
    return len(generated) + 2
