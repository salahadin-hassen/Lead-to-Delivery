"""Google Sheets adapter for ``sheets.upsert_project`` jobs.

One row per project in a system-managed worksheet (default "Projects") whose
header row A1:M1 must be exactly HEADER. The project UUID in column A is the
row's identity; row numbers are never stored and are found again on every run.

Flow for one job:
1. Validate configuration: key file, spreadsheet ID, worksheet name
   (permanent errors; no request).
2. Short PostgreSQL read: the project snapshot (canonical, never the job
   payload) and the reconciliation state of this project's Sheets jobs.
3. Access token (cached, refreshed via google-auth when needed).
4. values:batchGet of A1:M1 and A2:A{limit+2}; validate the header;
   find every row whose column A equals the project ID. More rows than the
   limit means the scan is incomplete.
5. Exactly one match: PUT the full row (RAW). Several: permanent
   SHEETS_DUPLICATE_PROJECT_ROWS, nothing written. None: append (RAW,
   INSERT_ROWS), unless an earlier append of this project may have succeeded
   (an unresolved ``unknown`` attempt in any of the project's Sheets jobs) and
   no administrator has since confirmed the row is absent.

Appends are not idempotent: a lost append response is ``unknown``. Updates
of a located row are repeatable: their failures are retryable, and the row is
located again next time.
"""

import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from app import db
from app.config import Settings
from app.integrations.common import install_http_log_redaction, retry_after_from_headers
from app.integrations.google_auth import CredentialError, ServiceAccountTokenProvider
from app.jobs.registry import Handler, JobContext
from app.jobs.result import IntegrationResult
from app.jobs.retry import DEFAULT_MAX_DELAY_SECONDS
from app.schemas.portal import compute_progress
from app.store import activity

logger = logging.getLogger("app.integrations.sheets")
install_http_log_redaction()

JOB_TYPE = "sheets.upsert_project"
API_BASE = "https://sheets.googleapis.com/v4"
HEADER = ["project_id", "client_name", "company_name", "project_name", "template_name", "project_status",
          "progress_percent", "completed_tasks", "total_tasks", "start_date", "due_date", "clickup_url",
          "updated_at"]
DEFAULT_MAX_SCAN_ROWS = 10_000
_SPREADSHEET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,100}$")
_WORKSHEET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,99}$")
_UPDATED_RANGE_RE = re.compile(r"^'?(?P<sheet>.+?)'?!A(?P<start>\d+):M(?P<end>\d+)$")
QUOTA_REASONS = {"ratelimitexceeded", "userratelimitexceeded", "rate_limit_exceeded", "resource_exhausted",
                 "quotaexceeded"}


def a1(sheet: str, cells: str) -> str:
    return "'" + sheet.replace("'", "''") + "'!" + cells


# ------------------------------------------------------------- project data
@dataclass(frozen=True)
class Snapshot:
    values: list[Any]


def build_row(row: dict[str, Any]) -> list[Any]:
    """The 13 values, in HEADER order. No email, phone or onboarding answers."""
    progress = compute_progress(row["total_tasks"], row["completed_tasks"])  # same rule as portal/admin
    updated = row["updated_at"].astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if row["updated_at"] else ""
    return [
        str(row["id"]),
        row["client_name"] or "",
        row["company"] or "",
        row["name"],
        row["template_name"] or "",
        row["status"],
        progress.percent,
        progress.completed_tasks,
        progress.total_tasks,
        row["start_date"].isoformat() if row["start_date"] else "",
        row["due_date"].isoformat() if row["due_date"] else "",
        row["clickup_url"] or "",
        updated,
    ]


@dataclass(frozen=True)
class AppendGuard:
    unresolved_unknown: bool   # an earlier append for this project may have succeeded
    confirmed_absent: bool     # an administrator confirmed the row is absent since then


