"""The worker loop and process: startup, shutdown, and the full path from
project creation to processed jobs."""

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app import db
from app.jobs import worker
from app.jobs.registry import Handler, HandlerRegistry
from app.jobs.result import IntegrationResult
from tests.conftest import make_settings
from tests.crm_helpers import webhook_client
from tests.job_helpers import insert_job, job
from tests.portal_helpers import new_invitation, submit

BACKEND = Path(__file__).resolve().parents[1]


def test_run_loop_processes_jobs_and_closes_pool_on_stop(db_conn, migrated_database, pool_cleanup) -> None:
    ids = [insert_job(db_conn) for _ in range(3)]
    stop = threading.Event()
    registry = HandlerRegistry()
    registry.register(Handler("fake.op", execute=lambda ctx: IntegrationResult.success()))
    settings = make_settings(migrated_database, worker_poll_seconds=0.05)
    thread = threading.Thread(target=lambda: results.append(worker.run(settings, registry, stop, worker_id="loop")))
    results: list[int] = []
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(job(db_conn, j)["status"] != "succeeded" for j in ids):
        time.sleep(0.05)
    assert db.is_pool_open()
    stop.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert results == [3]
    assert not db.is_pool_open()  # pool closed on exit


def test_run_loop_survives_a_failing_iteration(db_conn, migrated_database, pool_cleanup) -> None:
    jid = insert_job(db_conn)
    stop = threading.Event()

    def on_success(conn, ctx, result):
        stop.set()  # stop after this iteration
        raise RuntimeError("finalisation failed")

    registry = HandlerRegistry()
    registry.register(Handler("fake.op", execute=lambda ctx: IntegrationResult.success(), on_success=on_success))
    worker.run(make_settings(migrated_database, worker_poll_seconds=0.05), registry, stop, worker_id="w")
    assert job(db_conn, jid)["status"] == "running"  # left for lease recovery, never marked succeeded
    assert not db.is_pool_open()


