"""ClickUp adapter for ``clickup.create_task`` jobs.

Flow for one job (runs inside the worker, see app/jobs/runner.py):

1. Validate configuration (token, numeric List ID, time zone). Invalid ->
   permanent ``CLICKUP_NOT_CONFIGURED`` / ``CLICKUP_INVALID_LIST_ID`` /
   ``CLICKUP_INVALID_TIMEZONE``; only this job is affected.
2. Read the local task (canonical source) and the job's attempt history in one
   short read; release the connection.
3. Local task already mapped -> success with that mapping, no HTTP request.
4. Any earlier attempt of this job ended ``unknown`` -> reconcile first: scan
   the List (paged, bounded) for the task's deterministic marker
   ``[LTD-TASK:<uuid>]``.
     exactly one match            -> adopt it (success, no POST)
     several matches              -> unknown CLICKUP_DUPLICATE_MARKER (never pick one)
     scan incomplete              -> unknown CLICKUP_RECONCILIATION_INCOMPLETE
     none, not confirmed          -> unknown CLICKUP_RECONCILIATION_REQUIRED
     none, confirmed by an admin  -> create
   "Confirmed" = payload.reconciliation.confirmed_after_attempt (written by
   services.job_admin.retry_dead_job) covers the latest unknown attempt.
5. Create: POST /list/{list_id}/task. Failures before the request could reach
   ClickUp are retryable; anything after dispatch without a definitive answer
   (read/write timeout, protocol error, 408, 5xx, unparsable 2xx) is unknown.
6. On success the worker's fenced finalisation calls ``on_success``, which
   stores clickup_task_id / clickup_url on the task in the same transaction.

Delivery is at-least-once; the marker makes duplicates detectable, not
impossible (see docs/integrations.md).
"""

import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from app import db
from app.config import Settings
from app.jobs.registry import Handler, JobContext
from app.jobs.result import IntegrationResult
from app.jobs.retry import DEFAULT_MAX_DELAY_SECONDS
from app.integrations.common import retry_after_from_headers  # noqa: F401 (re-exported)
from app.store import activity

logger = logging.getLogger("app.integrations.clickup")

API_BASE = "https://api.clickup.com/api/v2"
JOB_TYPE = "clickup.create_task"
PAGE_SIZE = 100                 # ClickUp returns at most 100 tasks per page
DEFAULT_MAX_SCAN_PAGES = 10     # bounded reconciliation: up to 1000 recent tasks
CREATED_AFTER_MARGIN = timedelta(minutes=10)
MAX_TITLE_CHARS = 150
_LIST_ID_RE = re.compile(r"^[0-9]{1,20}$")
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ECODE_RE = re.compile(r"^[A-Z0-9_]{1,32}$")
_TOKEN_RE = re.compile(r"^[\x21-\x7e]{8,512}$")  # printable, no whitespace: safe as a header value


# ----------------------------------------------------------- pure helpers
def marker(task_id: UUID | str) -> str:
    return f"[LTD-TASK:{task_id}]"


def task_name(title: str, task_id: UUID | str) -> str:
    """Readable title + marker. The title is truncated, never the marker."""
    title = " ".join(title.split())
    if len(title) > MAX_TITLE_CHARS:
        title = title[: MAX_TITLE_CHARS - 1].rstrip() + "…"
    return f"{title} {marker(task_id)}"


def due_date_ms(due: date | None, tz_name: str) -> int | None:
    """Unix milliseconds for a date-only deadline: 12:00 on that date in the
    agency's time zone. Noon keeps the same calendar date for anyone viewing
    within ±11 hours of that zone (ClickUp renders timestamps in the viewer's
    zone), and is never affected by DST transitions (which happen at night)."""
    if due is None:
        return None
    local_noon = datetime(due.year, due.month, due.day, 12, 0, tzinfo=ZoneInfo(tz_name))
    return int(local_noon.timestamp()) * 1000


@dataclass(frozen=True)
class LocalTask:
    id: UUID
    title: str
    description: str | None
    due_date: date | None
    created_at: datetime
    clickup_task_id: str | None
    clickup_url: str | None
    project_name: str
    milestone_name: str | None


def task_description(task: LocalTask, assignee_role: str | None) -> str:
    """Delivery context only: no client contact details, answers or tokens."""
    lines = [f"Project: {task.project_name}"]
    if task.milestone_name:
        lines.append(f"Milestone: {task.milestone_name}")
    if task.due_date:
        lines.append(f"Due: {task.due_date.isoformat()}")
    if assignee_role:
        lines.append(f"Suggested role: {assignee_role.replace('_', ' ')} (unassigned)")
    if task.description:
        lines += ["", task.description.strip()]
    lines += ["", "Created by Lead-to-Delivery Automation Hub. Keep the marker below; it prevents duplicates.",
              marker(task.id)]
    return "\n".join(lines)


