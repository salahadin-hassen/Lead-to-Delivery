"""Administrative operations on integration jobs (no HTTP route yet; the admin
API will call these after authenticating the administrator)."""

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg

from app import db
from app.errors import JobNotFoundError, JobNotResolvableError, JobNotRetryableError, ReconciliationRequiredError
from app.store import activity, jobs

logger = logging.getLogger("app.jobs")


@dataclass(frozen=True)
class RequeueResult:
    job_id: UUID
    job_type: str
    attempts_so_far: int
    max_attempts: int


def retry_dead_job(job_id: UUID, *, additional_attempts: int, reconciliation_confirmed: bool = False,
                   actor: str = "admin") -> RequeueResult:
    """Move a dead job back to pending so the worker runs it again.

    * Only ``dead`` jobs: succeeded, pending, running or cancelled are refused.
    * If the latest attempt's outcome is ``unknown`` (or the job died with
      UNKNOWN_OUTCOME_REQUIRES_RECONCILIATION / LEASE_EXPIRED), the caller must
      pass ``reconciliation_confirmed=True``: someone has checked the external
      system, so re-running cannot create a duplicate.
    * Attempt history is kept. ``attempts`` keeps counting (attempt numbers are
      unique per job and used for fencing); the new budget is
      ``max_attempts = attempts + additional_attempts``.
    """
    if additional_attempts < 1:
        raise ValueError("additional_attempts must be at least 1")
    with db.transaction() as conn:
        job = jobs.lock_job(conn, job_id)
        if job is None:
            raise JobNotFoundError(str(job_id))
        if job["status"] != "dead" or job["locked_by"] is not None:
            raise JobNotRetryableError(job["status"])
        last = conn.execute(
            "SELECT outcome FROM integration_attempts WHERE job_id = %s ORDER BY attempt_number DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        unknown = (last is not None and last["outcome"] == "unknown") or job["last_error_code"] in (
            "UNKNOWN_OUTCOME_REQUIRES_RECONCILIATION", "LEASE_EXPIRED")
        if unknown and not reconciliation_confirmed:
            raise ReconciliationRequiredError(str(job_id))
        if unknown:
            # Durable, per-job acknowledgement the handler can read after any restart.
            jobs.record_reconciliation_confirmation(conn, job_id, confirmed_by=actor)
        if job["coalesce_key"] is not None and conn.execute(
                "SELECT 1 FROM integration_jobs WHERE coalesce_key = %s AND status = 'pending' AND id <> %s",
                (job["coalesce_key"], job_id)).fetchone():
            # A newer version of this sync is already queued; it will read the
            # current state, so this one must not be requeued (and the partial
            # unique index would refuse it anyway).
            raise JobNotRetryableError("superseded")
        try:
            with conn.transaction():  # savepoint: a newer version may be enqueued concurrently
                updated = jobs.requeue(conn, job_id, additional_attempts=additional_attempts)
        except psycopg.errors.UniqueViolation as exc:
            if exc.diag.constraint_name != "integration_jobs_pending_coalesce_key":
                raise
            raise JobNotRetryableError("superseded") from exc
        scope: dict[str, Any] = jobs.subject_scope(conn, job["subject_type"], job["subject_id"])
        activity.record(
            conn, client_id=scope["client_id"], project_id=scope["project_id"], actor_type="admin",
            event_type="integration.job_requeued",
            summary=f"Integration job {job['job_type']} manually requeued",
            data={"job_id": str(job_id), "job_type": job["job_type"], "previous_error_code": job["last_error_code"],
                  "attempts_so_far": job["attempts"], "new_max_attempts": updated["max_attempts"],
                  "reconciliation_confirmed": reconciliation_confirmed, "requested_by": actor},
        )
    logger.info("job requeued: job=%s type=%s attempts=%s new_max=%s reconciliation_confirmed=%s",
                job_id, job["job_type"], job["attempts"], updated["max_attempts"], reconciliation_confirmed)
    return RequeueResult(job_id=job_id, job_type=job["job_type"], attempts_so_far=job["attempts"],
                         max_attempts=updated["max_attempts"])


RESOLVABLE_AS_DELIVERED = {"slack.notify"}


def resolve_slack_delivered(job_id: UUID, *, message_found_in_channel: bool, actor: str = "admin") -> dict[str, Any]:
    """Close a dead slack.notify job whose last attempt had an unknown outcome
    because an administrator FOUND the message in the channel. No request is
    made to Slack. The unknown attempt stays in the history; the job becomes
    succeeded with payload.resolution recording who confirmed it and when.

    Only for Slack: ClickUp reconciles by itself (marker search) and its
    mapping must come from ClickUp; Sheets is idempotent and has no adapter.
    """
    if message_found_in_channel is not True:
        raise JobNotResolvableError("confirmation_required")
    with db.transaction() as conn:
        job = jobs.lock_job(conn, job_id)
        if job is None:
            raise JobNotFoundError(str(job_id))
        if job["job_type"] not in RESOLVABLE_AS_DELIVERED:
            raise JobNotResolvableError("wrong_job_type")
        if job["status"] != "dead" or job["locked_by"] is not None:
            raise JobNotResolvableError("not_dead")
        last = conn.execute(
            "SELECT attempt_number, outcome FROM integration_attempts WHERE job_id = %s "
            "ORDER BY attempt_number DESC LIMIT 1", (job_id,)).fetchone()
        if last is None or last["outcome"] != "unknown":
            raise JobNotResolvableError("outcome_not_unknown")
        conn.execute(
            """
            UPDATE integration_jobs
            SET status = 'succeeded', completed_at = now(), last_error_code = NULL, last_error = NULL,
                payload = payload || jsonb_build_object('resolution', jsonb_build_object(
                    'resolved_as', 'delivered_confirmed_by_admin',
                    'after_attempt', %s::int,
                    'resolved_at', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
                    'resolved_by', %s::text))
            WHERE id = %s
            """,
            (last["attempt_number"], actor, job_id),
        )
        scope = jobs.subject_scope(conn, job["subject_type"], job["subject_id"])
        activity.record(
            conn, client_id=scope["client_id"], project_id=scope["project_id"], actor_type="admin",
            event_type="integration.job_resolved_delivered",
            summary="Slack message confirmed in channel; job marked delivered without resending",
            data={"job_id": str(job_id), "job_type": job["job_type"], "after_attempt": last["attempt_number"],
                  "previous_error_code": job["last_error_code"], "resolved_by": actor},
        )
    logger.info("job resolved as delivered: job=%s type=%s after_attempt=%s", job_id, job["job_type"],
                last["attempt_number"])
    return {"job_id": job_id, "after_attempt": last["attempt_number"]}
