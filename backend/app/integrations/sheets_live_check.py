"""Controlled live check against a DEDICATED test spreadsheet.

    cd backend
    GOOGLE_SHEETS_LIVE_TEST=1 .venv/bin/python -m app.integrations.sheets_live_check

Needs GOOGLE_SERVICE_ACCOUNT_FILE and GOOGLE_SHEETS_TEST_SPREADSHEET_ID (shared
with the service account as Editor, worksheet GOOGLE_SHEETS_TEST_WORKSHEET or
"Projects" with the exact header). Refuses to use the reporting spreadsheet
(GOOGLE_SHEETS_SPREADSHEET_ID), runs on ltd_test only, and refuses while other
Sheets jobs are pending there.

Steps: verify header; sync a synthetic project (append); sync again (update,
still one row); change the project and sync (row reflects the change); check
exactly one matching row; delete the synthetic row after re-locating and
re-checking it, then confirm it is gone. If cleanup cannot be verified, the
project ID and worksheet are printed for manual removal. Never prints
credentials or tokens.
"""

import os
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import httpx

from app import db
from app.config import Settings
from app.integrations.sheets import HEADER, SheetsAdapter, _Fail, a1
from app.jobs import runner
from app.jobs.registry import HandlerRegistry

REQUIRED_DB = "ltd_test"


class LiveCheckError(RuntimeError):
    pass


def _queue(conn: db.Connection, project, version: int) -> None:
    conn.execute(
        "INSERT INTO integration_jobs (job_type, idempotency_key, coalesce_key, subject_type, subject_id, payload, max_attempts) "
        "VALUES ('sheets.upsert_project', %s, %s, 'project', %s, jsonb_build_object('sync_version', %s::int), 1)",
        (f"sheets.upsert_project:{project}:{version}", f"sheets.upsert_project:{project}", project, version))