# ---------------------------------------------------------- local state
@dataclass(frozen=True)
class Reconciliation:
    last_unknown_attempt: int | None
    confirmed_after_attempt: int | None

    @property
    def required(self) -> bool:
        return self.last_unknown_attempt is not None

    @property
    def confirmed(self) -> bool:
        return (self.last_unknown_attempt is not None and self.confirmed_after_attempt is not None
                and self.confirmed_after_attempt >= self.last_unknown_attempt)


def load_local_state(ctx: JobContext) -> tuple[LocalTask | None, Reconciliation]:
    """One short read; the connection is released before any HTTP call."""
    with db.connection() as conn:
        row = conn.execute(
            """
            SELECT t.id, t.title, t.description, t.due_date, t.created_at, t.clickup_task_id, t.clickup_url,
                   p.name AS project_name, m.name AS milestone_name
            FROM tasks t
            JOIN projects p ON p.id = t.project_id
            LEFT JOIN milestones m ON m.id = t.milestone_id
            WHERE t.id = %s
            """,
            (ctx.subject_id,),
        ).fetchone()
        unknown = conn.execute(
            "SELECT max(attempt_number) AS n FROM integration_attempts "
            "WHERE job_id = %s AND outcome = 'unknown' AND attempt_number < %s",
            (ctx.job_id, ctx.attempt_number),
        ).fetchone()["n"]
    confirmed = (ctx.payload.get("reconciliation") or {}).get("confirmed_after_attempt")
    recon = Reconciliation(last_unknown_attempt=unknown,
                           confirmed_after_attempt=confirmed if isinstance(confirmed, int) else None)
    return (LocalTask(**row) if row else None), recon


# ---------------------------------------------------------------- adapter
@dataclass(frozen=True)
class _Scan:
    matches: list[tuple[str, str | None]]
    complete: bool
    failure: IntegrationResult | None = None


