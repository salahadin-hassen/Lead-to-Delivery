"""Helpers shared by integration adapters."""

import logging
import math
import re
from email.utils import parsedate_to_datetime

import httpx

# ------------------------------------------------------------ rate limiting


def retry_after_from_headers(headers: httpx.Headers, now: float, max_delay: int) -> float | None:
    """Seconds to wait after a 429, from Retry-After (seconds or HTTP date) or
    X-RateLimit-Reset (Unix seconds), bounded to [1, max_delay]. None when
    neither is usable; the worker then applies its normal backoff."""
    value: float | None = None
    retry_after = headers.get("retry-after")
    if retry_after:
        try:
            value = float(retry_after)
        except ValueError:
            try:
                value = parsedate_to_datetime(retry_after).timestamp() - now
            except (TypeError, ValueError):
                value = None
    if value is None and headers.get("x-ratelimit-reset"):
        try:
            value = float(headers["x-ratelimit-reset"]) - now
        except ValueError:
            value = None
    if value is None or math.isnan(value):
        return None
    return float(min(max(math.ceil(value), 1), max_delay))


# ------------------------------------------------------ log URL redaction
# Slack incoming-webhook URLs ARE the credential (the secret is in the path),
# and httpx logs every request URL at INFO ("HTTP Request: POST <url> ...").
# This filter rewrites any Slack webhook URL, in full, before a record from
# the HTTP-client loggers reaches any handler.
SLACK_WEBHOOK_URL_RE = re.compile(r"(?i)\b(?:https?://)?hooks\.slack(?:-gov)?\.com[^\s'\"<>)]*")
REDACTED_URL = "<redacted-slack-webhook-url>"
_HTTP_LOGGERS = ("httpx", "httpcore", "httpcore.connection", "httpcore.http11", "httpcore.http2",
                 "httpcore.proxy", "httpcore.socks")


class SecretUrlFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken record must not crash logging
            return True
        if SLACK_WEBHOOK_URL_RE.search(message):
            record.msg = SLACK_WEBHOOK_URL_RE.sub(REDACTED_URL, message)
            record.args = ()
        return True


_FILTER = SecretUrlFilter()


def install_http_log_redaction() -> None:
    """Attach the redaction filter to the httpx/httpcore loggers (idempotent).
    Filters on a logger only see records created by that logger, so each
    known HTTP-client logger gets its own."""
    for name in _HTTP_LOGGERS:
        logger = logging.getLogger(name)
        if _FILTER not in logger.filters:
            logger.addFilter(_FILTER)
