"""Slack adapter for ``slack.notify`` jobs (incoming webhooks).

The webhook URL is the credential: it is validated against a strict pattern
(https, hooks.slack.com, /services/T…/B…/…, no credentials, port, query or
fragment) so a misconfiguration cannot turn the worker into an HTTP client for
arbitrary hosts, and it is kept out of logs, results, errors and the database.

Flow for one job:
1. Validate configuration (permanent SLACK_NOT_CONFIGURED /
   SLACK_INVALID_WEBHOOK_URL; no request).
2. Short read of the project (canonical source) and of the job's attempt
   history; release the connection.
3. If an earlier attempt ended ``unknown``, the message may already be in the
   channel and a webhook cannot be searched: unless an administrator has
   confirmed it is ABSENT (payload.reconciliation), stop with unknown
   SLACK_DELIVERY_UNCONFIRMED and send nothing.
4. POST the message. Success only for HTTP 200 with body exactly ``ok``.
   Failures before dispatch are retryable; 429 is retryable with Retry-After;
   other 4xx are permanent; anything that may have been delivered (read/write
   timeout, dropped connection, 408, 5xx, unexpected 200 body) is unknown.

A message found in the channel after an unknown outcome is resolved with
services.job_admin.resolve_slack_delivered (no resend).
"""

import logging
import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from app import db
from app.config import Settings
from app.integrations.common import install_http_log_redaction, retry_after_from_headers
from app.jobs.registry import Handler, JobContext
from app.jobs.result import IntegrationResult
from app.jobs.retry import DEFAULT_MAX_DELAY_SECONDS
from app.store import activity

logger = logging.getLogger("app.integrations.slack")
install_http_log_redaction()

JOB_TYPE = "slack.notify"
ALLOWED_HOSTS = {"hooks.slack.com"}
_PATH_RE = re.compile(r"^/services/T[A-Z0-9]{2,20}/B[A-Z0-9]{2,20}/[A-Za-z0-9]{8,64}$")
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,48}$")
MAX_FIELD_CHARS = 150


# ------------------------------------------------------------ configuration
def webhook_url_problem(url: str | None) -> str | None:
    """None if the URL is an acceptable Slack incoming webhook, else an error code."""
    if not url:
        return "SLACK_NOT_CONFIGURED"
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return "SLACK_INVALID_WEBHOOK_URL"
    if (parts.scheme != "https" or (parts.hostname or "").lower() not in ALLOWED_HOSTS
            or parts.username is not None or parts.password is not None
            or port not in (None, 443) or parts.query or parts.fragment
            or "@" in parts.netloc or not _PATH_RE.match(parts.path)):
        return "SLACK_INVALID_WEBHOOK_URL"
    return None


# ------------------------------------------------------------ message
_MENTION_RE = re.compile(r"@(?=[A-Za-z])")


def safe_text(value: Any, limit: int = MAX_FIELD_CHARS) -> str:
    """Single line, bounded, no Slack control sequences: &, <, > escaped
    (so no <!channel>, <@U…> or links) and '@' followed by a letter broken with
    a zero-width space (no @here / @channel / @name). For plain_text and the
    mrkdwn fallback alike."""
    text = " ".join(str(value).split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _MENTION_RE.sub("@​", text)


@dataclass(frozen=True)
class ProjectFacts:
    id: UUID
    name: str
    status: str
    company: str | None
    service: str | None
    task_count: int
    start_date: date | None
    due_date: date | None


def build_message(project: ProjectFacts, admin_base_url: str) -> dict[str, Any]:
    """Block Kit payload with a plain-text fallback. Every dynamic value goes
    through safe_text; no channel/username override; unfurling off."""
    name = safe_text(project.name)
    company = safe_text(project.company) if project.company else None
    service = safe_text(project.service) if project.service else "—"
    status = project.status.replace("_", " ").capitalize()
    due = project.due_date.strftime("%d %b %Y") if project.due_date else "—"
    url = f"{admin_base_url}/admin/projects/{project.id}"

    lines = [f"Project: {name}"]
    if company:
        lines.append(f"Client: {company}")
    lines += [f"Service: {service}", f"Tasks created: {project.task_count}", f"Project status: {status}",
              f"Due: {due}"]
    fields = [{"type": "plain_text", "text": line} for line in lines]
    return {
        "text": f"New project ready: {name}" + (f" ({company})" if company else ""),
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "New project ready"}},
            {"type": "section", "fields": fields},
            {"type": "context", "elements": [
                {"type": "plain_text", "text": "Onboarding is complete; the team can begin delivery."}]},
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "View project"}, "url": url}]},
        ],
        "unfurl_links": False,
        "unfurl_media": False,
    }


