"""Application-level errors.

Routes and services see these, never raw driver exceptions or their messages
(driver messages can contain hosts, database names or parts of a DSN).
"""


class AppError(Exception):
    """Base class for errors raised by this application."""


# --- Database -------------------------------------------------------------


class DatabaseUnavailableError(AppError):
    """PostgreSQL cannot be reached or no pooled connection became free in time."""


class ConstraintViolationError(AppError):
    """A database constraint rejected a write. Carries the constraint name only."""

    def __init__(self, constraint: str | None) -> None:
        super().__init__(f"constraint violated: {constraint or 'unknown'}")
        self.constraint = constraint


class ConnectionHeldError(AppError):
    """Code tried to make an external call while holding a database connection.

    External HTTP requests must run with no connection checked out and no
    transaction open, so slow third parties cannot exhaust the pool or hold
    row locks.
    """


# --- CRM webhook intake ---------------------------------------------------


class WebhookAuthError(AppError):
    """Webhook authentication failed. ``code`` is safe to return to the caller."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class EventIdReusedError(AppError):
    """An event ID that was already stored arrived with a different payload."""


class UnknownServiceTypeError(AppError):
    """deal.service_type does not match an active project template key."""


class IntakeRetryExhaustedError(AppError):
    """Concurrent intake kept conflicting; nothing was committed. Safe to retry."""


# --- Client portal --------------------------------------------------------


class PortalAuthError(AppError):
    """Portal token missing, unknown, expired or revoked. ``code`` is safe to return."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AlreadySubmittedError(AppError):
    """Onboarding for this invitation was already submitted."""


class InvitationClosedError(AppError):
    """The invitation is expired or revoked and can no longer be submitted."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProjectNotFoundError(AppError):
    """No project is visible to this invitation (not created yet, or not its own)."""


class TemplateUnavailableError(AppError):
    """The invitation's project template is missing or its definition is invalid."""


# --- Administrator --------------------------------------------------------


class AdminAuthError(AppError):
    """Not authenticated as the administrator. ``code`` is safe to return."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AdminNotConfiguredError(AppError):
    """ADMIN_MFA_ENCRYPTION_KEY is not set (or cannot decrypt): administrator login is impossible."""


class AdminLoginThrottledError(AppError):
    def __init__(self, retry_after: int) -> None:
        super().__init__(f"retry after {retry_after}s")
        self.retry_after = retry_after


class AdminCsrfError(AppError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class NotFoundError(AppError):
    """A requested admin resource does not exist."""


class ConflictError(AppError):
    """A request conflicts with current state. ``code`` and message are safe to return."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --- Integration jobs -----------------------------------------------------


class JobNotFoundError(AppError):
    """No integration job with that id."""


class JobNotRetryableError(AppError):
    """Only dead jobs can be retried manually. ``status`` is the current status."""

    def __init__(self, status: str) -> None:
        super().__init__(f"job is {status}, not dead")
        self.status = status


class ReconciliationRequiredError(AppError):
    """The job's last outcome is unknown; the external system must be checked
    (and the caller must confirm that) before it may run again."""


class JobNotResolvableError(AppError):
    """The job cannot be resolved as delivered. ``reason`` is safe to return."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --- Migrations -----------------------------------------------------------


class MigrationError(AppError):
    """Base class for migration problems. Messages are safe to print."""


class MigrationFileError(MigrationError):
    """A migration file is misnamed, duplicated, missing or contains forbidden SQL."""


class MigrationChecksumError(MigrationError):
    """An already-applied migration file was edited after it was applied."""


class MigrationLockTimeoutError(MigrationError):
    """Another process held the migration advisory lock for too long."""


class MigrationFailedError(MigrationError):
    """A migration's SQL failed; its transaction was rolled back."""

    def __init__(self, version: int, name: str, sqlstate: str | None, detail: str) -> None:
        super().__init__(
            f"Migration {version:04d}_{name} failed (SQLSTATE {sqlstate or 'unknown'}): {detail}. "
            "It was rolled back and not recorded; earlier migrations remain applied."
        )
        self.version = version
        self.name = name
        self.sqlstate = sqlstate
