"""Application settings, loaded from the environment and the repository-root .env.

Secrets are typed as ``SecretStr`` so they never appear in ``repr()``, logs or
tracebacks. Production fails closed: the settings object cannot be built when a
required security setting is missing, so the API refuses to start.
"""

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.mfa import MfaKeyError, parse_key
from app.security import MIN_WEBHOOK_SECRET_LENGTH

REPO_ROOT = Path(__file__).resolve().parents[2]



class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # LTD_DISABLE_DOTENV=1 (set by the test suite) ignores .env entirely, so
        # tests and their subprocesses can never pick up real credentials.
        env_file=None if os.environ.get("LTD_DISABLE_DOTENV") == "1" else REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        # KEY= (empty) in .env means "not set", not "empty secret".
        env_ignore_empty=True,
        extra="ignore",
        # Validation errors must never echo input values (DSN, secrets).
        hide_input_in_errors=True,
    )

    app_env: Literal["development", "test", "production"] = "development"
    app_base_url: str = "http://localhost:5173"

    database_url: SecretStr
    # Only read by the test suite; declared here so it is documented and typed.
    test_database_url: SecretStr | None = None

    # Encrypts administrators' TOTP secrets at rest (32 random bytes, base64):
    # `python -m app.mfa generate-key`. Accounts: `python -m app.admin_users`.
    admin_mfa_encryption_key: SecretStr | None = None
    crm_webhook_secret: SecretStr | None = None

    clickup_api_token: SecretStr | None = None
    clickup_list_id: str | None = None
    # IANA time zone in which date-only due dates are meant (the agency's).
    # Validated by the ClickUp adapter, not here, so a bad value only affects
    # ClickUp jobs and never stops the API from starting.
    clickup_timezone: str = "UTC"
    google_service_account_file: Path | None = None
    google_sheets_spreadsheet_id: str | None = None
    # Validated by the Sheets adapter (not here), so a bad value only affects Sheets jobs.
    google_sheets_worksheet: str = "Projects"
    # Live check only: a dedicated test spreadsheet, never the reporting one.
    google_sheets_test_spreadsheet_id: str | None = None
    google_sheets_test_worksheet: str | None = None
    slack_webhook_url: SecretStr | None = None

    http_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    worker_poll_seconds: float = Field(default=2.0, gt=0, le=300)
    job_max_attempts: int = Field(default=5, ge=1, le=50)
    # Worker: a claimed job's lease. Handlers (and their HTTP timeouts) must
    # finish well inside it, or another worker may reclaim the job.
    worker_lease_seconds: int = Field(default=120, ge=10, le=3600)
    worker_max_retry_delay_seconds: int = Field(default=900, ge=30, le=86400)
    # Optional fixed identity; default is host:pid:random, unique per process.
    worker_id: str | None = Field(default=None, min_length=1, max_length=100,
                                  pattern=r"^[A-Za-z0-9._:-]+$")

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    def usable_webhook_secret(self) -> str | None:
        """The CRM webhook secret, or None when unset or too short.

        Enforced in every environment: there is no weak-secret or no-secret mode.
        """
        if self.crm_webhook_secret is None:
            return None
        value = self.crm_webhook_secret.get_secret_value()
        return value if len(value) >= MIN_WEBHOOK_SECRET_LENGTH else None

    @field_validator("app_base_url")
    @classmethod
    def _base_url_is_http(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("APP_BASE_URL must be an absolute http(s) URL")
        return value.rstrip("/")

    @field_validator("admin_mfa_encryption_key")
    @classmethod
    def _mfa_key_format(cls, value: SecretStr | None) -> SecretStr | None:
        # Validated in every environment: a malformed key would otherwise make
        # every administrator login fail with no clear cause.
        if value is not None:
            try:
                parse_key(value.get_secret_value())
            except MfaKeyError as exc:
                raise ValueError(f"{exc}; generate one with `python -m app.mfa generate-key`") from None
        return value

    def mfa_key(self) -> bytes:
        """The decoded MFA encryption key, or MfaKeyError when not configured."""
        return parse_key(self.admin_mfa_encryption_key.get_secret_value() if self.admin_mfa_encryption_key else None)

    @model_validator(mode="after")
    def _http_timeout_inside_lease(self) -> "Settings":
        if self.http_timeout_seconds >= self.worker_lease_seconds:
            raise ValueError("HTTP_TIMEOUT_SECONDS must be shorter than WORKER_LEASE_SECONDS")
        return self

    @model_validator(mode="after")
    def _production_fails_closed(self) -> "Settings":
        if not self.is_production:
            return self
        problems: list[str] = []
        if self.admin_mfa_encryption_key is None:
            problems.append("ADMIN_MFA_ENCRYPTION_KEY is required")
        secret = self.crm_webhook_secret.get_secret_value() if self.crm_webhook_secret else ""
        if len(secret) < MIN_WEBHOOK_SECRET_LENGTH:
            problems.append(
                f"CRM_WEBHOOK_SECRET is required (at least {MIN_WEBHOOK_SECRET_LENGTH} characters)"
            )
        if urlparse(self.app_base_url).scheme != "https":
            problems.append("APP_BASE_URL must use https")
        if problems:
            # Names only: never echo the values.
            raise ValueError("Insecure production configuration: " + "; ".join(problems))
        return self

    def integration_status(self) -> dict[str, bool]:
        """Which integrations have credentials. Booleans only, never values."""
        return {
            "clickup": self.clickup_api_token is not None and self.clickup_list_id is not None,
            "google_sheets": self.google_service_account_file is not None
            and self.google_sheets_spreadsheet_id is not None,
            "slack": self.slack_webhook_url is not None,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
