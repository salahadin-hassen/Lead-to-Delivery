"""Claim -> execute -> finalise, for one job at a time.

    1. claim (short transaction): lock one claimable job with
       FOR UPDATE SKIP LOCKED; if its lease expired, close the abandoned
       attempt as 'unknown' (LEASE_EXPIRED) and either kill the job (no attempts
       left) or reclaim it; set running / attempts+1 / locked_by /
       locked_until; insert the new integration_attempts row. COMMIT.
    2. execute: the handler runs with NO connection held and no transaction
       open (db.assert_no_connection_held() is enforced).
    3. finalise (short transaction): re-lock the job only if the fence still
       matches (status running, same worker, same attempt number); record the
       attempt result; run the handler's on_success callback; set the job's
       next state. COMMIT. A lost fence discards the result.

If finalisation itself fails, the transaction rolls back: the job stays
'running' with its lease and is recovered when the lease expires, with this
attempt recorded as 'unknown'. It is never marked succeeded by accident.

Delivery is at-least-once: a handler may run again after a crash or lease
expiry. Handlers must be idempotent and reconcile when
ctx.previous_outcome_unknown is true. Exactly-once is not claimed.
"""

import logging
import time

import psycopg
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from pydantic import ValidationError

from app import db
from app.jobs.registry import Handler, HandlerRegistry, JobContext
from app.jobs.result import IntegrationResult
from app.jobs.retry import DEFAULT_MAX_DELAY_SECONDS, backoff_seconds
from app.store import activity, jobs

logger = logging.getLogger("app.jobs")

UNKNOWN_OUTCOME_CODE = "UNKNOWN_OUTCOME_REQUIRES_RECONCILIATION"
LEASE_EXPIRED_CODE = "LEASE_EXPIRED"
MAX_STALE_REAPS_PER_CLAIM = 20
# Activity summaries are read by administrators: plain names, not job types.
JOB_LABELS = {"clickup.create_task": "ClickUp task", "sheets.upsert_project": "Google Sheets row",
              "slack.notify": "Slack message"}

FinalState = Literal["succeeded", "retry_scheduled", "dead", "lease_lost", "superseded"]


def _claim_pause(job: dict[str, Any]) -> None:
    """No-op in production. Tests replace it with a barrier to force two
    claim transactions to overlap after candidate selection."""


@dataclass(frozen=True)
class Claim:
    context: JobContext
    handler: Handler


@dataclass(frozen=True)
class RunReport:
    job_id: UUID
    job_type: str
    attempt_number: int
    outcome: str
    final_state: FinalState


def _activity_for_job(conn: db.Connection, job: dict[str, Any], event_type: str, summary: str,
                      data: dict[str, Any]) -> None:
    scope = jobs.subject_scope(conn, job["subject_type"], job["subject_id"])
    activity.record(
        conn, client_id=scope["client_id"], project_id=scope["project_id"], actor_type="worker",
        event_type=event_type, summary=summary,
        data={"job_id": str(job["id"]), "job_type": job["job_type"], **data},
    )


# ----------------------------------------------------------------- claim
def claim_next(registry: HandlerRegistry, *, worker_id: str, lease_seconds: int) -> Claim | None:
    job_types = registry.job_types()
    if not job_types:
        return None  # nothing can be executed; never touch unhandled jobs
    skipped: list[UUID] = []
    for _ in range(MAX_STALE_REAPS_PER_CLAIM):
        with db.transaction() as conn:
            job = jobs.lock_next_claimable(conn, job_types, skipped)
            if job is None:
                return None
            _claim_pause(job)  # test seam: deterministic overlap of concurrent claims
            if job["coalesce_key"] is not None:
                # Mutual exclusion per coalesce key, across processes: lock the
                # key for this transaction, then re-check in a NEW statement so
                # we see whatever the previous lock holder committed.
                jobs.lock_coalesce_key(conn, job["coalesce_key"])
                if jobs.other_running(conn, job["coalesce_key"], job["id"]):
                    skipped.append(job["id"])
                    continue  # this transaction changed nothing; try another job
            if job["lease_expired"]:
                jobs.close_open_attempt(
                    conn, job["id"], job["attempts"], outcome="unknown", error_code=LEASE_EXPIRED_CODE,
                    error_message="Worker lease expired before a result was recorded; outcome unknown.",
                )
                logger.warning("job lease expired: job=%s type=%s attempt=%s previous_worker=%s",
                               job["id"], job["job_type"], job["attempts"], job["locked_by"])
                if job["attempts"] >= job["max_attempts"]:
                    jobs.mark_dead(conn, job["id"], error_code=LEASE_EXPIRED_CODE,
                                   error="Lease expired on the final attempt; outcome unknown, reconcile before retrying.")
                    _activity_for_job(conn, job, "integration.job_dead",
                                      f"{JOB_LABELS.get(job['job_type'], job['job_type'])} failed: worker lease expired",
                                      {"error_code": LEASE_EXPIRED_CODE, "attempts": job["attempts"]})
                    continue  # committed; look for another job
            claimed = jobs.start_attempt(conn, job["id"], worker_id=worker_id, lease_seconds=lease_seconds)
            previous = jobs.previous_attempt_outcome(conn, job["id"], claimed["attempts"])
        context = JobContext(
            job_id=claimed["id"],
            job_type=claimed["job_type"],
            idempotency_key=claimed["idempotency_key"],
            subject_type=claimed["subject_type"],
            subject_id=claimed["subject_id"],
            payload=claimed["payload"],
            attempt_number=claimed["attempts"],
            max_attempts=claimed["max_attempts"],
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            previous_outcome_unknown=previous == "unknown",
        )
        logger.info("job claimed: job=%s type=%s attempt=%s/%s worker=%s",
                    context.job_id, context.job_type, context.attempt_number, context.max_attempts, worker_id)
        return Claim(context=context, handler=registry.get(context.job_type))  # type: ignore[arg-type]
    return None


