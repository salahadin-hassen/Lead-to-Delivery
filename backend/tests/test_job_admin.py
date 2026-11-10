"""Manual retry service for dead jobs."""

import uuid

import pytest

from app import db
from app.errors import JobNotFoundError, JobNotRetryableError, ReconciliationRequiredError
from app.jobs import runner
from app.jobs.result import IntegrationResult
from app.services.job_admin import retry_dead_job
from tests.job_helpers import Recorder, attempts, insert_job, job, registry_with


@pytest.fixture
def pool(db_conn, migrated_database, pool_cleanup):
    db.configure_pool(migrated_database)
    yield


def run(registry, worker="w1"):
    return runner.run_one(registry, worker_id=worker, lease_seconds=120)


def _dead_job(db_conn, result: IntegrationResult, max_attempts: int = 1):
    jid = insert_job(db_conn, max_attempts=max_attempts)
    assert run(registry_with(execute=Recorder(result))).final_state == "dead"
    return jid


def test_dead_job_is_requeued_with_history_intact(pool, db_conn) -> None:
    jid = _dead_job(db_conn, IntegrationResult.permanent("NOT_CONFIGURED", "Missing credentials"))
    history_before = attempts(db_conn, jid)

    result = retry_dead_job(jid, additional_attempts=3)
    assert (result.attempts_so_far, result.max_attempts) == (1, 4)
    row = job(db_conn, jid)
    assert (row["status"], row["attempts"], row["max_attempts"], row["completed_at"], row["locked_by"]) == (
        "pending", 1, 4, None, None)
    assert db_conn.execute("SELECT next_run_at <= now() AS due FROM integration_jobs WHERE id = %s",
                           (jid,)).fetchone()["due"]
    assert attempts(db_conn, jid) == history_before  # nothing rewritten

    handler = Recorder()
    report = run(registry_with(execute=handler))
    assert (report.attempt_number, report.final_state) == (2, "succeeded")
    assert [a["outcome"] for a in attempts(db_conn, jid)] == ["permanent", "success"]


def test_requeue_records_activity(pool, db_conn) -> None:
    jid = _dead_job(db_conn, IntegrationResult.permanent("BAD_LIST_ID"))
    retry_dead_job(jid, additional_attempts=2, actor="admin@agency")
    row = db_conn.execute("SELECT actor_type, data FROM activity_events WHERE event_type = 'integration.job_requeued'"
                          ).fetchone()
    assert row["actor_type"] == "admin"
    assert row["data"]["job_id"] == str(jid) and row["data"]["previous_error_code"] == "BAD_LIST_ID"
    assert row["data"]["requested_by"] == "admin@agency" and row["data"]["new_max_attempts"] == 3


@pytest.mark.parametrize("status", ["succeeded", "pending", "running", "cancelled"])
def test_only_dead_jobs_can_be_retried(pool, db_conn, status) -> None:
    jid = insert_job(db_conn, status=status)
    before = job(db_conn, jid)
    with pytest.raises(JobNotRetryableError) as excinfo:
        retry_dead_job(jid, additional_attempts=3)
    assert excinfo.value.status == status
    assert job(db_conn, jid) == before


def test_unknown_job_id(pool, db_conn) -> None:
    with pytest.raises(JobNotFoundError):
        retry_dead_job(uuid.uuid4(), additional_attempts=3)


def test_unknown_outcome_requires_reconciliation_acknowledgement(pool, db_conn) -> None:
    jid = _dead_job(db_conn, IntegrationResult.unknown("RESPONSE_TIMEOUT"), max_attempts=5)
    before = job(db_conn, jid)
    with pytest.raises(ReconciliationRequiredError):
        retry_dead_job(jid, additional_attempts=3)
    assert job(db_conn, jid) == before

    retry_dead_job(jid, additional_attempts=3, reconciliation_confirmed=True)
    handler = Recorder()
    run(registry_with(execute=handler))
    assert handler.calls[0].previous_outcome_unknown is True  # adapter is told to reconcile
    data = db_conn.execute("SELECT data FROM activity_events WHERE event_type = 'integration.job_requeued'").fetchone()["data"]
    assert data["reconciliation_confirmed"] is True


def test_lease_expired_dead_job_also_requires_reconciliation(pool, db_conn) -> None:
    jid = insert_job(db_conn, max_attempts=1)
    runner.claim_next(registry_with(), worker_id="crashed", lease_seconds=120)
    db_conn.execute("UPDATE integration_jobs SET locked_until = now() - interval '1 second' WHERE id = %s", (jid,))
    assert run(registry_with()) is None
    assert job(db_conn, jid)["status"] == "dead"
    with pytest.raises(ReconciliationRequiredError):
        retry_dead_job(jid, additional_attempts=1)


def test_additional_attempts_must_be_positive(pool, db_conn) -> None:
    jid = _dead_job(db_conn, IntegrationResult.permanent("X"))
    with pytest.raises(ValueError):
        retry_dead_job(jid, additional_attempts=0)
