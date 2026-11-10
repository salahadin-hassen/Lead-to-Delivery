"""Helpers for worker tests: insert jobs directly, build registries of fake handlers."""

import uuid
from collections.abc import Callable
from typing import Any

from app.jobs.registry import Handler, HandlerRegistry, JobContext
from app.jobs.result import IntegrationResult


def insert_job(conn, *, job_type: str = "fake.op", key: str | None = None, status: str = "pending",
               attempts: int = 0, max_attempts: int = 3, run_in_seconds: int = 0,
               subject_type: str = "project", subject_id: uuid.UUID | None = None,
               payload: dict[str, Any] | None = None, coalesce_key: str | None = None) -> uuid.UUID:
    # coalesce_key is written in the same INSERT (as jobs.enqueue does): a job
    # must never be visible to workers without its key.
    from psycopg.types.json import Jsonb

    terminal = status in ("succeeded", "dead", "cancelled")
    running = status == "running"
    return conn.execute(
        """
        INSERT INTO integration_jobs (job_type, idempotency_key, subject_type, subject_id, payload, status,
            attempts, max_attempts, next_run_at, completed_at, locked_by, locked_until, coalesce_key)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now() + make_interval(secs => %s),
                CASE WHEN %s THEN now() END, CASE WHEN %s THEN 'other-worker' END,
                CASE WHEN %s THEN now() + interval '2 minutes' END, %s)
        RETURNING id
        """,
        (job_type, key or f"{job_type}:{uuid.uuid4()}", subject_type, subject_id or uuid.uuid4(),
         Jsonb(payload or {}), status, attempts, max_attempts, run_in_seconds, terminal, running, running,
         coalesce_key),
    ).fetchone()["id"]


def job(conn, job_id) -> dict[str, Any]:
    return conn.execute("SELECT * FROM integration_jobs WHERE id = %s", (job_id,)).fetchone()


def attempts(conn, job_id) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT * FROM integration_attempts WHERE job_id = %s ORDER BY attempt_number", (job_id,)).fetchall()


def expire_lease(conn, job_id) -> None:
    conn.execute("UPDATE integration_jobs SET locked_until = now() - interval '1 second' WHERE id = %s", (job_id,))


def make_due(conn, job_id) -> None:
    conn.execute("UPDATE integration_jobs SET next_run_at = now() WHERE id = %s", (job_id,))


def registry_with(*job_types: str, execute: Callable[[JobContext], IntegrationResult] | None = None,
                  on_success=None, payload_model=None) -> HandlerRegistry:
    registry = HandlerRegistry()
    for job_type in job_types or ("fake.op",):
        registry.register(Handler(job_type, execute=execute or (lambda ctx: IntegrationResult.success("ext-1")),
                                  on_success=on_success, payload_model=payload_model))
    return registry


class Recorder:
    """A fake handler that records calls and returns a scripted result."""

    def __init__(self, *results: IntegrationResult) -> None:
        self.results = list(results) or [IntegrationResult.success("ext-1")]
        self.calls: list[JobContext] = []

    def __call__(self, ctx: JobContext) -> IntegrationResult:
        self.calls.append(ctx)
        return self.results[min(len(self.calls) - 1, len(self.results) - 1)]
