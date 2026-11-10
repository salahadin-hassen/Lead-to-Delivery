"""Configuration: production fails closed; secrets never leak through repr."""

import pytest
from pydantic import ValidationError

from app.config import Settings
MFA_KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="  # test-only

_ENV_KEYS = (
    "APP_ENV", "APP_BASE_URL", "DATABASE_URL", "ADMIN_MFA_ENCRYPTION_KEY", "CRM_WEBHOOK_SECRET",
    "CLICKUP_API_TOKEN", "CLICKUP_LIST_ID", "GOOGLE_SERVICE_ACCOUNT_FILE",
    "GOOGLE_SHEETS_SPREADSHEET_ID", "SLACK_WEBHOOK_URL", "HTTP_TIMEOUT_SECONDS",
    "WORKER_POLL_SECONDS", "JOB_MAX_ATTEMPTS",
)

DB_URL = "postgresql://ltd:db-password-123@localhost/ltd"
WEBHOOK_SECRET = "w" * 40


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def build(**values) -> Settings:
    return Settings(_env_file=None, database_url=DB_URL, **values)


def test_development_defaults_need_only_database_url() -> None:
    settings = build()
    assert settings.app_env == "development"
    assert settings.admin_mfa_encryption_key is None
    assert settings.integration_status() == {"clickup": False, "google_sheets": False, "slack": False}


def test_database_url_is_required() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_production_without_security_settings_fails_closed() -> None:
    with pytest.raises(ValidationError) as excinfo:
        build(app_env="production", app_base_url="https://hub.example.com")
    message = str(excinfo.value)
    assert "ADMIN_MFA_ENCRYPTION_KEY is required" in message
    assert "CRM_WEBHOOK_SECRET is required" in message
    assert "db-password-123" not in message  # input values are hidden


def test_production_rejects_short_webhook_secret_and_plain_http() -> None:
    with pytest.raises(ValidationError) as excinfo:
        build(
            app_env="production",
            app_base_url="http://hub.example.com",
            admin_mfa_encryption_key=MFA_KEY,
            crm_webhook_secret="short",
        )
    message = str(excinfo.value)
    assert "CRM_WEBHOOK_SECRET" in message and "https" in message
    assert "short" not in message.replace("CRM_WEBHOOK_SECRET is required", "")


def test_complete_production_configuration_is_accepted() -> None:
    settings = build(
        app_env="production",
        app_base_url="https://hub.example.com/",
        admin_mfa_encryption_key=MFA_KEY,
        crm_webhook_secret=WEBHOOK_SECRET,
    )
    assert settings.is_production
    assert settings.app_base_url == "https://hub.example.com"


def test_malformed_mfa_key_is_rejected_in_every_environment() -> None:
    with pytest.raises(ValidationError) as excinfo:
        build(admin_mfa_encryption_key="plaintext-not-a-key")
    assert "plaintext-not-a-key" not in str(excinfo.value)


def test_mfa_key_is_never_shown_in_repr() -> None:
    settings = build(admin_mfa_encryption_key=MFA_KEY)
    assert MFA_KEY not in repr(settings) and MFA_KEY not in str(settings.model_dump())


def test_secrets_do_not_appear_in_repr_or_str() -> None:
    settings = build(
        crm_webhook_secret=WEBHOOK_SECRET,
        clickup_api_token="pk_secret_token",
        slack_webhook_url="https://hooks.slack.com/services/T000/B000/XXXX",
    )
    for rendered in (repr(settings), str(settings), str(settings.model_dump())):
        assert "db-password-123" not in rendered
        assert WEBHOOK_SECRET not in rendered
        assert "pk_secret_token" not in rendered
        assert "hooks.slack.com" not in rendered


def test_empty_environment_values_mean_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "")
    settings = build()
    assert settings.slack_webhook_url is None
    assert settings.clickup_api_token is None


def test_integration_status_reports_booleans_only() -> None:
    settings = build(
        clickup_api_token="pk_x", clickup_list_id="123",
        slack_webhook_url="https://hooks.slack.com/services/a/b/c",
    )
    assert settings.integration_status() == {"clickup": True, "google_sheets": False, "slack": True}


@pytest.mark.parametrize(
    ("field", "value"),
    [("http_timeout_seconds", 0), ("worker_poll_seconds", -1), ("job_max_attempts", 0),
     ("app_base_url", "not-a-url"), ("app_env", "staging")],
)
def test_invalid_values_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        build(**{field: value})


def test_suite_never_reads_dotenv_credentials() -> None:
    """The test session must not see integration credentials from .env."""
    import os

    from app.config import Settings
    assert os.environ.get("LTD_DISABLE_DOTENV") == "1"
    assert Settings.model_config["env_file"] is None
    for key in ("GOOGLE_SERVICE_ACCOUNT_FILE", "GOOGLE_SHEETS_SPREADSHEET_ID", "CLICKUP_API_TOKEN",
                "SLACK_WEBHOOK_URL", "CRM_WEBHOOK_SECRET", "ADMIN_MFA_ENCRYPTION_KEY"):
        assert key not in os.environ, key
