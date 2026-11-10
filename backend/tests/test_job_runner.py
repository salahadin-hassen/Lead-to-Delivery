"""Job claiming, leases, fencing, execution and result handling (PostgreSQL ltd_test)."""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from app import db
from app.jobs import runner
from app.jobs.registry import Handler, HandlerRegistry
from app.jobs.result import IntegrationResult, sanitize_message
from app.jobs.retry import backoff_seconds, classify_http_status
from tests.job_helpers import (
    Recorder,
    attempts,
    expire_lease,
    insert_job,
    job,
    make_due,
    registry_with,
)

LEASE = 120


@pytest.fixture
def pool(db_conn, migrated_database, pool_cleanup):
    db.configure_pool(migrated_database)
    yield


def run(registry, worker="w1", **kw):
    return runner.run_one(registry, worker_id=worker, lease_seconds=kw.pop("lease", LEASE), **kw)


def seconds_until_next_run(conn, job_id) -> float:
    return conn.execute("SELECT EXTRACT(EPOCH FROM next_run_at - now()) AS s FROM integration_jobs WHERE id = %s",
                        (job_id,)).fetchone()["s"]


# ------------------------------------------------------------- unit: policy
@pytest.mark.parametrize(("attempt", "delay"), [(1, 30), (2, 60), (3, 120), (4, 240), (5, 480), (6, 900), (7, 900), (60, 900)])
def test_backoff_formula(attempt, delay) -> None:
    assert backoff_seconds(attempt) == delay


def test_backoff_respects_retry_after_within_cap() -> None:
    assert backoff_seconds(1, retry_after=7) == 7
    assert backoff_seconds(1, retry_after=0) == 1
    assert backoff_seconds(1, retry_after=5000, max_delay=900) == 900
    assert backoff_seconds(3, max_delay=100) == 100


@pytest.mark.parametrize(("status", "outcome"), [(200, "success"), (201, "success"), (408, "retryable"),
                                                 (429, "retryable"), (500, "retryable"), (503, "retryable"),
                                                 (400, "permanent"), (401, "permanent"), (403, "permanent"),
                                                 (404, "permanent"), (422, "permanent")])
def test_http_status_classification(status, outcome) -> None:
    assert classify_http_status(status) == outcome


def test_error_messages_are_sanitised() -> None:
    raw = ("POST https://hooks.slack.com/services/T000/B000/XYZ failed: Authorization: Bearer abc.def "
           "token=pk_1234567890abcdefghijklmnopqrstuvwxyz client jane@example.com " + "x" * 500)
    clean = sanitize_message(raw)
    for leaked in ("hooks.slack.com", "abc.def", "pk_1234567890", "jane@example.com"):
        assert leaked not in clean
    assert len(clean) <= 300
    assert IntegrationResult.permanent("bad code-1!").error_code == "BAD_CODE_1"


# ---------------------------------------------------------------- claiming
def test_pending_due_job_is_claimed_and_attempt_recorded(pool, db_conn) -> None:
    jid = insert_job(db_conn)
    seen = {}

    def handler(ctx):
        row = db_conn.execute("SELECT status, attempts, locked_by, locked_until > now() AS leased "
                              "FROM integration_jobs WHERE id = %s", (ctx.job_id,)).fetchone()
        seen.update(row)
        seen["open_attempts"] = db_conn.execute(
            "SELECT count(*) AS n FROM integration_attempts WHERE job_id = %s AND outcome IS NULL",
            (ctx.job_id,)).fetchone()["n"]
        return IntegrationResult.success("ext-9")

    report = run(registry_with(execute=handler))
    assert (report.job_id, report.attempt_number, report.final_state) == (jid, 1, "succeeded")
    # While the handler ran, the claim was already committed and visible to others.
    assert seen == {"status": "running", "attempts": 1, "locked_by": "w1", "leased": True, "open_attempts": 1}


@pytest.mark.parametrize("kwargs", [
    {"run_in_seconds": 60},                         # future
    {"status": "succeeded"},
    {"status": "dead"},
    {"status": "cancelled"},
    {"attempts": 3, "max_attempts": 3},             # no attempts left
    {"status": "running"},                          # active lease held by another worker
])
def test_non_claimable_jobs_are_left_alone(pool, db_conn, kwargs) -> None:
    jid = insert_job(db_conn, **kwargs)
    before = job(db_conn, jid)
    handler = Recorder()
    assert run(registry_with(execute=handler)) is None
    assert handler.calls == []
    assert job(db_conn, jid) == before
    assert attempts(db_conn, jid) == []


