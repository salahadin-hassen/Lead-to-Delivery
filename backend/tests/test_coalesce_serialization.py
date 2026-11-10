"""Mutual exclusion for jobs sharing a coalesce_key (Task 8.1).

Overlap is forced deterministically: runner._claim_pause (a no-op in
production) is replaced by a barrier, so every participating worker has
already selected and row-locked its candidate before any of them proceeds to
the coalesce-key lock and re-check.

Two *pending* jobs with one key cannot exist in production (partial unique
index integration_jobs_pending_coalesce_key). Tests that need that state drop
the index in ltd_test for their duration and restore it exactly afterwards,
proving the advisory-lock mechanism works on its own, not only because of the
index.
"""

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest

from app import db
from app.jobs import runner
from app.jobs.result import IntegrationResult
from app.services.job_admin import retry_dead_job
from app.errors import JobNotRetryableError
from app.store import jobs as jobs_store
from tests.job_helpers import attempts, expire_lease, insert_job, job, registry_with

BACKEND = Path(__file__).resolve().parents[1]
INDEX = "integration_jobs_pending_coalesce_key"


@pytest.fixture
def pool(db_conn, migrated_database, pool_cleanup):
    db.configure_pool(migrated_database)
    yield migrated_database


@pytest.fixture
def without_pending_index(db_conn):
    definition = db_conn.execute("SELECT indexdef FROM pg_indexes WHERE indexname = %s", (INDEX,)).fetchone()["indexdef"]
    db_conn.execute(f"DROP INDEX {INDEX}")
    try:
        yield
    finally:
        db_conn.execute("TRUNCATE integration_jobs CASCADE")
        db_conn.execute(definition)
        assert db_conn.execute("SELECT indexdef FROM pg_indexes WHERE indexname = %s", (INDEX,)).fetchone()["indexdef"] == definition


def keyed(conn, key, *, status="pending", payload_key=True):
    return insert_job(conn, key=f"{key}:{time.monotonic_ns()}", status=status,
                      payload={"key": key} if payload_key else None, coalesce_key=key)


def barrier_pause(monkeypatch, parties):
    """Each thread waits at the barrier on its FIRST candidate selection only."""
    barrier = threading.Barrier(parties, timeout=10)
    local = threading.local()

    def pause(_job):
        if not getattr(local, "done", False):
            local.done = True
            barrier.wait()
    monkeypatch.setattr(runner, "_claim_pause", pause)


def claim_concurrently(workers, monkeypatch=None):
    """Run claim_next in parallel; afterwards the pause is reset to a no-op so
    later single-threaded claims in the test do not wait at the barrier."""
    start = threading.Barrier(len(workers), timeout=10)

    def claim(worker):
        start.wait()  # all workers enter claim_next together
        return runner.claim_next(registry_with(), worker_id=worker, lease_seconds=120)

    with ThreadPoolExecutor(len(workers)) as pool_:
        results = [f.result(timeout=20) for f in [pool_.submit(claim, w) for w in workers]]
    if monkeypatch is not None:
        monkeypatch.setattr(runner, "_claim_pause", lambda job: None)
    return results


def running_count(conn, key=None):
    if key is None:
        return conn.execute("SELECT count(*) AS n FROM integration_jobs WHERE status = 'running'").fetchone()["n"]
    return conn.execute("SELECT count(*) AS n FROM integration_jobs WHERE status = 'running' AND coalesce_key = %s",
                        (key,)).fetchone()["n"]