def load_state(ctx: JobContext) -> tuple[list[Any] | None, AppendGuard]:
    """One short read: the row snapshot and the project's append-safety state.

    The guard spans ALL Sheets jobs of the project (same coalesce_key): a newer
    sync version must not append while an older version's append is unresolved.
    """
    with db.connection() as conn:
        row = conn.execute(
            """
            SELECT p.id, p.name, p.status, p.start_date, p.due_date,
                   c.name AS client_name, c.company, pt.name AS template_name,
                   (SELECT count(*) FROM tasks t WHERE t.project_id = p.id) AS total_tasks,
                   (SELECT count(*) FROM tasks t WHERE t.project_id = p.id AND t.status = 'done') AS completed_tasks,
                   (SELECT t.clickup_url FROM tasks t WHERE t.project_id = p.id AND t.clickup_url IS NOT NULL
                     ORDER BY t.position, t.id LIMIT 1) AS clickup_url,
                   GREATEST(p.updated_at, (SELECT max(t.updated_at) FROM tasks t WHERE t.project_id = p.id)) AS updated_at
            FROM projects p JOIN clients c ON c.id = p.client_id
            LEFT JOIN project_templates pt ON pt.id = p.template_id
            WHERE p.id = %s
            """,
            (ctx.subject_id,),
        ).fetchone()
        guard = conn.execute(
            """
            WITH grp AS (
                SELECT j.id FROM integration_jobs j
                WHERE j.job_type = 'sheets.upsert_project'
                  AND j.coalesce_key = (SELECT coalesce_key FROM integration_jobs WHERE id = %(job)s)
            ),
            unknown AS (
                SELECT max(a.finished_at) AS at FROM integration_attempts a JOIN grp ON grp.id = a.job_id
                WHERE a.outcome = 'unknown' AND NOT (a.job_id = %(job)s AND a.attempt_number = %(attempt)s)
            ),
            success AS (
                SELECT max(a.finished_at) AS at FROM integration_attempts a JOIN grp ON grp.id = a.job_id
                WHERE a.outcome = 'success'
            ),
            confirmed AS (
                SELECT max((j.payload -> 'reconciliation' ->> 'confirmed_at')::timestamptz) AS at
                FROM integration_jobs j JOIN grp ON grp.id = j.id
            )
            SELECT unknown.at AS unknown_at, success.at AS success_at, confirmed.at AS confirmed_at
            FROM unknown, success, confirmed
            """,
            {"job": ctx.job_id, "attempt": ctx.attempt_number},
        ).fetchone()
    unknown_at, success_at, confirmed_at = guard["unknown_at"], guard["success_at"], guard["confirmed_at"]
    unresolved = unknown_at is not None and (success_at is None or success_at < unknown_at)
    confirmed = unresolved and confirmed_at is not None and confirmed_at >= unknown_at
    return (build_row(row) if row else None), AppendGuard(unresolved, confirmed)


# ---------------------------------------------------------------- adapter
class _Fail(Exception):
    def __init__(self, result: IntegrationResult) -> None:
        super().__init__(result.error_code)
        self.result = result


@dataclass(frozen=True)
class _Scan:
    rows: list[int]      # 1-based sheet row numbers whose column A equals the project ID
    complete: bool