def test_unregistered_job_types_are_never_touched(pool, db_conn) -> None:
    unhandled = [insert_job(db_conn, job_type=t) for t in ("clickup.create_task", "sheets.upsert_project")]
    handled = insert_job(db_conn, job_type="slack.notify")
    snapshot = {j: job(db_conn, j) for j in unhandled}
    assert run(registry_with("slack.notify")).job_id == handled
    assert run(registry_with("slack.notify")) is None
    assert {j: job(db_conn, j) for j in unhandled} == snapshot  # still pending, attempts 0, unchanged
    assert run(HandlerRegistry()) is None  # empty registry claims nothing at all


def test_two_workers_cannot_claim_the_same_job(pool, db_conn) -> None:
    jid = insert_job(db_conn)
    gate, release = threading.Event(), threading.Event()
    calls = []

    def slow(ctx):
        calls.append(ctx.worker_id)
        gate.set()
        release.wait(5)
        return IntegrationResult.success()

    registry = registry_with(execute=slow)
    with ThreadPoolExecutor(2) as pool_:
        first = pool_.submit(run, registry, "w1")
        assert gate.wait(5)
        second = pool_.submit(run, registry, "w2").result(timeout=10)  # job is running and leased
        release.set()
        assert first.result(timeout=10).final_state == "succeeded"
    assert second is None and calls == ["w1"]
    assert [a["attempt_number"] for a in attempts(db_conn, jid)] == [1]


def test_many_workers_process_each_job_exactly_once(pool, db_conn) -> None:
    ids = {insert_job(db_conn) for _ in range(30)}
    executed: list = []
    lock = threading.Lock()

    def handler(ctx):
        with lock:
            executed.append(ctx.job_id)
        time.sleep(0.01)
        return IntegrationResult.success()

    registry = registry_with(execute=handler)

    def drain(worker):
        while run(registry, worker) is not None:
            pass

    with ThreadPoolExecutor(4) as pool_:
        list(pool_.map(drain, ["w1", "w2", "w3", "w4"]))
    assert sorted(executed) == sorted(ids) and len(executed) == 30
    statuses = db_conn.execute("SELECT status, count(*) AS n FROM integration_jobs GROUP BY status").fetchall()
    assert statuses == [{"status": "succeeded", "n": 30}]
    assert db_conn.execute("SELECT count(*) AS n FROM integration_attempts").fetchone()["n"] == 30


# ------------------------------------------------------------------ leases
def test_expired_lease_is_recovered_and_old_attempt_marked_unknown(pool, db_conn) -> None:
    jid = insert_job(db_conn)
    claim = runner.claim_next(registry_with(), worker_id="crashed", lease_seconds=LEASE)  # worker "dies" here
    expire_lease(db_conn, jid)
    handler = Recorder()
    report = run(registry_with(execute=handler), "w2")
    assert (report.attempt_number, report.final_state) == (2, "succeeded")
    assert handler.calls[0].previous_outcome_unknown is True
    first, second = attempts(db_conn, jid)
    assert (first["outcome"], first["error_code"], first["worker_id"]) == ("unknown", "LEASE_EXPIRED", "crashed")
    assert first["finished_at"] is not None
    assert (second["outcome"], second["worker_id"]) == ("success", "w2")
    assert claim.context.attempt_number == 1


def test_expired_lease_on_final_attempt_becomes_dead(pool, db_conn) -> None:
    jid = insert_job(db_conn, max_attempts=1)
    runner.claim_next(registry_with(), worker_id="crashed", lease_seconds=LEASE)
    expire_lease(db_conn, jid)
    handler = Recorder()
    assert run(registry_with(execute=handler), "w2") is None
    assert handler.calls == []
    row = job(db_conn, jid)
    assert (row["status"], row["last_error_code"], row["locked_by"]) == ("dead", "LEASE_EXPIRED", None)
    assert row["completed_at"] is not None
    assert [a["outcome"] for a in attempts(db_conn, jid)] == ["unknown"]
    assert db_conn.execute("SELECT count(*) AS n FROM activity_events WHERE event_type = 'integration.job_dead'"
                           ).fetchone()["n"] == 1


