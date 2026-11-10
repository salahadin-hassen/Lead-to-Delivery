"""SQL for the integration_jobs outbox. Rows are written on the caller's
transaction, so they commit or roll back with the business change."""

from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db import Connection


def enqueue(
    conn: Connection,
    *,
    job_type: str,
    idempotency_key: str,
    subject_type: str,
    subject_id: UUID,
    max_attempts: int,
    payload: dict[str, Any] | None = None,
    coalesce_key: str | None = None,
) -> UUID | None:
    """Queue a pending job. Returns None when it is not needed:

    * the same idempotency_key was queued before (permanent key), or
    * a job with the same coalesce_key is still pending (it will read the
      current state when it runs, so the new change is covered).
    """
    row = conn.execute(
        """
        INSERT INTO integration_jobs (job_type, idempotency_key, coalesce_key, subject_type, subject_id,
                                      payload, max_attempts)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        (job_type, idempotency_key, coalesce_key, subject_type, subject_id, Jsonb(payload or {}), max_attempts),
    ).fetchone()
    return row["id"] if row else None


# ------------------------------------------------------------- worker side
_JOB_COLUMNS = (
    "id, job_type, idempotency_key, coalesce_key, subject_type, subject_id, payload, status, "
    "attempts, max_attempts, next_run_at, locked_by, locked_until, last_error_code, last_error, completed_at"
)


def lock_next_claimable(conn: Connection, job_types: list[str], exclude: list[UUID] | None = None) -> dict[str, Any] | None:
    """Lock one job a worker may take, skipping rows other workers hold.

    Claimable: a registered job type AND either
      * pending, due (next_run_at <= now) and attempts < max_attempts, or
      * running with an expired lease (its worker died or overran);
    AND, for a job with a coalesce_key, no OTHER job with the same key is
    running (Task 8). The block applies even to an expired lease, because
    that worker may still be executing; the expired job itself stays
    claimable, so it is recovered first and the next version runs after it.

    This NOT EXISTS is only a pre-filter (it sees committed state as of the
    statement start). Mutual exclusion is enforced by the caller
    (runner.claim_next): advisory lock on the key + a fresh re-check.
    Expired running jobs are ordered first so they are recovered before a
    newer version of the same key is considered.
    """
    return conn.execute(
        f"""
        SELECT {_JOB_COLUMNS}, (status = 'running') AS lease_expired
        FROM integration_jobs
        WHERE job_type = ANY(%s)
          AND NOT (id = ANY(%s::uuid[]))
          AND ((status = 'pending' AND next_run_at <= now() AND attempts < max_attempts)
               OR (status = 'running' AND locked_until < now()))
          AND (coalesce_key IS NULL OR NOT EXISTS (
                SELECT 1 FROM integration_jobs active
                WHERE active.coalesce_key = integration_jobs.coalesce_key
                  AND active.status = 'running'
                  AND active.id <> integration_jobs.id))
        ORDER BY (status = 'running') DESC,
                 CASE WHEN status = 'running' THEN locked_until ELSE next_run_at END, created_at, id
        LIMIT 1
        FOR UPDATE SKIP LOCKED
        """,
        (job_types, list(exclude or [])),
    ).fetchone()


def lock_coalesce_key(conn: Connection, coalesce_key: str) -> None:
    """Transaction-scoped advisory lock serialising claims of one coalesce key
    across all workers/processes. Released automatically at commit/rollback."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('ltd-coalesce:' || %s, 0))", (coalesce_key,))


def other_running(conn: Connection, coalesce_key: str, job_id: UUID) -> bool:
    """Fresh statement (new READ COMMITTED snapshot, taken after the advisory
    lock was granted): is another job with this key running?"""
    return conn.execute(
        "SELECT 1 FROM integration_jobs WHERE coalesce_key = %s AND status = 'running' AND id <> %s LIMIT 1",
        (coalesce_key, job_id),
    ).fetchone() is not None


def supersede(conn: Connection, job_id: UUID, *, error_code: str | None) -> None:
    """Close a coalesced job whose newer version is already pending."""
    conn.execute(
        """
        UPDATE integration_jobs
        SET status = 'cancelled', completed_at = now(), locked_by = NULL, locked_until = NULL,
            last_error_code = 'SUPERSEDED',
            last_error = 'A newer sync of the same data is queued and will send the latest state'
                         || CASE WHEN %s::text IS NULL THEN '' ELSE ' (this attempt: ' || %s || ')' END || '.'
        WHERE id = %s
        """,
        (error_code, error_code, job_id),
    )


def close_open_attempt(
    conn: Connection, job_id: UUID, attempt_number: int, *, outcome: str, error_code: str, error_message: str
) -> int:
    return conn.execute(
        """
        UPDATE integration_attempts
        SET outcome = %s, error_code = %s, error_message = %s, finished_at = now(),
            duration_ms = GREATEST(0, (EXTRACT(EPOCH FROM (now() - started_at)) * 1000)::int)
        WHERE job_id = %s AND attempt_number = %s AND outcome IS NULL
        """,
        (outcome, error_code, error_message, job_id, attempt_number),
    ).rowcount


def mark_dead(conn: Connection, job_id: UUID, *, error_code: str | None, error: str | None) -> None:
    conn.execute(
        """
        UPDATE integration_jobs
        SET status = 'dead', completed_at = now(), locked_by = NULL, locked_until = NULL,
            last_error_code = %s, last_error = %s
        WHERE id = %s
        """,
        (error_code, error, job_id),
    )


