"""Integration monitoring and the authenticated manual-retry endpoint."""

from datetime import date
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from app import db
from app.errors import (
    ConflictError,
    JobNotFoundError,
    JobNotResolvableError,
    JobNotRetryableError,
    NotFoundError,
    ReconciliationRequiredError,
)
from app.integrations.clickup import ClickUpAdapter
from app.integrations.sheets import SheetsAdapter
from app.integrations.slack import SlackAdapter
from app.routes.admin import Limit, Offset, ok, page
from app.routes.admin_deps import csrf_protect, require_admin
from app.schemas.admin import JobResolveDelivered, JobRetry
from app.services import job_admin
from app.services.admin_auth import AdminSession
from app.store import admin_queries as q

router = APIRouter(prefix="/admin", dependencies=[Depends(csrf_protect), Depends(require_admin)])

JobType = Literal["clickup.create_task", "sheets.upsert_project", "slack.notify"]
JobStatus = Literal["pending", "running", "succeeded", "dead", "cancelled"]


@router.get("/integrations/status")
def integrations_status(request: Request) -> JSONResponse:
    """Configuration and health per integration. Booleans and codes only; never
    a credential. 'Connected' is not claimed from environment variables alone."""
    settings = request.app.state.settings
    problem = ClickUpAdapter(settings).configuration_error()
    slack_problem = SlackAdapter(settings).configuration_error()
    sheets_adapter = SheetsAdapter(settings)
    try:
        sheets_problem = sheets_adapter.configuration_error()  # reads the key file locally; no network
    finally:
        sheets_adapter.close()
    with db.connection() as conn:
        counts = q.job_counts(conn)
        clickup_success = q.last_success(conn, "clickup.create_task")
        slack_success = q.last_success(conn, "slack.notify")
        sheets_success = q.last_success(conn, "sheets.upsert_project")

    def jobs_for(job_type: str) -> dict[str, int]:
        return {r["status"]: r["n"] for r in counts if r["job_type"] == job_type}

    return ok({
        "clickup": {
            "implemented": True,
            "credentials_present": settings.clickup_api_token is not None and bool(settings.clickup_list_id),
            "configuration_valid": problem is None,
            "configuration_error": problem.error_code if problem else None,
            # Only a job that succeeded in THIS database counts as evidence.
            "live_verification": "verified_by_successful_job" if clickup_success else "not_verified",
            "last_success_at": clickup_success,
            "jobs": jobs_for("clickup.create_task"),
            "two_way_sync": False,
        },
        "google_sheets": {
            "implemented": True,
            # Booleans and codes only: never the key file path, its contents or a token.
            "service_account_file_configured": settings.google_service_account_file is not None,
            "spreadsheet_id_configured": bool(settings.google_sheets_spreadsheet_id),
            "worksheet_configured": bool(settings.google_sheets_worksheet),
            "credentials_present": settings.google_service_account_file is not None
            and bool(settings.google_sheets_spreadsheet_id),
            "configuration_valid": sheets_problem is None,
            "configuration_error": sheets_problem,
            "live_verification": "verified_by_successful_job" if sheets_success else "not_verified",
            "last_success_at": sheets_success,
            "jobs": jobs_for("sheets.upsert_project"),
        },
        "slack": {
            "implemented": True,
            "credentials_present": settings.slack_webhook_url is not None,
            # Error code only: the webhook URL is a secret and is never returned.
            "configuration_valid": slack_problem is None,
            "configuration_error": slack_problem,
            # HTTP 200 "ok" proves Slack accepted a message, not that anyone saw it.
            "live_verification": "verified_by_successful_job" if slack_success else "not_verified",
            "last_success_at": slack_success,
            "jobs": jobs_for("slack.notify"),
        },
    })


@router.get("/integration-jobs")
def list_jobs(job_type: JobType | None = None, status: JobStatus | None = None, project_id: UUID | None = None,
              created_from: date | None = None, created_to: date | None = None,
              limit: int = Limit, offset: int = Offset) -> JSONResponse:
    with db.connection() as conn:
        items, total = q.list_jobs(conn, job_type=job_type, status=status, project_id=project_id,
                                   created_from=created_from, created_to=created_to, limit=limit, offset=offset)
    return page(items, total, limit, offset)


@router.get("/integration-jobs/{job_id}")
def get_job(job_id: UUID) -> JSONResponse:
    with db.connection() as conn:
        job = q.get_job(conn, job_id)
    if job is None:
        raise NotFoundError()
    return ok(job)


@router.post("/integration-jobs/{job_id}/retry")
def retry_job(job_id: UUID, body: JobRetry, session: AdminSession = Depends(require_admin)) -> JSONResponse:
    """Delegates to services.job_admin.retry_dead_job: dead jobs only, and an
    unknown outcome needs reconciliation_confirmed=true."""
    try:
        result = job_admin.retry_dead_job(job_id, additional_attempts=body.additional_attempts,
                                          reconciliation_confirmed=body.reconciliation_confirmed, actor=session.email)
    except JobNotFoundError as exc:
        raise NotFoundError() from exc
    except JobNotRetryableError as exc:
        if exc.status == "superseded":
            raise ConflictError("job_superseded", "A newer sync for this project is already queued; "
                                "it will send the latest data, so this job does not need a retry.") from exc
        raise ConflictError("job_not_retryable", f"Only dead jobs can be retried (this job is {exc.status}).") from exc
    except ReconciliationRequiredError as exc:
        raise ConflictError("reconciliation_required",
                            "The last attempt's outcome is unknown. Check the external system for the resource, "
                            "then confirm reconciliation to retry.") from exc
    with db.connection() as conn:
        job = q.get_job(conn, result.job_id)
    return ok(job)


@router.post("/integration-jobs/{job_id}/resolve-delivered")
def resolve_delivered(job_id: UUID, body: JobResolveDelivered,
                      session: AdminSession = Depends(require_admin)) -> JSONResponse:
    """Slack only: the administrator found the message in the channel after an
    unknown outcome. Marks the job succeeded without sending anything."""
    try:
        job_admin.resolve_slack_delivered(job_id, message_found_in_channel=body.message_found_in_channel,
                                          actor=session.email)
    except JobNotFoundError as exc:
        raise NotFoundError() from exc
    except JobNotResolvableError as exc:
        messages = {
            "wrong_job_type": "Only Slack notification jobs can be resolved as delivered.",
            "not_dead": "Only failed jobs can be resolved as delivered.",
            "outcome_not_unknown": "The last attempt did not have an unknown outcome; nothing to resolve.",
            "confirmation_required": "Confirm that the message is in the channel.",
        }
        raise ConflictError(f"job_not_resolvable_{exc.reason}", messages.get(exc.reason, "Not resolvable.")) from exc
    with db.connection() as conn:
        job = q.get_job(conn, job_id)
    return ok(job)