def test_worker_process_starts_and_stops_cleanly_on_sigterm(db_conn, migrated_database) -> None:
    """`python -m app.jobs.worker` with the production registry: starts, leaves
    job types without a handler (Sheets) untouched, exits 0 on SIGTERM.
    (Updated in Task 5: ClickUp registered; output read without blocking.
    Updated in Task 7: Slack registered too, so with no webhook configured its
    job fails permanently with SLACK_NOT_CONFIGURED.
    Updated in Task 8: Sheets registered too (SHEETS_NOT_CONFIGURED); the
    "no handler -> untouched" rule is checked with a type that has none.)"""
    slack = insert_job(db_conn, job_type="slack.notify")
    sheets = insert_job(db_conn, job_type="sheets.upsert_project")
    unhandled = insert_job(db_conn, job_type="fake.no_adapter")
    before = job(db_conn, unhandled)
    env = {**os.environ, "DATABASE_URL": migrated_database, "WORKER_POLL_SECONDS": "0.2", "PYTHONUNBUFFERED": "1",
           "CLICKUP_API_TOKEN": "", "CLICKUP_LIST_ID": "", "SLACK_WEBHOOK_URL": "",
           "GOOGLE_SERVICE_ACCOUNT_FILE": "", "GOOGLE_SHEETS_SPREADSHEET_ID": ""}
    proc = subprocess.Popen([sys.executable, "-m", "app.jobs.worker"], cwd=BACKEND, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        # Wait for the outcome instead of a fixed sleep: under load, start-up
        # alone (imports) took ~2 s, so a fixed 2 s budget was flaky (Task 9).
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and proc.poll() is None and any(
                job(db_conn, j)["status"] == "pending" for j in (slack, sheets)):
            time.sleep(0.1)
        time.sleep(0.5)  # a few more poll cycles: the unhandled job must still be untouched
        proc.send_signal(signal.SIGTERM)
        output, _ = proc.communicate(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)
    assert proc.returncode == 0, output
    assert "worker started" in output and "clickup.create_task" in output and "slack.notify" in output
    assert "signal SIGTERM received" in output and "worker stopped" in output
    assert job(db_conn, unhandled) == before  # type without a handler: untouched
    assert (job(db_conn, sheets)["status"], job(db_conn, sheets)["last_error_code"]) == ("dead", "SHEETS_NOT_CONFIGURED")
    assert (job(db_conn, slack)["status"], job(db_conn, slack)["last_error_code"]) == ("dead", "SLACK_NOT_CONFIGURED")
    assert "postgresql://" not in output and migrated_database not in output


def test_no_job_is_lost_between_project_creation_and_processing(db_conn, migrated_database, pool_cleanup) -> None:
    """Webhook -> onboarding -> 12 queued jobs -> worker with fake adapters for
    all three types -> every job succeeds exactly once; the ClickUp completion
    callback stores the external task id transactionally."""
    with webhook_client(migrated_database) as client:
        inv = new_invitation(client)
        assert submit(client, inv["token"]).status_code == 201
    assert db_conn.execute("SELECT count(*) AS n FROM integration_jobs WHERE status = 'pending'").fetchone()["n"] == 12

    calls: dict[str, int] = {}

    def fake(ctx):
        calls[ctx.idempotency_key] = calls.get(ctx.idempotency_key, 0) + 1
        return IntegrationResult.success(f"ext-{ctx.subject_id.hex[:8]}")

    def store_clickup_id(conn, ctx, result):
        conn.execute("UPDATE tasks SET clickup_task_id = %s WHERE id = %s AND clickup_task_id IS NULL",
                     (result.external_id, ctx.subject_id))

    registry = HandlerRegistry()
    registry.register(Handler("clickup.create_task", execute=fake, on_success=store_clickup_id))
    registry.register(Handler("sheets.upsert_project", execute=fake))
    registry.register(Handler("slack.notify", execute=fake))

    stop = threading.Event()
    settings = make_settings(migrated_database, worker_poll_seconds=0.05)
    # One worker process per pool: worker.run() owns the module-level pool.
    # (Concurrent workers sharing a database are covered in test_job_runner.)
    thread = threading.Thread(target=worker.run, args=(settings, registry, stop), kwargs={"worker_id": "e2e"})
    thread.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if db_conn.execute("SELECT count(*) AS n FROM integration_jobs WHERE status <> 'succeeded'").fetchone()["n"] == 0:
            break
        time.sleep(0.05)
    stop.set()
    thread.join(timeout=10)

    statuses = db_conn.execute("SELECT job_type, status, count(*) AS n FROM integration_jobs "
                               "GROUP BY 1, 2 ORDER BY 1").fetchall()
    assert statuses == [{"job_type": "clickup.create_task", "status": "succeeded", "n": 10},
                        {"job_type": "sheets.upsert_project", "status": "succeeded", "n": 1},
                        {"job_type": "slack.notify", "status": "succeeded", "n": 1}]
    assert len(calls) == 12 and set(calls.values()) == {1}
    assert db_conn.execute("SELECT count(*) AS n FROM integration_attempts WHERE outcome = 'success'").fetchone()["n"] == 12
    assert db_conn.execute("SELECT count(*) AS n FROM tasks WHERE clickup_task_id IS NOT NULL").fetchone()["n"] == 10
    # Integration processing never touched the client, submission or project.
    assert db_conn.execute("SELECT count(*) AS n FROM projects").fetchone()["n"] == 1
    assert db_conn.execute("SELECT status FROM onboarding_invitations").fetchone()["status"] == "submitted"


def test_failed_integration_never_undoes_the_project(db_conn, migrated_database, pool_cleanup) -> None:
    with webhook_client(migrated_database) as client:
        inv = new_invitation(client)
        submit(client, inv["token"])
    db.configure_pool(migrated_database)
    from app.jobs import runner

    registry = HandlerRegistry()
    registry.register(Handler("slack.notify", execute=lambda ctx: IntegrationResult.permanent("NOT_CONFIGURED")))
    assert runner.run_one(registry, worker_id="w", lease_seconds=120).final_state == "dead"
    counts = db_conn.execute("SELECT (SELECT count(*) FROM projects) AS p, (SELECT count(*) FROM tasks) AS t, "
                             "(SELECT count(*) FROM onboarding_submissions) AS s, (SELECT count(*) FROM clients) AS c"
                             ).fetchone()
    assert counts == {"p": 1, "t": 10, "s": 1, "c": 1}


@pytest.mark.parametrize("bad", [{"worker_lease_seconds": 5}, {"worker_max_retry_delay_seconds": 1},
                                 {"worker_id": "has spaces"}, {"http_timeout_seconds": 120, "worker_lease_seconds": 60}])
def test_worker_settings_are_validated(bad) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        make_settings("postgresql:///ltd_test", **bad)


def test_default_worker_id_is_unique_per_instance() -> None:
    a, b = worker.default_worker_id(), worker.default_worker_id()
    assert a != b and str(os.getpid()) in a
