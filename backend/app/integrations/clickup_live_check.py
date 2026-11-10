"""Controlled live check against a DEDICATED ClickUp test List.

    cd backend
    CLICKUP_LIVE_TEST=1 .venv/bin/python -m app.integrations.clickup_live_check

Requires CLICKUP_API_TOKEN and CLICKUP_LIST_ID (environment or .env) and runs
against TEST_DATABASE_URL, which must be ``ltd_test``. Refuses to run while
any other clickup.create_task job is pending there, so it can never create
ClickUp tasks for leftover test data.

Creates ONE synthetic task "[LTD live test] ..." in the List, then verifies:
1. creation succeeds and returns an id and URL;
2. PostgreSQL stores both on the task;
3. running the job again makes no further ClickUp request;
4. a reconciliation scan finds exactly that one task by its marker.
The created ClickUp task is left in the List for inspection; delete it by
hand. The token is never printed.
"""

import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import httpx

from app import db
from app.config import Settings
from app.integrations.clickup import ClickUpAdapter, load_local_state, marker
from app.jobs import runner
from app.jobs.registry import HandlerRegistry, JobContext

REQUIRED_DB = "ltd_test"


class LiveCheckError(RuntimeError):
    pass


def live_check(settings: Settings, *, transport: httpx.BaseTransport | None = None,
               out: Callable[[str], None] = print) -> dict:
    adapter = ClickUpAdapter(settings, transport=transport)
    problem = adapter.configuration_error()
    if problem is not None:
        raise LiveCheckError(f"configuration: {problem.error_code}")
    db.configure_pool(settings.database_url.get_secret_value())
    try:
        with db.transaction() as conn:
            name = conn.execute("SELECT current_database() AS d").fetchone()["d"]
            if name != REQUIRED_DB:
                raise LiveCheckError(f"refusing to run against database {name!r}; use {REQUIRED_DB}")
            others = conn.execute("SELECT count(*) AS n FROM integration_jobs WHERE job_type = 'clickup.create_task' "
                                  "AND status IN ('pending', 'running')").fetchone()["n"]
            if others:
                raise LiveCheckError(f"{others} other ClickUp job(s) pending in {REQUIRED_DB}; clear them first")
            stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
            client = conn.execute("INSERT INTO clients (name) VALUES ('LTD Live Test Client') RETURNING id").fetchone()["id"]
            project = conn.execute("INSERT INTO projects (client_id, name) VALUES (%s, %s) RETURNING id",
                                   (client, f"LTD live test {stamp}")).fetchone()["id"]
            task = conn.execute(
                "INSERT INTO tasks (project_id, title, description, due_date) VALUES (%s, %s, %s, %s) RETURNING id",
                (project, f"[LTD live test] synthetic task {stamp}", "Synthetic task created by the live check.",
                 date.today() + timedelta(days=7))).fetchone()["id"]
            job = conn.execute(
                "INSERT INTO integration_jobs (job_type, idempotency_key, subject_type, subject_id, payload, max_attempts) "
                "VALUES ('clickup.create_task', %s, 'task', %s, '{}'::jsonb, 1) RETURNING id",
                (f"clickup.create_task:{task}", task)).fetchone()["id"]
        out(f"synthetic task {task}, job {job}")

        registry = HandlerRegistry()
        registry.register(adapter.handler())
        report = runner.run_one(registry, worker_id="live-check", lease_seconds=settings.worker_lease_seconds)
        if report is None or report.final_state != "succeeded":
            with db.connection() as conn:
                row = conn.execute("SELECT status, last_error_code FROM integration_jobs WHERE id = %s", (job,)).fetchone()
            raise LiveCheckError(f"1. creation failed: {row['status']} {row['last_error_code']}")
        with db.connection() as conn:
            stored = conn.execute("SELECT clickup_task_id, clickup_url FROM tasks WHERE id = %s", (task,)).fetchone()
        if not stored["clickup_task_id"] or not stored["clickup_url"]:
            raise LiveCheckError("2. id/url not stored")
        out(f"1-2. created ClickUp task {stored['clickup_task_id']} {stored['clickup_url']} and stored it")

        if runner.run_one(registry, worker_id="live-check", lease_seconds=settings.worker_lease_seconds) is not None:
            raise LiveCheckError("3. job was executed again")
        out("3. re-running the worker made no further ClickUp request")

        ctx = JobContext(job_id=job, job_type="clickup.create_task", idempotency_key=f"clickup.create_task:{task}",
                         subject_type="task", subject_id=task, payload={}, attempt_number=2, max_attempts=2,
                         worker_id="live-check", lease_seconds=settings.worker_lease_seconds,
                         previous_outcome_unknown=True)
        local, _ = load_local_state(ctx)
        scan = adapter._scan(local, time.monotonic() + settings.worker_lease_seconds * 0.6)
        ids = [m[0] for m in scan.matches]
        if scan.failure is not None or not scan.complete or ids != [stored["clickup_task_id"]]:
            raise LiveCheckError(f"4. reconciliation found {ids} (complete={scan.complete})")
        out(f"4. reconciliation found exactly one task with {marker(task)}")
        return {"task_id": str(task), "clickup_task_id": stored["clickup_task_id"], "url": stored["clickup_url"]}
    finally:
        adapter.close()
        db.close_pool()


def main() -> int:
    if os.environ.get("CLICKUP_LIVE_TEST") != "1":
        print("Refusing: set CLICKUP_LIVE_TEST=1 to make real writes to the ClickUp test List.", file=sys.stderr)
        return 2
    from dotenv import dotenv_values

    from app.config import REPO_ROOT
    test_url = os.environ.get("TEST_DATABASE_URL") or dotenv_values(REPO_ROOT / ".env").get("TEST_DATABASE_URL")
    if not test_url:
        print("Refusing: TEST_DATABASE_URL is not set.", file=sys.stderr)
        return 2
    settings = Settings(database_url=test_url)
    try:
        live_check(settings)
    except LiveCheckError as exc:
        print(f"LIVE CHECK FAILED: {exc}", file=sys.stderr)
        return 1
    print("LIVE CHECK PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