def test_stale_worker_cannot_finalize_a_newer_attempt(pool, db_conn) -> None:
    jid = insert_job(db_conn)
    registry = registry_with()
    stale = runner.claim_next(registry, worker_id="w1", lease_seconds=LEASE)
    expire_lease(db_conn, jid)
    fresh = runner.claim_next(registry, worker_id="w2", lease_seconds=LEASE)
    assert (stale.context.attempt_number, fresh.context.attempt_number) == (1, 2)

    # The stale worker reports first: discarded, job untouched.
    assert runner.finalize(stale, IntegrationResult.permanent("STALE_RESULT")) == "lease_lost"
    row = job(db_conn, jid)
    assert (row["status"], row["locked_by"], row["attempts"]) == ("running", "w2", 2)
    # The current worker finalises normally; the stale one still cannot afterwards.
    assert runner.finalize(fresh, IntegrationResult.success("ext-2")) == "succeeded"
    assert runner.finalize(stale, IntegrationResult.success("ext-1")) == "lease_lost"
    a1, a2 = attempts(db_conn, jid)
    assert (a1["outcome"], a1["error_code"]) == ("unknown", "LEASE_EXPIRED")
    assert (a2["outcome"], a2["external_id"]) == ("success", "ext-2")
    assert job(db_conn, jid)["status"] == "succeeded"


def test_same_worker_reclaiming_is_still_fenced_by_attempt_number(pool, db_conn) -> None:
    jid = insert_job(db_conn)
    registry = registry_with()
    first = runner.claim_next(registry, worker_id="same", lease_seconds=LEASE)
    expire_lease(db_conn, jid)
    second = runner.claim_next(registry, worker_id="same", lease_seconds=LEASE)
    assert first.context.worker_id == second.context.worker_id == "same"
    assert runner.finalize(first, IntegrationResult.success("old")) == "lease_lost"
    assert runner.finalize(second, IntegrationResult.success("new")) == "succeeded"
    assert [a["external_id"] for a in attempts(db_conn, jid)] == [None, "new"]


# --------------------------------------------------------------- execution
def test_handler_runs_with_no_connection_held(pool, db_conn) -> None:
    insert_job(db_conn)
    observed = {}

    def handler(ctx):
        db.assert_no_connection_held()  # raises if this thread held a pooled connection
        stats = db._pool.get_stats()
        observed["in_use"] = stats.get("pool_size", 0) - stats.get("pool_available", 0)
        observed["held"] = db.connection_held()
        observed["open_txn"] = db_conn.execute(
            "SELECT count(*) AS n FROM pg_stat_activity WHERE application_name = %s "
            "AND state LIKE 'idle in transaction%%'", (db.APPLICATION_NAME,)).fetchone()["n"]
        return IntegrationResult.success()

    assert run(registry_with(execute=handler)).final_state == "succeeded"
    assert observed == {"in_use": 0, "held": False, "open_txn": 0}


def test_successful_job_is_finalised(pool, db_conn) -> None:
    jid = insert_job(db_conn)
    run(registry_with(execute=Recorder(IntegrationResult.success("ext-42", http_status=201))))
    row = job(db_conn, jid)
    assert (row["status"], row["attempts"], row["locked_by"], row["locked_until"]) == ("succeeded", 1, None, None)
    assert row["completed_at"] is not None and row["last_error_code"] is None
    (a,) = attempts(db_conn, jid)
    assert (a["outcome"], a["external_id"], a["http_status"], a["worker_id"]) == ("success", "ext-42", 201, "w1")
    assert a["finished_at"] is not None and a["duration_ms"] >= 0


def test_repeated_runs_do_not_duplicate_a_success(pool, db_conn) -> None:
    jid = insert_job(db_conn)
    handler = Recorder()
    registry = registry_with(execute=handler)
    assert run(registry).final_state == "succeeded"
    for _ in range(3):
        assert run(registry) is None
    assert len(handler.calls) == 1 and len(attempts(db_conn, jid)) == 1