def start_attempt(conn: Connection, job_id: UUID, *, worker_id: str, lease_seconds: int) -> dict[str, Any]:
    """Take the lease and open the next attempt (atomic with the row lock)."""
    job = conn.execute(
        f"""
        UPDATE integration_jobs
        SET status = 'running', attempts = attempts + 1, locked_by = %s,
            locked_until = now() + make_interval(secs => %s)
        WHERE id = %s
        RETURNING {_JOB_COLUMNS}
        """,
        (worker_id, lease_seconds, job_id),
    ).fetchone()
    conn.execute(
        "INSERT INTO integration_attempts (job_id, attempt_number, worker_id) VALUES (%s, %s, %s)",
        (job_id, job["attempts"], worker_id),
    )
    return job


def previous_attempt_outcome(conn: Connection, job_id: UUID, before_attempt: int) -> str | None:
    row = conn.execute(
        "SELECT outcome FROM integration_attempts WHERE job_id = %s AND attempt_number < %s "
        "ORDER BY attempt_number DESC LIMIT 1",
        (job_id, before_attempt),
    ).fetchone()
    return row["outcome"] if row else None


def lock_fenced(conn: Connection, job_id: UUID, *, worker_id: str, attempt_number: int) -> dict[str, Any] | None:
    """The job, locked, only if THIS claim still owns it.

    Fence: same job, still running, same worker AND same attempt number. A
    worker whose lease expired and was reclaimed (by anyone, including
    itself) no longer matches, because the attempt number moved on.
    """
    return conn.execute(
        f"""
        SELECT {_JOB_COLUMNS} FROM integration_jobs
        WHERE id = %s AND status = 'running' AND locked_by = %s AND attempts = %s
        FOR UPDATE
        """,
        (job_id, worker_id, attempt_number),
    ).fetchone()


def finish_attempt(
    conn: Connection,
    job_id: UUID,
    attempt_number: int,
    *,
    outcome: str,
    http_status: int | None,
    error_code: str | None,
    error_message: str | None,
    external_id: str | None,
    duration_ms: int | None,
) -> int:
    return conn.execute(
        """
        UPDATE integration_attempts
        SET outcome = %s, finished_at = now(), http_status = %s, error_code = %s, error_message = %s,
            external_id = %s, duration_ms = %s
        WHERE job_id = %s AND attempt_number = %s AND outcome IS NULL
        """,
        (outcome, http_status, error_code, error_message, external_id, duration_ms, job_id, attempt_number),
    ).rowcount


def mark_succeeded(conn: Connection, job_id: UUID) -> None:
    conn.execute(
        """
        UPDATE integration_jobs
        SET status = 'succeeded', completed_at = now(), locked_by = NULL, locked_until = NULL,
            last_error_code = NULL, last_error = NULL
        WHERE id = %s
        """,
        (job_id,),
    )


def reschedule(conn: Connection, job_id: UUID, *, delay_seconds: int, error_code: str | None, error: str | None) -> None:
    conn.execute(
        """
        UPDATE integration_jobs
        SET status = 'pending', locked_by = NULL, locked_until = NULL,
            next_run_at = now() + make_interval(secs => %s), last_error_code = %s, last_error = %s
        WHERE id = %s
        """,
        (delay_seconds, error_code, error, job_id),
    )


# ------------------------------------------------------------ admin side
def lock_job(conn: Connection, job_id: UUID) -> dict[str, Any] | None:
    return conn.execute(f"SELECT {_JOB_COLUMNS} FROM integration_jobs WHERE id = %s FOR UPDATE", (job_id,)).fetchone()


def requeue(conn: Connection, job_id: UUID, *, additional_attempts: int) -> dict[str, Any]:
    """Dead -> pending with a fresh budget. Attempt numbers keep increasing
    (UNIQUE(job_id, attempt_number) and fencing rely on that), so the budget
    is granted by raising max_attempts rather than resetting attempts."""
    return conn.execute(
        f"""
        UPDATE integration_jobs
        SET status = 'pending', max_attempts = attempts + %s, next_run_at = now(), completed_at = NULL,
            locked_by = NULL, locked_until = NULL
        WHERE id = %s AND status = 'dead'
        RETURNING {_JOB_COLUMNS}
        """,
        (additional_attempts, job_id),
    ).fetchone()


def record_reconciliation_confirmation(conn: Connection, job_id: UUID, *, confirmed_by: str) -> None:
    """Durably mark that someone checked the external system for every attempt
    up to the current one. Stored in the job's own payload, so it survives
    worker restarts and is visible to the handler on the next attempt."""
    conn.execute(
        """
        UPDATE integration_jobs
        SET payload = payload || jsonb_build_object('reconciliation', jsonb_build_object(
                'confirmed_after_attempt', attempts,
                -- Microsecond precision: compared against attempt timestamps (Sheets).
                'confirmed_at', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                'confirmed_by', %s::text))
        WHERE id = %s
        """,
        (confirmed_by, job_id),
    )


def subject_scope(conn: Connection, subject_type: str, subject_id: UUID) -> dict[str, Any]:
    """client_id / project_id a job belongs to, for activity records."""
    if subject_type == "project":
        row = conn.execute("SELECT id AS project_id, client_id FROM projects WHERE id = %s", (subject_id,)).fetchone()
    elif subject_type == "task":
        row = conn.execute(
            "SELECT p.id AS project_id, p.client_id FROM tasks t JOIN projects p ON p.id = t.project_id WHERE t.id = %s",
            (subject_id,),
        ).fetchone()
    else:
        row = None
    return row or {"project_id": None, "client_id": None}
