"""Guards that keep destructive test setup away from any non-test database.

Two independent checks, both required before anything is dropped:

1. The configured TEST_DATABASE_URL must name exactly ``ltd_test``.
2. The *connected* database (``current_database()``) must be exactly
   ``ltd_test``; this catches poolers, service files or defaults that route a
   correct-looking URL somewhere else.

APP_ENV=production refuses outright.
"""

import os

import psycopg
from psycopg.conninfo import conninfo_to_dict

REQUIRED_TEST_DATABASE = "ltd_test"


class UnsafeTestDatabaseError(RuntimeError):
    pass


def assert_test_database_url(url: str) -> None:
    if os.environ.get("APP_ENV", "").strip().lower() == "production":
        raise UnsafeTestDatabaseError("refusing to run tests with APP_ENV=production")
    try:
        params = conninfo_to_dict(url)
    except psycopg.ProgrammingError as exc:
        raise UnsafeTestDatabaseError("TEST_DATABASE_URL is not a valid PostgreSQL URL") from exc
    dbname = params.get("dbname")
    if dbname != REQUIRED_TEST_DATABASE:
        raise UnsafeTestDatabaseError(
            f"TEST_DATABASE_URL must name database {REQUIRED_TEST_DATABASE!r}, "
            f"got {dbname!r}; refusing to touch it"
        )


def assert_connected_to_test_database(conn: psycopg.Connection) -> None:
    row = conn.execute("SELECT current_database()").fetchone()
    actual = row[0] if isinstance(row, tuple) else (row or {}).get("current_database")
    if actual != REQUIRED_TEST_DATABASE:
        raise UnsafeTestDatabaseError(
            f"connected to database {actual!r}, not {REQUIRED_TEST_DATABASE!r}; "
            "refusing destructive test setup"
        )
