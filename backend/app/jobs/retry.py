"""Retry policy: exponential backoff, Retry-After, and HTTP status classification."""

from app.jobs.result import Outcome

BASE_DELAY_SECONDS = 30
DEFAULT_MAX_DELAY_SECONDS = 900


def backoff_seconds(
    attempt_number: int,
    *,
    max_delay: int = DEFAULT_MAX_DELAY_SECONDS,
    retry_after: float | None = None,
) -> int:
    """Delay before the next attempt after attempt ``attempt_number`` failed.

    Default: min(30 * 2**(attempt_number - 1), max_delay) -> 30, 60, 120, 240,
    480, 900, 900, ... A Retry-After supplied by the integration replaces the
    backoff, bounded to [1, max_delay].
    """
    if retry_after is not None:
        return int(min(max(retry_after, 1), max_delay))
    exponent = max(attempt_number - 1, 0)
    if exponent >= 20:  # avoid huge integers; already far beyond any cap
        return max_delay
    return int(min(BASE_DELAY_SECONDS * 2**exponent, max_delay))


def classify_http_status(status: int) -> Outcome:
    """For adapters: 2xx success; 408, 429, 5xx retryable; other 4xx permanent.

    Note: a request that may have reached the server but whose response was
    lost is NOT an HTTP status; adapters must report that as ``unknown``.
    """
    if 200 <= status < 300:
        return "success"
    if status in (408, 429) or 500 <= status < 600:
        return "retryable"
    return "permanent"