class SheetsAdapter:
    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None,
                 token_provider: ServiceAccountTokenProvider | None = None, clock=time.time,
                 max_scan_rows: int = DEFAULT_MAX_SCAN_ROWS, spreadsheet_id: str | None = None,
                 worksheet: str | None = None) -> None:
        self._key_file: Path | None = settings.google_service_account_file
        self._spreadsheet = (spreadsheet_id or settings.google_sheets_spreadsheet_id or "").strip() or None
        self._worksheet = (worksheet or settings.google_sheets_worksheet or "").strip()
        self._timeout = settings.http_timeout_seconds
        self._max_delay = getattr(settings, "worker_max_retry_delay_seconds", DEFAULT_MAX_DELAY_SECONDS)
        self._clock = clock
        self._max_rows = max_scan_rows
        self._client = httpx.Client(
            timeout=httpx.Timeout(self._timeout, connect=min(5.0, self._timeout)),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            verify=True, follow_redirects=False, transport=transport,
        )
        self._tokens = token_provider or ServiceAccountTokenProvider(self._key_file, self._client, self._timeout)

    def handler(self) -> Handler:
        return Handler(JOB_TYPE, execute=self.execute, on_success=self.on_success, close=self.close)

    def close(self) -> None:
        self._client.close()

    # -- configuration
    def configuration_error(self) -> str | None:
        if not self._spreadsheet or not self._key_file:
            return "SHEETS_NOT_CONFIGURED"
        if not _SPREADSHEET_ID_RE.match(self._spreadsheet):
            return "SHEETS_INVALID_SPREADSHEET_ID"
        if not _WORKSHEET_RE.match(self._worksheet):
            return "SHEETS_INVALID_WORKSHEET_NAME"
        try:
            self._tokens.validate()
        except CredentialError as exc:
            return exc.code
        return None

    # -- HTTP
    def _request(self, method: str, path: str, *, params=None, json=None) -> httpx.Response:
        token = self._tokens.access_token()  # may refresh (guarded inside)
        db.assert_no_connection_held()
        return self._client.request(method, f"{API_BASE}/spreadsheets/{quote(self._spreadsheet, safe='')}{path}",
                                    params=params, json=json, headers={"Authorization": f"Bearer {token}"})

    def _google_error(self, response: httpx.Response) -> tuple[str, str]:
        """(reason-set lower-cased, message) from a Google error body; never stored."""
        try:
            err = response.json().get("error", {})
        except (ValueError, AttributeError):
            return "", ""
        reasons = {str(e.get("reason", "")).lower() for e in err.get("errors", []) if isinstance(e, dict)}
        reasons |= {str(d.get("reason", "")).lower() for d in err.get("details", []) if isinstance(d, dict)}
        reasons.add(str(err.get("status", "")).lower())
        return " ".join(sorted(r for r in reasons if r)), str(err.get("message", ""))

    def _classify(self, response: httpx.Response, *, operation: str) -> IntegrationResult:
        """operation: read | append | update."""
        status = response.status_code
        reasons, message = self._google_error(response)
        if status == 429 or (status == 403 and any(r in reasons.split() for r in QUOTA_REASONS)):
            return IntegrationResult.retryable(
                "SHEETS_RATE_LIMITED", "Google Sheets quota or rate limit reached.", http_status=status,
                retry_after_seconds=retry_after_from_headers(response.headers, self._clock(), self._max_delay))
        if status == 408 or status >= 500:
            if operation == "append":
                return IntegrationResult.unknown("SHEETS_APPEND_OUTCOME_UNKNOWN",
                                                 f"Google answered {status}; the row may have been added.",
                                                 http_status=status)
            return IntegrationResult.retryable(f"SHEETS_HTTP_{status}", f"Google answered {status}.", http_status=status)
        if status == 400 and message.startswith("Unable to parse range"):
            return IntegrationResult.permanent("SHEETS_WORKSHEET_NOT_FOUND",
                                               f"Worksheet '{self._worksheet}' does not exist.", http_status=status)
        codes = {400: ("SHEETS_BAD_REQUEST", "Google rejected the request."),
                 401: ("SHEETS_INVALID_CREDENTIALS", "Google rejected the access token."),
                 403: ("SHEETS_PERMISSION_DENIED", "The service account cannot access this spreadsheet; share it as Editor."),
                 404: ("SHEETS_NOT_FOUND", "The spreadsheet was not found.")}
        code, text = codes.get(status, (f"SHEETS_HTTP_{status}", f"Google rejected the request ({status})."))
        return IntegrationResult.permanent(code, text, http_status=status)

    def _call(self, method: str, path: str, *, operation: str, params=None, json=None) -> httpx.Response:
        try:
            response = self._request(method, path, params=params, json=json)
        except CredentialError as exc:
            result = (IntegrationResult.retryable(exc.code, exc.message) if exc.retryable
                      else IntegrationResult.permanent(exc.code, exc.message))
            raise _Fail(result) from None
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise _Fail(IntegrationResult.retryable("SHEETS_CONNECT_FAILED", f"{type(exc).__name__} before sending.")) from None
        except httpx.TransportError as exc:
            if operation == "append":
                raise _Fail(IntegrationResult.unknown("SHEETS_APPEND_OUTCOME_UNKNOWN",
                                                      f"{type(exc).__name__} after sending the append.")) from None
            raise _Fail(IntegrationResult.retryable("SHEETS_TRANSPORT_ERROR", f"{type(exc).__name__}.")) from None
        if not 200 <= response.status_code < 300:
            raise _Fail(self._classify(response, operation=operation))
        return response

    # -- steps
    def _scan(self, project_id: str) -> _Scan:
        last = self._max_rows + 2  # one probe row beyond the limit
        response = self._call("GET", "/values:batchGet", operation="read", params=[
            ("ranges", a1(self._worksheet, "A1:M1")), ("ranges", a1(self._worksheet, f"A2:A{last}")),
            ("majorDimension", "ROWS"), ("valueRenderOption", "UNFORMATTED_VALUE")])
        try:
            ranges = response.json()["valueRanges"]
            header = (ranges[0].get("values") or [[]])[0]
            ids = ranges[1].get("values") or []
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise _Fail(IntegrationResult.retryable("SHEETS_MALFORMED_RESPONSE", "Unexpected read response.")) from None
        if [str(h).strip() for h in header] != HEADER:
            raise _Fail(IntegrationResult.permanent(
                "SHEETS_SCHEMA_MISMATCH", f"Row 1 of '{self._worksheet}' must be exactly: {', '.join(HEADER)}."))
        rows = [i + 2 for i, cells in enumerate(ids)
                if isinstance(cells, list) and cells and str(cells[0]).strip() == project_id]
        return _Scan(rows=rows, complete=len(ids) <= self._max_rows)

    def _update(self, row: int, values: list[Any]) -> IntegrationResult:
        rng = a1(self._worksheet, f"A{row}:M{row}")
        response = self._call("PUT", f"/values/{quote(rng, safe='')}", operation="update",
                              params={"valueInputOption": "RAW"},
                              json={"range": rng, "majorDimension": "ROWS", "values": [values]})
        try:
            data = response.json()
            ok = data.get("updatedRows") == 1 and data.get("updatedColumns") == len(HEADER)
        except (ValueError, AttributeError):
            ok = False
        if not ok:  # a located-row update is repeatable: safe to retry
            return IntegrationResult.retryable("SHEETS_MALFORMED_RESPONSE", "Unexpected update response.",
                                               http_status=response.status_code)
        return IntegrationResult.success(None, http_status=response.status_code,
                                         result_data={"action": "updated", "row": row})

    def _append(self, values: list[Any]) -> IntegrationResult:
        rng = a1(self._worksheet, "A:M")
        response = self._call("POST", f"/values/{quote(rng, safe='')}:append", operation="append",
                              params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                              json={"majorDimension": "ROWS", "values": [values]})
        try:
            updates = response.json()["updates"]
            match = _UPDATED_RANGE_RE.match(str(updates["updatedRange"]))
            ok = (updates.get("updatedRows") == 1 and updates.get("updatedColumns") == len(HEADER)
                  and match is not None and match["start"] == match["end"])
        except (ValueError, KeyError, TypeError, AttributeError):
            ok, match = False, None
        if not ok:
            return IntegrationResult.unknown("SHEETS_APPEND_OUTCOME_UNKNOWN",
                                             "Append returned success without the expected confirmation.",
                                             http_status=response.status_code)
        return IntegrationResult.success(None, http_status=response.status_code,
                                         result_data={"action": "appended", "row": int(match["start"])})

    # -- execute
    def execute(self, ctx: JobContext) -> IntegrationResult:
        problem = self.configuration_error()
        if problem:
            return IntegrationResult.permanent(problem, {
                "SHEETS_NOT_CONFIGURED": "GOOGLE_SERVICE_ACCOUNT_FILE and GOOGLE_SHEETS_SPREADSHEET_ID must be set.",
                "SHEETS_INVALID_SPREADSHEET_ID": "GOOGLE_SHEETS_SPREADSHEET_ID is not a spreadsheet ID.",
                "SHEETS_INVALID_WORKSHEET_NAME": "GOOGLE_SHEETS_WORKSHEET has unsupported characters.",
            }.get(problem, "The service-account key file is not usable."))
        values, guard = load_state(ctx)
        if values is None:
            return IntegrationResult.permanent("SHEETS_PROJECT_MISSING", "The project no longer exists.")
        try:
            scan = self._scan(values[0])
            if len(scan.rows) > 1:
                return IntegrationResult.permanent(
                    "SHEETS_DUPLICATE_PROJECT_ROWS",
                    f"Project appears in {len(scan.rows)} rows; delete the extra rows, then retry.")
            if len(scan.rows) == 1:
                result = self._update(scan.rows[0], values)
                if result.outcome == "success" and guard.unresolved_unknown:
                    return IntegrationResult.success(None, http_status=result.http_status,
                                                     result_data={**result.result_data, "action": "adopted"})
                return result
            if not scan.complete:
                return IntegrationResult.permanent(
                    "SHEETS_SCAN_LIMIT_REACHED",
                    f"More than {self._max_rows} rows in '{self._worksheet}'; not appending without a full search.")
            if guard.unresolved_unknown and not guard.confirmed_absent:
                return IntegrationResult.unknown(
                    "SHEETS_RECONCILIATION_REQUIRED",
                    "An earlier append may have added this project; check the sheet and confirm the row is absent.")
            return self._append(values)
        except _Fail as fail:
            return fail.result

    def on_success(self, conn: db.Connection, ctx: JobContext, result: IntegrationResult) -> None:
        scope = conn.execute("SELECT id, client_id FROM projects WHERE id = %s", (ctx.subject_id,)).fetchone()
        if scope is None:
            return
        action = result.result_data.get("action", "updated")
        summary = {"appended": "Project row added to Google Sheets",
                   "adopted": "Existing Google Sheets row found and updated after an uncertain append",
                   }.get(action, "Project row updated in Google Sheets")
        activity.record(conn, client_id=scope["client_id"], project_id=scope["id"], actor_type="worker",
                        event_type="integration.sheets_synced", summary=summary,
                        data={"job_id": str(ctx.job_id), "action": action, "attempt": ctx.attempt_number,
                              "sync_version": ctx.payload.get("sync_version")})