# --------------------------------------------------------------- execute
def execute(claim: Claim) -> IntegrationResult:
    """Run the handler with no database connection held. Never raises."""
    db.assert_no_connection_held()
    handler, ctx = claim.handler, claim.context
    started = time.monotonic()
    if handler.payload_model is not None:
        try:
            handler.payload_model.model_validate(ctx.payload)
        except ValidationError:
            return IntegrationResult.permanent("INVALID_JOB_PAYLOAD", "Job payload failed validation.",
                                               duration_ms=0)
    try:
        result = handler.execute(ctx)
        if not isinstance(result, IntegrationResult):
            raise TypeError("handler did not return an IntegrationResult")
    except Exception as exc:  # noqa: BLE001 - classify, never crash the worker
        # The handler may have reached the external system before failing, so
        # the outcome is unknown. Only the exception class is kept: messages
        # can contain URLs, tokens or customer data.
        logger.error("job handler raised: job=%s type=%s attempt=%s error=%s",
                     ctx.job_id, ctx.job_type, ctx.attempt_number, type(exc).__name__)
        result = IntegrationResult.unknown("HANDLER_EXCEPTION", f"Handler raised {type(exc).__name__}.")
    duration_ms = int((time.monotonic() - started) * 1000)
    return IntegrationResult(
        outcome=result.outcome, error_code=result.error_code, error_message=result.error_message,
        http_status=result.http_status, external_id=result.external_id, duration_ms=duration_ms,
        retry_after_seconds=result.retry_after_seconds, result_data=result.result_data,
    )


# -------------------------------------------------------------- finalise
def finalize(claim: Claim, result: IntegrationResult, *,
             max_retry_delay: int = DEFAULT_MAX_DELAY_SECONDS) -> FinalState:
    ctx = claim.context
    with db.transaction() as conn:
        job = jobs.lock_fenced(conn, ctx.job_id, worker_id=ctx.worker_id, attempt_number=ctx.attempt_number)
        if job is None:
            logger.warning("job result discarded, lease lost: job=%s type=%s attempt=%s worker=%s outcome=%s",
                           ctx.job_id, ctx.job_type, ctx.attempt_number, ctx.worker_id, result.outcome)
            return "lease_lost"

        jobs.finish_attempt(
            conn, ctx.job_id, ctx.attempt_number, outcome=result.outcome, http_status=result.http_status,
            error_code=result.error_code, error_message=result.error_message,
            external_id=result.external_id, duration_ms=result.duration_ms,
        )
        state: FinalState
        if result.outcome == "success":
            if claim.handler.on_success is not None:
                claim.handler.on_success(conn, ctx, result)
            jobs.mark_succeeded(conn, ctx.job_id)
            state = "succeeded"
        elif result.outcome == "retryable" and job["attempts"] < job["max_attempts"]:
            delay = backoff_seconds(ctx.attempt_number, max_delay=max_retry_delay,
                                    retry_after=result.retry_after_seconds)
            try:
                with conn.transaction():  # savepoint: a newer version may already be pending
                    jobs.reschedule(conn, ctx.job_id, delay_seconds=delay, error_code=result.error_code,
                                    error=result.error_message)
                state = "retry_scheduled"
            except psycopg.errors.UniqueViolation as exc:
                if exc.diag.constraint_name != "integration_jobs_pending_coalesce_key":
                    raise
                # A newer job with the same coalesce_key is pending and will read
                # the current state; this one is no longer needed.
                jobs.supersede(conn, ctx.job_id, error_code=result.error_code)
                state = "superseded"
        else:
            if result.outcome == "unknown":
                code, error = UNKNOWN_OUTCOME_CODE, (
                    f"{result.error_code or 'UNKNOWN'}: outcome unknown; reconcile before retrying.")
            else:  # permanent, or retryable with no attempts left
                code, error = result.error_code, result.error_message
            jobs.mark_dead(conn, ctx.job_id, error_code=code, error=error)
            _activity_for_job(conn, job, "integration.job_dead",
                              f"{JOB_LABELS.get(ctx.job_type, ctx.job_type)} failed: {code or result.outcome}",
                              {"error_code": code, "result_error_code": result.error_code,
                               "attempts": ctx.attempt_number, "outcome": result.outcome})
            state = "dead"

    logger.info("job finished: job=%s type=%s attempt=%s worker=%s outcome=%s state=%s http_status=%s "
                "error_code=%s duration_ms=%s", ctx.job_id, ctx.job_type, ctx.attempt_number, ctx.worker_id,
                result.outcome, state, result.http_status, result.error_code, result.duration_ms)
    return state


def run_one(registry: HandlerRegistry, *, worker_id: str, lease_seconds: int,
            max_retry_delay: int = DEFAULT_MAX_DELAY_SECONDS) -> RunReport | None:
    """Claim, execute and finalise one job. None when nothing was claimable."""
    claim = claim_next(registry, worker_id=worker_id, lease_seconds=lease_seconds)
    if claim is None:
        return None
    result = execute(claim)
    state = finalize(claim, result, max_retry_delay=max_retry_delay)
    return RunReport(job_id=claim.context.job_id, job_type=claim.context.job_type,
                     attempt_number=claim.context.attempt_number, outcome=result.outcome, final_state=state)
