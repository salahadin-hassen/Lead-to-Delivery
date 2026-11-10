"""GET /api/health: 200 only with a reachable, fully migrated database."""

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from app import db
from app.main import create_app
from app.migrations import SQL_DIR, run_migrations
from tests.conftest import connect_to_schema, make_settings

UNREACHABLE = "postgresql://ltd:TopSecretPw@127.0.0.1:1/ltd_test?connect_timeout=1"


def _get_health(database_url: str) -> tuple[int, dict, str]:
    with TestClient(create_app(make_settings(database_url))) as client:
        response = client.get("/api/health")
    return response.status_code, response.json(), response.text


def test_health_ok_when_database_is_reachable_and_migrated(migrated_database, pool_cleanup) -> None:
    status, body, _ = _get_health(migrated_database)
    assert (status, body) == (200, {"status": "ok"})


def test_health_503_when_database_is_unreachable(monkeypatch: pytest.MonkeyPatch, pool_cleanup) -> None:
    monkeypatch.setattr(db, "HEALTH_CHECK_TIMEOUT_SECONDS", 0.5)
    status, body, text = _get_health(UNREACHABLE)
    assert (status, body) == (503, {"status": "unavailable"})
    for leak in ("TopSecretPw", "127.0.0.1", "postgresql", "ltd_test"):
        assert leak not in text


def test_health_503_when_schema_is_not_migrated(migrated_database, scratch_schema, pool_cleanup) -> None:
    url = make_conninfo(migrated_database, options=f"-c search_path={scratch_schema}")
    status, body, _ = _get_health(url)
    assert (status, body) == (503, {"status": "unavailable"})


def test_health_503_when_schema_is_behind(migrated_database, scratch_schema, tmp_path: Path, pool_cleanup) -> None:
    shutil.copy(SQL_DIR / "0001_core_schema.sql", tmp_path)
    with connect_to_schema(migrated_database, scratch_schema) as conn:
        run_migrations(conn, sql_dir=tmp_path)
    url = make_conninfo(migrated_database, options=f"-c search_path={scratch_schema}")
    status, body, _ = _get_health(url)
    assert (status, body) == (503, {"status": "unavailable"})


def test_app_lifespan_opens_and_closes_the_pool(migrated_database, pool_cleanup) -> None:
    app = create_app(make_settings(migrated_database))
    with TestClient(app):
        assert db.is_pool_open()
    assert not db.is_pool_open()


def test_unhandled_database_errors_return_generic_500(migrated_database, pool_cleanup) -> None:
    app = create_app(make_settings(migrated_database))

    @app.get("/api/_boom")
    def boom() -> None:
        with db.connection() as conn:
            conn.execute("SELECT * FROM table_that_does_not_exist_x")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/_boom")
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert "table_that_does_not_exist_x" not in response.text


def test_production_app_refuses_to_start_without_security_settings(
    monkeypatch: pytest.MonkeyPatch, pool_cleanup
) -> None:
    from pydantic import ValidationError

    import app.main as main_module
    from app.config import Settings

    for key in ("ADMIN_PASSWORD_HASH", "CRM_WEBHOOK_SECRET"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("APP_BASE_URL", "https://hub.example.com")
    monkeypatch.setenv("DATABASE_URL", "postgresql:///ltd")
    # Same code path as `uvicorn app.main:app`, isolated from the developer's .env.
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(_env_file=None))
    app = main_module.create_app()
    with pytest.raises(ValidationError, match="Insecure production configuration"):
        with TestClient(app):
            pass
    assert not db.is_pool_open()  # never got as far as opening the pool