# ------------------------------------------------------- simultaneous claims
def test_two_pending_same_key_simultaneous_claims_only_one_runs(pool, db_conn, without_pending_index, monkeypatch):
    a, b = keyed(db_conn, "k:same"), keyed(db_conn, "k:same")
    barrier_pause(monkeypatch, 2)
    claims = claim_concurrently(["w1", "w2"], monkeypatch)
    won = [c for c in claims if c is not None]
    assert len(won) == 1 and claims.count(None) == 1
    assert running_count(db_conn, "k:same") == 1
    loser = b if won[0].context.job_id == a else a
    assert (job(db_conn, loser)["status"], attempts(db_conn, loser)) == ("pending", [])
    # Not claimable while the winner runs…
    assert runner.claim_next(registry_with(), worker_id="w3", lease_seconds=120) is None
    # …and claimable as soon as it finishes.
    assert runner.finalize(won[0], IntegrationResult.success()) == "succeeded"
    second = runner.claim_next(registry_with(), worker_id="w3", lease_seconds=120)
    assert second is not None and second.context.job_id == loser


def test_negative_control_without_the_lock_both_run(pool, db_conn, without_pending_index, monkeypatch):
    """Same overlap with the lock + re-check disabled: the race is real and the test sees it."""
    keyed(db_conn, "k:race"), keyed(db_conn, "k:race")
    barrier_pause(monkeypatch, 2)
    monkeypatch.setattr(jobs_store, "lock_coalesce_key", lambda conn, key: None)
    monkeypatch.setattr(jobs_store, "other_running", lambda conn, key, job_id: False)
    claims = claim_concurrently(["w1", "w2"])
    assert all(c is not None for c in claims)
    assert running_count(db_conn, "k:race") == 2  # two executions of one key at once


def test_many_pending_same_key_many_workers(pool, db_conn, without_pending_index, monkeypatch):
    for _ in range(4):
        keyed(db_conn, "k:many")
    barrier_pause(monkeypatch, 4)
    claims = claim_concurrently(["w1", "w2", "w3", "w4"])
    assert sum(c is not None for c in claims) == 1 and running_count(db_conn, "k:many") == 1


def test_different_keys_run_concurrently(pool, db_conn, monkeypatch):
    keyed(db_conn, "k:A"), keyed(db_conn, "k:B")
    barrier_pause(monkeypatch, 2)
    claims = claim_concurrently(["w1", "w2"])
    assert all(c is not None for c in claims) and running_count(db_conn) == 2


def test_null_keys_keep_existing_concurrency(pool, db_conn, monkeypatch):
    insert_job(db_conn), insert_job(db_conn)
    barrier_pause(monkeypatch, 2)
    claims = claim_concurrently(["w1", "w2"])
    assert all(c is not None for c in claims) and running_count(db_conn) == 2


# ---------------------------------------------------------- lease recovery
def test_expired_running_plus_pending_newer_version(pool, db_conn, monkeypatch):
    v1 = keyed(db_conn, "k:exp")
    runner.claim_next(registry_with(), worker_id="crashed", lease_seconds=120)
    v2 = keyed(db_conn, "k:exp")  # newer version queued while v1 "runs"
    expire_lease(db_conn, v1)
    # No critical-section pause here: the pre-filter already hides v2 while v1
    # is running, so only one worker even finds a candidate. Both start together.
    claims = claim_concurrently(["w1", "w2"])
    won = [c for c in claims if c is not None]
    assert len(won) == 1 and won[0].context.job_id == v1  # recovery first
    assert won[0].context.attempt_number == 2 and won[0].context.previous_outcome_unknown
    assert job(db_conn, v2)["status"] == "pending" and attempts(db_conn, v2) == []
    runner.finalize(won[0], IntegrationResult.success())
    nxt = runner.claim_next(registry_with(), worker_id="w3", lease_seconds=120)
    assert nxt.context.job_id == v2


def test_crash_and_recovery_then_same_worker_fencing(pool, db_conn):
    v1 = keyed(db_conn, "k:fence")
    first = runner.claim_next(registry_with(), worker_id="same", lease_seconds=120)
    expire_lease(db_conn, v1)
    second = runner.claim_next(registry_with(), worker_id="same", lease_seconds=120)
    assert (first.context.attempt_number, second.context.attempt_number) == (1, 2)
    assert runner.finalize(first, IntegrationResult.success()) == "lease_lost"
    assert runner.finalize(second, IntegrationResult.success()) == "succeeded"
    assert [a["outcome"] for a in attempts(db_conn, v1)] == ["unknown", "success"]