def test_on_success_callback_commits_with_finalisation(pool, db_conn) -> None:
    project_id = insert_project(db_conn)
    jid = insert_job(db_conn, subject_type="project", subject_id=project_id)

    def on_success(conn, ctx, result):
        conn.execute("UPDATE projects SET name = %s WHERE id = %s", (f"synced {result.external_id}", ctx.subject_id))

    run(registry_with(on_success=on_success, execute=Recorder(IntegrationResult.success("X1"))))
    assert db_conn.execute("SELECT name FROM projects WHERE id = %s", (project_id,)).fetchone()["name"] == "synced X1"
    assert job(db_conn, jid)["status"] == "succeeded"


def test_failing_finalisation_never_marks_success(pool, db_conn) -> None:
    project_id = insert_project(db_conn)
    jid = insert_job(db_conn, subject_id=project_id)

    def on_success(conn, ctx, result):
        conn.execute("UPDATE projects SET name = 'should be rolled back' WHERE id = %s", (ctx.subject_id,))
        raise RuntimeError("callback failed")

    registry = registry_with(on_success=on_success)
    with pytest.raises(RuntimeError):
        run(registry)
    row = job(db_conn, jid)
    assert (row["status"], row["locked_by"]) == ("running", "w1")  # still leased, not succeeded
    assert [a["outcome"] for a in attempts(db_conn, jid)] == [None]
    assert db_conn.execute("SELECT name FROM projects WHERE id = %s", (project_id,)).fetchone()["name"] == "P"
    # Recovery after the lease expires: attempt 1 becomes unknown, the job is retried.
    expire_lease(db_conn, jid)
    handler = Recorder()
    assert run(registry_with(execute=handler)).final_state == "succeeded"
    assert handler.calls[0].previous_outcome_unknown is True
    assert [a["outcome"] for a in attempts(db_conn, jid)] == ["unknown", "success"]