def live_check(settings: Settings, *, transport: httpx.BaseTransport | None = None,
               out: Callable[[str], None] = print) -> dict:
    test_id = (settings.google_sheets_test_spreadsheet_id or "").strip()
    if not test_id:
        raise LiveCheckError("GOOGLE_SHEETS_TEST_SPREADSHEET_ID is not set")
    if test_id == (settings.google_sheets_spreadsheet_id or "").strip():
        raise LiveCheckError("the test spreadsheet must not be the reporting spreadsheet")
    worksheet = settings.google_sheets_test_worksheet or "Projects"
    adapter = SheetsAdapter(settings, transport=transport, spreadsheet_id=test_id, worksheet=worksheet)
    problem = adapter.configuration_error()
    if problem:
        adapter.close()
        raise LiveCheckError(f"configuration: {problem}")
    db.configure_pool(settings.database_url.get_secret_value())
    project = None
    try:
        with db.transaction() as conn:
            if conn.execute("SELECT current_database() AS d").fetchone()["d"] != REQUIRED_DB:
                raise LiveCheckError(f"refusing to run outside {REQUIRED_DB}")
            if conn.execute("SELECT count(*) AS n FROM integration_jobs WHERE job_type = 'sheets.upsert_project' "
                            "AND status IN ('pending', 'running')").fetchone()["n"]:
                raise LiveCheckError(f"other Sheets jobs are pending in {REQUIRED_DB}; clear them first")
            stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
            client = conn.execute("INSERT INTO clients (name, company) VALUES ('LTD Live Test', 'LTD Sandbox Co') "
                                  "RETURNING id").fetchone()["id"]
            project = conn.execute(
                "INSERT INTO projects (client_id, name, start_date, due_date) VALUES (%s, %s, %s, %s) RETURNING id",
                (client, f"[LTD live test] {stamp}", date.today(), date.today() + timedelta(days=30))).fetchone()["id"]
            conn.execute("INSERT INTO tasks (project_id, title, position, status) VALUES (%s, 'A', 0, 'done'), "
                         "(%s, 'B', 1, 'todo')", (project, project))
            _queue(conn, project, 1)
        out(f"synthetic project {project} in worksheet '{worksheet}'")

        registry = HandlerRegistry()
        registry.register(adapter.handler())

        def sync(step: str) -> None:
            report = runner.run_one(registry, worker_id="live-check", lease_seconds=settings.worker_lease_seconds)
            if report is None or report.final_state != "succeeded":
                with db.connection() as conn:
                    code = conn.execute("SELECT last_error_code FROM integration_jobs WHERE subject_id = %s "
                                        "ORDER BY created_at DESC LIMIT 1", (project,)).fetchone()["last_error_code"]
                raise LiveCheckError(f"{step}: sync failed ({code})")

        def call(step: str, method: str, path: str, **kwargs):
            try:
                return adapter._call(method, path, **kwargs)
            except _Fail as fail:
                raise LiveCheckError(f"{step}: request failed ({fail.result.error_code})") from None

        def located() -> list[int]:
            try:
                return adapter._scan(str(project)).rows
            except _Fail as fail:
                raise LiveCheckError(f"scan failed ({fail.result.error_code})") from None

        sync("1-5 append")
        if len(located()) != 1:
            raise LiveCheckError("5. appended row not found exactly once")
        out("1-6. header valid; row appended and found once")

        with db.transaction() as conn:
            _queue(conn, project, 2)
        sync("7 re-sync")
        if len(located()) != 1:
            raise LiveCheckError("8. re-sync did not keep exactly one row")
        out("7-8. re-sync updated the same row (still one row)")

        with db.transaction() as conn:
            conn.execute("UPDATE projects SET status = 'completed' WHERE id = %s", (project,))
            conn.execute("UPDATE tasks SET status = 'done' WHERE project_id = %s", (project,))
            _queue(conn, project, 3)
        sync("9 change")
        rows = located()
        if len(rows) != 1:
            raise LiveCheckError("11. expected exactly one matching row")
        values = call("10 read row", "GET", "/values:batchGet", operation="read", params=[
            ("ranges", a1(worksheet, f"A{rows[0]}:M{rows[0]}")), ("valueRenderOption", "UNFORMATTED_VALUE")]
        ).json()["valueRanges"][0]["values"][0]
        row = dict(zip(HEADER, values))
        if row.get("project_status") != "completed" or int(row.get("progress_percent", -1)) != 100:
            raise LiveCheckError("10. row does not reflect the change")
        out("9-11. row reflects the change (completed, 100%); exactly one row")

        # 12. cleanup: re-locate, re-check, delete, verify.
        meta = call("12 metadata", "GET", "", operation="read", params={"fields": "sheets.properties(sheetId,title)"}).json()
        sheet_id = next((s["properties"]["sheetId"] for s in meta.get("sheets", [])
                         if s["properties"]["title"] == worksheet), None)
        rows = located()
        if sheet_id is None or len(rows) != 1:
            raise LiveCheckError("12. cleanup not attempted: row or worksheet could not be verified")
        call("12 delete row", "POST", ":batchUpdate", operation="update", json={"requests": [{"deleteDimension": {
            "range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": rows[0] - 1, "endIndex": rows[0]}}}]})
        if located():
            raise LiveCheckError("12. cleanup could not be verified")
        out("12. synthetic row deleted and verified gone")
        return {"project_id": str(project), "worksheet": worksheet}
    except LiveCheckError as exc:
        if project is not None:
            out(f"remove manually if present: project_id {project} in worksheet '{worksheet}'")
        raise exc
    finally:
        adapter.close()
        db.close_pool()


def main() -> int:
    if os.environ.get("GOOGLE_SHEETS_LIVE_TEST") != "1":
        print("Refusing: set GOOGLE_SHEETS_LIVE_TEST=1 to write to the test spreadsheet.", file=sys.stderr)
        return 2
    from dotenv import dotenv_values

    from app.config import REPO_ROOT
    test_url = os.environ.get("TEST_DATABASE_URL") or dotenv_values(REPO_ROOT / ".env").get("TEST_DATABASE_URL")
    if not test_url:
        print("Refusing: TEST_DATABASE_URL is not set.", file=sys.stderr)
        return 2
    try:
        live_check(Settings(database_url=test_url))
    except LiveCheckError as exc:
        print(f"LIVE CHECK FAILED: {exc}", file=sys.stderr)
        return 1
    print("LIVE CHECK PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
