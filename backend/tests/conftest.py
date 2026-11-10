"""Shared fixtures. Database tests run against a real PostgreSQL ``ltd_test``.

Database tests fail loudly (no skips) when TEST_DATABASE_URL is missing or the
database is unreachable. Tests that do not request a database fixture run
without one.

Reset strategy: once per session, ``public`` is dropped and recreated in
ltd_test (after both safety checks) and every migration is applied. Before
each database test, every table except ``schema_migrations`` and the seeded
``project_templates`` is truncated; the table list is read from the catalog,
so new tables are covered automatically.
"""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

# Must run before any `app` import: Settings fixes its env file at import time.
def _isolate_from_real_configuration() -> None:
    """Take ONLY TEST_DATABASE_URL from the repo .env (if not already set) and
    make Settings ignore .env for the rest of the session, including in
    subprocesses. Real integration credentials in .env (ClickUp, Slack,
    Google) must never reach a test. Values are never printed."""
    path = Path(__file__).resolve().parents[2] / ".env"
    if "TEST_DATABASE_URL" not in os.environ and path.is_file():
        from dotenv import dotenv_values  # installed with pydantic-settings

        value = dotenv_values(path).get("TEST_DATABASE_URL")
        if value:
            os.environ["TEST_DATABASE_URL"] = value
    os.environ["LTD_DISABLE_DOTENV"] = "1"


_isolate_from_real_configuration()


from app import db
from app.config import Settings
from app.migrations import run_migrations
from tests.db_safety import (
    assert_connected_to_test_database,
    assert_test_database_url,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
_KEEP_TABLES = ("schema_migrations", "project_templates")




@pytest.fixture(scope="session")
def test_database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "TEST_DATABASE_URL is not set. Database tests fail loudly by design: set it "
            "(e.g. postgresql:///ltd_test) in the environment or the repository .env."
        )
    assert_test_database_url(url)
    return url


@pytest.fixture(scope="session")
def migrated_database(test_database_url: str) -> str:
    """Fresh public schema in ltd_test with all migrations applied."""
    with psycopg.connect(test_database_url, autocommit=True, connect_timeout=5) as conn:
        assert_connected_to_test_database(conn)
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        run_migrations(conn)
    return test_database_url


@pytest.fixture
def db_conn(migrated_database: str) -> Iterator[psycopg.Connection]:
    """Autocommit connection to a truncated ltd_test (dict rows)."""
    with psycopg.connect(migrated_database, autocommit=True, row_factory=dict_row) as conn:
        assert_connected_to_test_database(conn)
        row = conn.execute(
            "SELECT string_agg(format('%%I.%%I', schemaname, tablename), ', ') AS tables "
            "FROM pg_tables WHERE schemaname = 'public' AND NOT (tablename = ANY(%s))",
            (list(_KEEP_TABLES),),
        ).fetchone()
        if row and row["tables"]:
            conn.execute(f"TRUNCATE {row['tables']} RESTART IDENTITY CASCADE")
        yield conn


@pytest.fixture
def scratch_schema(migrated_database: str) -> Iterator[str]:
    """An empty, uniquely named schema in ltd_test, dropped afterwards.

    Connect with ``options=-c search_path=<schema>`` to migrate it in isolation
    from the session schema.
    """
    name = f"scratch_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        assert_connected_to_test_database(conn)
        conn.execute(f'CREATE SCHEMA "{name}"')
    try:
        yield name
    finally:
        with psycopg.connect(migrated_database, autocommit=True) as conn:
            assert_connected_to_test_database(conn)
            conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')


def connect_to_schema(url: str, schema: str, **kwargs) -> psycopg.Connection:
    return psycopg.connect(url, autocommit=True, options=f"-c search_path={schema}", **kwargs)


def make_settings(database_url: str, **overrides) -> Settings:
    """Settings isolated from the developer's .env and integration credentials."""
    values = {"database_url": database_url, "app_env": "test", **overrides}
    return Settings(_env_file=None, **values)


@pytest.fixture
def pool_cleanup() -> Iterator[None]:
    """Guarantee the module-level pool is closed after a test."""
    try:
        yield
    finally:
        db.close_pool()