# --------------------------------------------------- rollback and deadlocks
def test_rolled_back_claim_releases_the_key_lock(pool, db_conn, monkeypatch):
    v1 = keyed(db_conn, "k:rb")
    original = jobs_store.start_attempt
    calls = {"n": 0}

    def failing(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("crash after the advisory lock was taken")
        return original(*args, **kwargs)

    monkeypatch.setattr(jobs_store, "start_attempt", failing)
    with pytest.raises(RuntimeError):
        runner.claim_next(registry_with(), worker_id="w1", lease_seconds=120)
    assert (job(db_conn, v1)["status"], attempts(db_conn, v1)) == ("pending", [])  # whole claim rolled back
    with ThreadPoolExecutor(1) as pool_:  # another connection must not block on a leftover lock
        claim = pool_.submit(runner.claim_next, registry_with(), worker_id="w2", lease_seconds=120).result(timeout=5)
    assert claim.context.job_id == v1 and claim.context.attempt_number == 1
    held = db_conn.execute("SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory'").fetchone()["n"]
    assert held == 0  # transaction-scoped: nothing outlives the claim


def test_no_deadlocks_or_overlap_under_contention(pool, db_conn, without_pending_index):
    keys = [f"k:dl{i}" for i in range(4)]
    for key in keys:
        for _ in range(4):
            keyed(db_conn, key)
    for _ in range(4):
        insert_job(db_conn)
    lock = threading.Lock()
    active: dict = {}
    peak: dict = {}
    errors: list = []

    def handler(ctx):
        key = ctx.payload.get("key")
        with lock:
            active[key] = active.get(key, 0) + 1
            peak[key] = max(peak.get(key, 0), active[key])
        time.sleep(0.01)
        with lock:
            active[key] -= 1
        return IntegrationResult.success()

    registry = registry_with(execute=handler)

    def drain(worker):
        idle = 0
        while idle < 10:
            try:
                report = runner.run_one(registry, worker_id=worker, lease_seconds=60)
            except Exception as exc:  # noqa: BLE001
                errors.append(type(exc).__name__)
                return
            idle = idle + 1 if report is None else 0
            if report is None:
                time.sleep(0.005)

    with ThreadPoolExecutor(5) as pool_:
        list(pool_.map(drain, [f"w{i}" for i in range(5)]))
    assert errors == []  # no DeadlockDetected, no other failure
    assert all(peak[k] == 1 for k in keys)
    assert db_conn.execute("SELECT count(*) AS n FROM integration_jobs WHERE status = 'succeeded'").fetchone()["n"] == 20


# ---------------------------------------------------------- real processes
def test_independent_processes_never_overlap_on_a_key(db_conn, migrated_database, without_pending_index, tmp_path):
    for key in ("p:A", "p:B", "p:C"):
        for _ in range(3):
            keyed(db_conn, key)
    for _ in range(3):
        insert_job(db_conn)
    go = tmp_path / "go"
    env = {**os.environ, "PYTHONPATH": str(BACKEND)}
    procs = [subprocess.Popen([sys.executable, "-m", "tests.proc_worker", migrated_database,
                               str(tmp_path / f"out{i}.jsonl"), f"proc{i}", str(go)], cwd=BACKEND, env=env)
             for i in range(3)]
    time.sleep(1.0)  # let all processes import and connect
    go.touch()
    for p in procs:
        assert p.wait(timeout=60) == 0
    records = [json.loads(line) for i in range(3) for line in (tmp_path / f"out{i}.jsonl").read_text().splitlines()]
    assert len(records) == 12 and len({r["job"] for r in records}) == 12
    assert len({r["pid"] for r in records}) >= 2  # work really was spread over processes
    for key in ("p:A", "p:B", "p:C"):
        spans = sorted((r["start"], r["end"]) for r in records if r["key"] == key)
        assert all(spans[i][1] <= spans[i + 1][0] for i in range(len(spans) - 1)), key
    # monotonic_ns is per-process; overlap is checked with the database's own record too:
    rows = db_conn.execute(
        """SELECT j.coalesce_key, a.started_at, a.finished_at FROM integration_attempts a
           JOIN integration_jobs j ON j.id = a.job_id WHERE j.coalesce_key IS NOT NULL
           ORDER BY j.coalesce_key, a.started_at""").fetchall()
    for prev, cur in zip(rows, rows[1:]):
        if prev["coalesce_key"] == cur["coalesce_key"]:
            assert prev["finished_at"] <= cur["started_at"], prev["coalesce_key"]


# ------------------------------------------- superseded retryable failures
def test_retryable_failure_with_newer_pending_version_is_superseded(pool, db_conn):
    v1 = keyed(db_conn, "k:sup")
    claim = runner.claim_next(registry_with(), worker_id="w1", lease_seconds=120)
    v2 = keyed(db_conn, "k:sup")  # allowed: v1 is running, not pending
    state = runner.finalize(claim, IntegrationResult.retryable("UPSTREAM_503", http_status=503))
    assert state == "superseded"
    row = job(db_conn, v1)
    assert (row["status"], row["last_error_code"], row["locked_by"]) == ("cancelled", "SUPERSEDED", None)
    assert "UPSTREAM_503" in row["last_error"]
    assert attempts(db_conn, v1)[0]["outcome"] == "retryable"  # history kept
    assert job(db_conn, v2)["status"] == "pending"
    assert runner.claim_next(registry_with(), worker_id="w2", lease_seconds=120).context.job_id == v2


def test_retryable_failure_without_newer_version_still_reschedules(pool, db_conn):
    v1 = keyed(db_conn, "k:alone")
    claim = runner.claim_next(registry_with(), worker_id="w1", lease_seconds=120)
    assert runner.finalize(claim, IntegrationResult.retryable("UPSTREAM_503")) == "retry_scheduled"
    assert job(db_conn, v1)["status"] == "pending"


def test_requeue_of_dead_job_with_newer_pending_is_refused(pool, db_conn):
    v1 = keyed(db_conn, "k:dead")
    claim = runner.claim_next(registry_with(), worker_id="w1", lease_seconds=120)
    runner.finalize(claim, IntegrationResult.permanent("X"))
    keyed(db_conn, "k:dead")
    with pytest.raises(JobNotRetryableError) as excinfo:
        retry_dead_job(v1, additional_attempts=1)
    assert excinfo.value.status == "superseded"
    assert job(db_conn, v1)["status"] == "dead"


def test_requeue_race_with_concurrent_enqueue_is_handled(pool, db_conn, monkeypatch):
    """The pre-check passes, then a newer version appears before the UPDATE:
    the unique index stops it and the service reports 'superseded', not a 500."""
    v1 = keyed(db_conn, "k:race2")
    claim = runner.claim_next(registry_with(), worker_id="w1", lease_seconds=120)
    runner.finalize(claim, IntegrationResult.permanent("X"))
    original = jobs_store.requeue

    def enqueue_then_requeue(conn, job_id, **kwargs):
        with psycopg.connect(pool, autocommit=True) as other:
            other.execute("INSERT INTO integration_jobs (job_type, idempotency_key, coalesce_key, subject_type, subject_id) "
                          "VALUES ('fake.op', 'race2-new', 'k:race2', 'project', gen_random_uuid())")
        return original(conn, job_id, **kwargs)

    monkeypatch.setattr(jobs_store, "requeue", enqueue_then_requeue)
    with pytest.raises(JobNotRetryableError) as excinfo:
        retry_dead_job(v1, additional_attempts=1)
    assert excinfo.value.status == "superseded"
    assert job(db_conn, v1)["status"] == "dead"