def test_handler_exception_is_classified_unknown_without_leaking(pool, db_conn, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    jid = insert_job(db_conn)

    def boom(ctx):
        raise ValueError("token=SECRET123 https://hooks.slack.com/services/T1/B2/C3 jane@example.com")

    report = run(registry_with(execute=boom))
    assert (report.outcome, report.final_state) == ("unknown", "dead")
    row = job(db_conn, jid)
    assert row["last_error_code"] == "UNKNOWN_OUTCOME_REQUIRES_RECONCILIATION"
    (a,) = attempts(db_conn, jid)
    assert (a["outcome"], a["error_code"], a["error_message"]) == ("unknown", "HANDLER_EXCEPTION", "Handler raised ValueError.")
    for leaked in ("SECRET123", "hooks.slack.com", "jane@example.com"):
        assert leaked not in caplog.text and leaked not in str(row) and leaked not in str(a)


def test_invalid_payload_is_permanent_and_handler_not_called(pool, db_conn) -> None:
    from pydantic import BaseModel

    class Payload(BaseModel):
        project_id: str

    jid = insert_job(db_conn, payload={"wrong": 1})
    handler = Recorder()
    assert run(registry_with(execute=handler, payload_model=Payload)).final_state == "dead"
    assert handler.calls == [] and job(db_conn, jid)["last_error_code"] == "INVALID_JOB_PAYLOAD"


def test_permanent_failure_marks_dead(pool, db_conn) -> None:
    jid = insert_job(db_conn, max_attempts=5)
    handler = Recorder(IntegrationResult.permanent("NOT_CONFIGURED", "Integration credentials are not configured.",
                                                    http_status=None))
    assert run(registry_with(execute=handler)).final_state == "dead"
    row = job(db_conn, jid)
    assert (row["status"], row["attempts"], row["last_error_code"]) == ("dead", 1, "NOT_CONFIGURED")
    assert (row["locked_by"], row["locked_until"]) == (None, None) and row["completed_at"] is not None
    assert run(registry_with(execute=handler)) is None and len(handler.calls) == 1  # never retried automatically


def test_retryable_failure_schedules_exponential_backoff_then_dies(pool, db_conn) -> None:
    jid = insert_job(db_conn, max_attempts=3)
    handler = Recorder(IntegrationResult.retryable("UPSTREAM_503", "Service unavailable", http_status=503))
    registry = registry_with(execute=handler)
    expected = {1: 30, 2: 60}
    for attempt_number in (1, 2):
        report = run(registry)
        assert (report.attempt_number, report.final_state) == (attempt_number, "retry_scheduled")
        row = job(db_conn, jid)
        assert (row["status"], row["locked_by"], row["last_error_code"]) == ("pending", None, "UPSTREAM_503")
        assert expected[attempt_number] - 3 <= seconds_until_next_run(db_conn, jid) <= expected[attempt_number]
        assert run(registry) is None  # not due yet
        make_due(db_conn, jid)
    report = run(registry)
    assert (report.attempt_number, report.final_state) == (3, "dead")
    row = job(db_conn, jid)
    assert (row["status"], row["attempts"], row["last_error_code"], row["last_error"]) == (
        "dead", 3, "UPSTREAM_503", "Service unavailable")
    assert [a["outcome"] for a in attempts(db_conn, jid)] == ["retryable"] * 3
    assert [a["http_status"] for a in attempts(db_conn, jid)] == [503] * 3


def test_retry_after_is_respected_and_capped(pool, db_conn) -> None:
    short = insert_job(db_conn, key="a")
    run(registry_with(execute=Recorder(IntegrationResult.retryable("RATE_LIMITED", retry_after_seconds=7, http_status=429))))
    assert 4 <= seconds_until_next_run(db_conn, short) <= 7
    long_ = insert_job(db_conn, key="b")
    run(registry_with(execute=Recorder(IntegrationResult.retryable("RATE_LIMITED", retry_after_seconds=99999))),
        max_retry_delay=600)
    assert 597 <= seconds_until_next_run(db_conn, long_) <= 600


def test_unknown_outcome_is_not_retried_automatically(pool, db_conn) -> None:
    jid = insert_job(db_conn, max_attempts=5)
    handler = Recorder(IntegrationResult.unknown("RESPONSE_TIMEOUT", "Request sent; no response before timeout."))
    assert run(registry_with(execute=handler)).final_state == "dead"
    row = job(db_conn, jid)
    assert (row["status"], row["last_error_code"]) == ("dead", "UNKNOWN_OUTCOME_REQUIRES_RECONCILIATION")
    assert "RESPONSE_TIMEOUT" in row["last_error"]
    assert run(registry_with(execute=handler)) is None and len(handler.calls) == 1


def insert_project(conn):
    client_id = conn.execute("INSERT INTO clients (name) VALUES ('C') RETURNING id").fetchone()["id"]
    return conn.execute("INSERT INTO projects (client_id, name) VALUES (%s, 'P') RETURNING id",
                        (client_id,)).fetchone()["id"]


def test_lease_seconds_set_on_claim(pool, db_conn) -> None:
    jid = insert_job(db_conn)
    runner.claim_next(registry_with(), worker_id="w1", lease_seconds=45)
    remaining = db_conn.execute("SELECT locked_until - now() AS r FROM integration_jobs WHERE id = %s",
                                (jid,)).fetchone()["r"]
    assert timedelta(seconds=40) <= remaining <= timedelta(seconds=45)


# ------------------------------------------- coalesce-key serialization (Task 8)
def test_same_coalesce_key_never_runs_concurrently(pool, db_conn) -> None:
    v1 = insert_job(db_conn, key="sync:v1", coalesce_key="sync:p1")
    started, release = threading.Event(), threading.Event()

    def slow(ctx):
        started.set()
        release.wait(5)
        return IntegrationResult.success()

    registry = registry_with(execute=slow)
    with ThreadPoolExecutor(2) as pool_:
        first = pool_.submit(run, registry, "w1")
        assert started.wait(5)
        v2 = insert_job(db_conn, key="sync:v2", coalesce_key="sync:p1")  # queued while v1 runs
        assert run(registry_with(), "w2") is None  # v2 is due, but v1 holds the key
        assert job(db_conn, v2)["status"] == "pending" and attempts(db_conn, v2) == []
        release.set()
        assert first.result(timeout=10).job_id == v1
    report = run(registry_with(), "w2")  # now v2 may run, reading the latest state
    assert report.job_id == v2 and report.final_state == "succeeded"


def test_different_coalesce_keys_still_run_concurrently(pool, db_conn) -> None:
    insert_job(db_conn, key="a1", coalesce_key="sync:pA")
    insert_job(db_conn, key="b1", coalesce_key="sync:pB")
    barrier = threading.Barrier(2, timeout=5)

    def meet(ctx):
        barrier.wait()  # only passes if both jobs run at the same time
        return IntegrationResult.success()

    registry = registry_with(execute=meet)
    with ThreadPoolExecutor(2) as pool_:
        results = list(pool_.map(lambda w: run(registry, w), ["w1", "w2"]))
    assert sorted(r.final_state for r in results) == ["succeeded", "succeeded"]


def test_jobs_without_coalesce_key_are_unaffected(pool, db_conn) -> None:
    running = insert_job(db_conn, key="k1", status="running", coalesce_key=None)
    free = insert_job(db_conn, key="k2")
    assert run(registry_with()).job_id == free
    assert job(db_conn, running)["status"] == "running"


def test_expired_lease_is_recovered_before_next_version(pool, db_conn) -> None:
    v1 = insert_job(db_conn, key="e1", coalesce_key="sync:pE")
    runner.claim_next(registry_with(), worker_id="crashed", lease_seconds=LEASE)
    v2 = insert_job(db_conn, key="e2", coalesce_key="sync:pE")
    assert run(registry_with(), "w2") is None  # v1's lease is still valid: v2 blocked
    expire_lease(db_conn, v1)
    first = run(registry_with(), "w2")
    assert first.job_id == v1 and first.attempt_number == 2  # the expired v1 is recovered first
    second = run(registry_with(), "w2")
    assert second.job_id == v2 and second.final_state == "succeeded"
    assert [a["outcome"] for a in attempts(db_conn, v1)] == ["unknown", "success"]


def test_fencing_still_applies_with_coalesce_keys(pool, db_conn) -> None:
    v1 = insert_job(db_conn, key="f1", coalesce_key="sync:pF")
    stale = runner.claim_next(registry_with(), worker_id="w1", lease_seconds=LEASE)
    expire_lease(db_conn, v1)
    fresh = runner.claim_next(registry_with(), worker_id="w2", lease_seconds=LEASE)
    assert fresh.context.job_id == v1
    assert runner.finalize(stale, IntegrationResult.success()) == "lease_lost"
    assert runner.finalize(fresh, IntegrationResult.success()) == "succeeded"


def test_many_workers_serialise_each_key(pool, db_conn) -> None:
    """3 keys x 4 sequential versions, 4 workers: never two jobs of one key at once."""
    lock = threading.Lock()
    active: dict[str, int] = {}
    max_seen: dict[str, int] = {}
    keys = [f"sync:s{i}" for i in range(3)]
    for key in keys:
        insert_job(db_conn, key=f"{key}:1", coalesce_key=key)
    remaining = {key: 3 for key in keys}

    def handler(ctx):
        key = db_conn.execute("SELECT coalesce_key FROM integration_jobs WHERE id = %s", (ctx.job_id,)).fetchone()["coalesce_key"]
        with lock:
            active[key] = active.get(key, 0) + 1
            max_seen[key] = max(max_seen.get(key, 0), active[key])
            if remaining[key]:  # a new version arrives while this one runs
                remaining[key] -= 1
                insert_job(db_conn, key=f"{key}:{4 - remaining[key]}", coalesce_key=key)
        time.sleep(0.03)
        with lock:
            active[key] -= 1
        return IntegrationResult.success()

    registry = registry_with(execute=handler)

    def drain(worker):
        idle = 0
        while idle < 5:
            if run(registry, worker) is None:
                idle += 1
                time.sleep(0.02)
            else:
                idle = 0

    with ThreadPoolExecutor(4) as pool_:
        list(pool_.map(drain, ["w1", "w2", "w3", "w4"]))
    assert max_seen == {key: 1 for key in keys}
    done = db_conn.execute("SELECT coalesce_key, count(*) AS n FROM integration_jobs WHERE status = 'succeeded' "
                           "GROUP BY coalesce_key ORDER BY 1").fetchall()
    assert [(d["coalesce_key"], d["n"]) for d in done] == [(key, 4) for key in keys]