# ------------------------------------------------------------ local state
def load_state(ctx: JobContext) -> tuple[ProjectFacts | None, int | None]:
    """(project facts, latest earlier unknown attempt). One short read."""
    with db.connection() as conn:
        row = conn.execute(
            """
            SELECT p.id, p.name, p.status, p.start_date, p.due_date, c.company, pt.name AS service,
                   (SELECT count(*) FROM tasks t WHERE t.project_id = p.id) AS task_count
            FROM projects p JOIN clients c ON c.id = p.client_id
            LEFT JOIN project_templates pt ON pt.id = p.template_id
            WHERE p.id = %s
            """,
            (ctx.subject_id,),
        ).fetchone()
        unknown = conn.execute(
            "SELECT max(attempt_number) AS n FROM integration_attempts "
            "WHERE job_id = %s AND outcome = 'unknown' AND attempt_number < %s",
            (ctx.job_id, ctx.attempt_number),
        ).fetchone()["n"]
    return (ProjectFacts(**row) if row else None), unknown


# ---------------------------------------------------------------- adapter
class SlackAdapter:
    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None, clock=time.time) -> None:
        self._url = settings.slack_webhook_url.get_secret_value().strip() if settings.slack_webhook_url else None
        self._base_url = settings.app_base_url
        self._timeout = settings.http_timeout_seconds
        self._max_delay = getattr(settings, "worker_max_retry_delay_seconds", DEFAULT_MAX_DELAY_SECONDS)
        self._transport = transport
        self._clock = clock
        self._client: httpx.Client | None = None

    def handler(self) -> Handler:
        return Handler(JOB_TYPE, execute=self.execute, on_success=self.on_success, close=self.close)

    def configuration_error(self) -> str | None:
        return webhook_url_problem(self._url)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=httpx.Timeout(self._timeout, connect=min(5.0, self._timeout)),
                limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
                verify=True,
                follow_redirects=False,
                transport=self._transport,
            )
        return self._client

    def execute(self, ctx: JobContext) -> IntegrationResult:
        problem = self.configuration_error()
        if problem == "SLACK_NOT_CONFIGURED":
            return IntegrationResult.permanent(problem, "SLACK_WEBHOOK_URL is not set.")
        if problem:
            return IntegrationResult.permanent(problem, "SLACK_WEBHOOK_URL is not a valid Slack incoming-webhook URL.")
        if ctx.payload.get("event") not in (None, "project_created"):
            return IntegrationResult.permanent("SLACK_UNSUPPORTED_EVENT", "Unsupported notification event.")

        project, last_unknown = load_state(ctx)
        if project is None:
            return IntegrationResult.permanent("SLACK_PROJECT_MISSING", "The project no longer exists.")
        confirmed = (ctx.payload.get("reconciliation") or {}).get("confirmed_after_attempt")
        if (last_unknown is not None or ctx.previous_outcome_unknown) and not (
                isinstance(confirmed, int) and last_unknown is not None and confirmed >= last_unknown):
            # No way to look the message up: never resend without a human check.
            return IntegrationResult.unknown(
                "SLACK_DELIVERY_UNCONFIRMED",
                "An earlier attempt may have posted the message; check the channel before retrying.")

        body = build_message(project, self._base_url)
        db.assert_no_connection_held()
        try:
            response = self._http().post(self._url, json=body)  # type: ignore[arg-type]
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            return IntegrationResult.retryable("SLACK_CONNECT_FAILED", f"{type(exc).__name__} before sending.")
        except httpx.TransportError as exc:
            return IntegrationResult.unknown("SLACK_DELIVERY_UNKNOWN", f"{type(exc).__name__} after sending.")
        return self._classify(response)

    def _classify(self, response: httpx.Response) -> IntegrationResult:
        status = response.status_code
        text = response.text.strip() if len(response.content) <= 200 else ""
        slack_code = text if _ERROR_CODE_RE.match(text) else None
        if status == 200:
            if text == "ok":
                return IntegrationResult.success(None, http_status=200)
            return IntegrationResult.unknown("SLACK_UNEXPECTED_RESPONSE",
                                             "HTTP 200 without the expected 'ok' body.", http_status=200)
        if status == 429:
            return IntegrationResult.retryable(
                "SLACK_RATE_LIMITED", "Slack rate limit reached.", http_status=429,
                retry_after_seconds=retry_after_from_headers(response.headers, self._clock(), self._max_delay))
        if status == 408 or status >= 500:
            return IntegrationResult.unknown("SLACK_DELIVERY_UNKNOWN",
                                             f"Slack answered {status}; the message may have been posted.",
                                             http_status=status)
        code = f"SLACK_{slack_code.upper()}" if slack_code else f"SLACK_HTTP_{status}"
        return IntegrationResult.permanent(code, f"Slack rejected the message ({status}).", http_status=status)

    def on_success(self, conn: db.Connection, ctx: JobContext, result: IntegrationResult) -> None:
        scope = conn.execute("SELECT id, client_id FROM projects WHERE id = %s", (ctx.subject_id,)).fetchone()
        if scope is None:
            return
        activity.record(conn, client_id=scope["client_id"], project_id=scope["id"], actor_type="worker",
                        event_type="integration.slack_notified",
                        summary="Team notified in Slack: new project ready",
                        data={"job_id": str(ctx.job_id), "attempt": ctx.attempt_number})