class ClickUpAdapter:
    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None,
                 clock=time.time, max_scan_pages: int = DEFAULT_MAX_SCAN_PAGES) -> None:
        self._token = settings.clickup_api_token.get_secret_value() if settings.clickup_api_token else None
        self._list_id = (settings.clickup_list_id or "").strip() or None
        self._tz = settings.clickup_timezone
        self._timeout = settings.http_timeout_seconds
        self._max_delay = getattr(settings, "worker_max_retry_delay_seconds", DEFAULT_MAX_DELAY_SECONDS)
        self._transport = transport
        self._clock = clock
        self._max_scan_pages = max_scan_pages
        self._client: httpx.Client | None = None

    # -- registration
    def handler(self) -> Handler:
        return Handler(JOB_TYPE, execute=self.execute, on_success=self.on_success, close=self.close)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # -- configuration
    def configuration_error(self) -> IntegrationResult | None:
        if not self._token or not self._list_id:
            return IntegrationResult.permanent("CLICKUP_NOT_CONFIGURED",
                                               "CLICKUP_API_TOKEN and CLICKUP_LIST_ID must both be set.")
        if not _TOKEN_RE.match(self._token):
            return IntegrationResult.permanent("CLICKUP_INVALID_TOKEN_FORMAT",
                                               "CLICKUP_API_TOKEN contains invalid characters.")
        if not _LIST_ID_RE.match(self._list_id):
            return IntegrationResult.permanent("CLICKUP_INVALID_LIST_ID", "CLICKUP_LIST_ID must be numeric.")
        try:
            ZoneInfo(self._tz)
        except (ZoneInfoNotFoundError, ValueError):
            return IntegrationResult.permanent("CLICKUP_INVALID_TIMEZONE", "CLICKUP_TIMEZONE is not a known time zone.")
        return None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=API_BASE,
                headers={"Authorization": self._token or "", "Accept": "application/json"},
                timeout=httpx.Timeout(self._timeout, connect=min(5.0, self._timeout)),
                limits=httpx.Limits(max_connections=5, max_keepalive_connections=2),
                verify=True,
                follow_redirects=False,
                transport=self._transport,
            )
        return self._client

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        db.assert_no_connection_held()  # never call ClickUp while holding a DB connection
        return self._http().request(method, path, **kwargs)

    # -- execute
    def execute(self, ctx: JobContext) -> IntegrationResult:
        problem = self.configuration_error()
        if problem is not None:
            return problem
        task, recon = load_local_state(ctx)
        if task is None:
            return IntegrationResult.permanent("CLICKUP_LOCAL_TASK_MISSING", "The local task no longer exists.")
        if task.clickup_task_id:
            return IntegrationResult.success(task.clickup_task_id,
                                             result_data={"url": task.clickup_url, "decision": "local_mapping"})

        deadline = time.monotonic() + max(ctx.lease_seconds * 0.6, self._timeout)
        decision = "created"
        if recon.required or ctx.previous_outcome_unknown:
            scan = self._scan(task, deadline)
            if scan.failure is not None:
                return scan.failure
            if len(scan.matches) == 1:
                task_id, url = scan.matches[0]
                logger.info("clickup reconciliation adopted existing task: job=%s", ctx.job_id)
                return IntegrationResult.success(task_id, result_data={"url": url, "decision": "adopted_existing"})
            if len(scan.matches) > 1:
                return IntegrationResult.unknown(
                    "CLICKUP_DUPLICATE_MARKER",
                    f"{len(scan.matches)} ClickUp tasks carry this task's marker; resolve manually.")
            if not scan.complete:
                return IntegrationResult.unknown(
                    "CLICKUP_RECONCILIATION_INCOMPLETE",
                    "The List could not be fully searched; not creating a task.")
            if not recon.confirmed:
                return IntegrationResult.unknown(
                    "CLICKUP_RECONCILIATION_REQUIRED",
                    "No matching ClickUp task found after an uncertain attempt; confirm reconciliation to create it.")
            decision = "created_after_reconciliation"

        if time.monotonic() + self._timeout > deadline:
            return IntegrationResult.retryable("CLICKUP_TIME_BUDGET", "Not enough lease time left to create safely.")
        return self._create(task, ctx, decision)

    def _create(self, task: LocalTask, ctx: JobContext, decision: str) -> IntegrationResult:
        body: dict[str, Any] = {
            "name": task_name(task.title, task.id),
            "description": task_description(task, ctx.payload.get("assignee_role")),
        }
        due = due_date_ms(task.due_date, self._tz)
        if due is not None:
            body["due_date"] = due
            body["due_date_time"] = False
        try:
            response = self._request("POST", f"/list/{self._list_id}/task", json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            # The request never reached ClickUp.
            return IntegrationResult.retryable("CLICKUP_CONNECT_FAILED", f"{type(exc).__name__} before sending.")
        except httpx.TransportError as exc:
            # Read/write timeout, protocol error, connection reset: it may have been created.
            return IntegrationResult.unknown("CLICKUP_CREATE_OUTCOME_UNKNOWN",
                                             f"{type(exc).__name__} after sending the create request.")

        status = response.status_code
        if 200 <= status < 300:
            try:
                data = response.json()
                task_id, url = str(data["id"]), data.get("url")
            except (ValueError, KeyError, TypeError):
                return IntegrationResult.unknown("CLICKUP_MALFORMED_RESPONSE",
                                                 "Create returned success but no readable task id.",
                                                 http_status=status)
            if not _TASK_ID_RE.match(task_id) or (url is not None and not str(url).startswith("https://")):
                return IntegrationResult.unknown("CLICKUP_MALFORMED_RESPONSE",
                                                 "Create returned an unexpected task id or URL.", http_status=status)
            logger.info("clickup task created: job=%s http_status=%s", ctx.job_id, status)
            return IntegrationResult.success(task_id, http_status=status, result_data={"url": url, "decision": decision})
        return self._classify_error(response, after_create=True)

    def _classify_error(self, response: httpx.Response, *, after_create: bool) -> IntegrationResult:
        status = response.status_code
        ecode = self._ecode(response)
        suffix = f" (ClickUp {ecode})" if ecode else ""
        if status == 429:
            return IntegrationResult.retryable(
                "CLICKUP_RATE_LIMITED", "ClickUp rate limit reached." + suffix, http_status=status,
                retry_after_seconds=retry_after_from_headers(response.headers, self._clock(), self._max_delay))
        if after_create and (status == 408 or status >= 500):
            return IntegrationResult.unknown("CLICKUP_CREATE_OUTCOME_UNKNOWN",
                                             f"ClickUp answered {status}; the task may exist." + suffix,
                                             http_status=status)
        if status == 408 or status >= 500:
            return IntegrationResult.retryable(f"CLICKUP_HTTP_{status}", f"ClickUp answered {status}." + suffix,
                                               http_status=status)
        codes = {400: "CLICKUP_BAD_REQUEST", 401: "CLICKUP_UNAUTHORIZED", 403: "CLICKUP_FORBIDDEN",
                 404: "CLICKUP_LIST_NOT_FOUND"}
        return IntegrationResult.permanent(codes.get(status, f"CLICKUP_HTTP_{status}"),
                                           f"ClickUp rejected the request ({status})." + suffix, http_status=status)

    @staticmethod
    def _ecode(response: httpx.Response) -> str | None:
        """ClickUp's short error code (e.g. OAUTH_017) only; never the message body."""
        try:
            value = response.json().get("ECODE")
        except (ValueError, AttributeError):
            return None
        return value if isinstance(value, str) and _ECODE_RE.match(value) else None

    # -- reconciliation scan
    def _scan(self, task: LocalTask, deadline: float) -> _Scan:
        wanted = marker(task.id)
        created_after = int((task.created_at - CREATED_AFTER_MARGIN).timestamp() * 1000)
        found: dict[str, str | None] = {}
        for page in range(self._max_scan_pages):
            if time.monotonic() + self._timeout > deadline:
                return _Scan(list(found.items()), complete=False)
            params = {"page": page, "include_closed": "true", "subtasks": "true",
                      "include_markdown_description": "true", "date_created_gt": created_after}
            try:
                response = self._request("GET", f"/list/{self._list_id}/task", params=params)
            except httpx.TransportError as exc:
                # A read has no side effects: safe to retry; the unknown attempt
                # stays in history, so the next attempt reconciles again.
                return _Scan([], complete=False, failure=IntegrationResult.retryable(
                    "CLICKUP_RECONCILIATION_FAILED", f"{type(exc).__name__} while searching the List."))
            if response.status_code != 200:
                return _Scan([], complete=False, failure=self._classify_error(response, after_create=False))
            try:
                data = response.json()
                tasks = data["tasks"]
                if not isinstance(tasks, list):
                    raise TypeError
            except (ValueError, KeyError, TypeError):
                return _Scan([], complete=False, failure=IntegrationResult.retryable(
                    "CLICKUP_MALFORMED_LIST_RESPONSE", "Unexpected response while searching the List."))
            for item in tasks:
                if not isinstance(item, dict):
                    continue
                text = " ".join(str(item.get(k) or "") for k in
                                ("name", "description", "text_content", "markdown_description"))
                if wanted in text and item.get("id"):
                    found[str(item["id"])] = item.get("url")
            last_page = data.get("last_page")
            if last_page is True or (last_page is None and len(tasks) < PAGE_SIZE):
                return _Scan(list(found.items()), complete=True)
        return _Scan(list(found.items()), complete=False)  # page limit reached

    # -- fenced success callback (inside the worker's finalisation transaction)
    def on_success(self, conn: db.Connection, ctx: JobContext, result: IntegrationResult) -> None:
        url = result.result_data.get("url")
        current = conn.execute("SELECT clickup_task_id FROM tasks WHERE id = %s FOR UPDATE",
                               (ctx.subject_id,)).fetchone()
        if current is None:
            raise RuntimeError("local task disappeared before the ClickUp mapping could be stored")
        if current["clickup_task_id"] not in (None, result.external_id):
            # Never overwrite a different mapping; rolling back leaves the job
            # for lease recovery, which then sees the existing mapping.
            raise RuntimeError("task is already mapped to a different ClickUp task")
        conn.execute(
            "UPDATE tasks SET clickup_task_id = %s, clickup_url = COALESCE(%s, clickup_url) WHERE id = %s",
            (result.external_id, url, ctx.subject_id),
        )
        decision = result.result_data.get("decision")
        if decision in ("adopted_existing", "created_after_reconciliation"):
            scope = conn.execute("SELECT p.id AS project_id, p.client_id FROM tasks t JOIN projects p "
                                 "ON p.id = t.project_id WHERE t.id = %s", (ctx.subject_id,)).fetchone()
            activity.record(
                conn, client_id=scope["client_id"], project_id=scope["project_id"], actor_type="worker",
                event_type="integration.clickup_reconciled",
                summary=("Existing ClickUp task adopted after an uncertain attempt" if decision == "adopted_existing"
                         else "ClickUp task created after confirmed reconciliation"),
                data={"job_id": str(ctx.job_id), "task_id": str(ctx.subject_id), "decision": decision,
                      "clickup_task_id": result.external_id, "attempt": ctx.attempt_number},
            )
