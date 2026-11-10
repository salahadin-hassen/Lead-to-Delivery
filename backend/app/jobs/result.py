"""The result every job handler returns, and error-text sanitising.

Outcomes:
  success    the external operation completed (external_id optional)
  retryable  it did not happen and may succeed later (timeouts before send,
             connection refused, 408, 429, 5xx)
  permanent  it will not succeed without a change (bad credentials, missing
             configuration, invalid resource, 4xx)
  unknown    it MAY have happened (e.g. the request was sent, then the
             connection dropped). Never retried automatically.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Literal

Outcome = Literal["success", "retryable", "permanent", "unknown"]

MAX_ERROR_MESSAGE = 300
_CODE_RE = re.compile(r"[^A-Z0-9_]+")
_REDACTIONS = (
    (re.compile(r"https?://\S+", re.IGNORECASE), "<url>"),
    # "Authorization: Bearer xyz" -> the scheme AND the credential go.
    (re.compile(r"(?i)\bauthorization\s*[:=]\s*(?:(?:bearer|basic|token)\s+)?\S+"), "authorization=<redacted>"),
    (re.compile(r"(?i)\b(bearer|basic)\s+\S+"), r"\1 <redacted>"),
    (re.compile(r"(?i)\b(password|secret|token|api[_-]?key|access[_-]?token)\s*[:=]\s*\S+"), r"\1=<redacted>"),
    (re.compile(r"[A-Za-z0-9_\-]{32,}"), "<redacted>"),  # long opaque strings: tokens, keys
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
)


def sanitize_message(message: str | None) -> str | None:
    """Short, single-line, with URLs, credentials, long tokens and emails removed."""
    if not message:
        return None
    text = " ".join(str(message).split())
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text[:MAX_ERROR_MESSAGE] or None


def sanitize_code(code: str | None) -> str | None:
    if not code:
        return None
    cleaned = _CODE_RE.sub("_", str(code).upper()).strip("_")[:64]
    return cleaned or None


@dataclass(frozen=True)
class IntegrationResult:
    outcome: Outcome
    error_code: str | None = None
    error_message: str | None = None
    http_status: int | None = None
    external_id: str | None = None
    duration_ms: int | None = None
    retry_after_seconds: float | None = None
    result_data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.outcome not in ("success", "retryable", "permanent", "unknown"):
            raise ValueError(f"invalid outcome {self.outcome!r}")
        if self.http_status is not None and not 100 <= self.http_status <= 599:
            object.__setattr__(self, "http_status", None)
        object.__setattr__(self, "error_code", sanitize_code(self.error_code))
        object.__setattr__(self, "error_message", sanitize_message(self.error_message))
        if self.external_id is not None:
            object.__setattr__(self, "external_id", str(self.external_id)[:200])

    @classmethod
    def success(cls, external_id: str | None = None, **kwargs: Any) -> "IntegrationResult":
        return cls("success", external_id=external_id, **kwargs)

    @classmethod
    def retryable(cls, error_code: str, error_message: str | None = None, **kwargs: Any) -> "IntegrationResult":
        return cls("retryable", error_code=error_code, error_message=error_message, **kwargs)

    @classmethod
    def permanent(cls, error_code: str, error_message: str | None = None, **kwargs: Any) -> "IntegrationResult":
        return cls("permanent", error_code=error_code, error_message=error_message, **kwargs)

    @classmethod
    def unknown(cls, error_code: str, error_message: str | None = None, **kwargs: Any) -> "IntegrationResult":
        return cls("unknown", error_code=error_code, error_message=error_message, **kwargs)
