"""The test-database guard must refuse everything except exactly ltd_test."""

import psycopg
import pytest

from tests.db_safety import (
    UnsafeTestDatabaseError,
    assert_connected_to_test_database,
    assert_test_database_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql:///ltd",
        "postgresql://user:pw@localhost:5432/ltd",
        "postgresql:///ltd_test_copy",
        "postgresql:///LTD_TEST",
        "postgresql:///postgres",
        "postgresql://localhost",  # no database name: libpq would default to the user name
        "dbname=ltd host=localhost",
        "postgresql:///ltd_test?dbname=ltd",  # query parameter overrides the path
    ],
)
def test_url_guard_rejects_non_test_databases(url: str) -> None:
    with pytest.raises(UnsafeTestDatabaseError):
        assert_test_database_url(url)


@pytest.mark.parametrize("url", ["postgresql:///ltd_test", "dbname=ltd_test", "postgresql://u@h:5432/ltd_test"])
def test_url_guard_accepts_ltd_test(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    assert_test_database_url(url)


def test_url_guard_refuses_production_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(UnsafeTestDatabaseError):
        assert_test_database_url("postgresql:///ltd_test")


class _FakeConnection:
    def __init__(self, dbname: str) -> None:
        self._dbname = dbname

    def execute(self, _sql: str) -> "_FakeConnection":
        return self

    def fetchone(self) -> tuple[str]:
        return (self._dbname,)


@pytest.mark.parametrize("actual", ["ltd", "postgres", "ltd_test2", "production"])
def test_connection_guard_rejects_other_databases(actual: str) -> None:
    with pytest.raises(UnsafeTestDatabaseError):
        assert_connected_to_test_database(_FakeConnection(actual))  # type: ignore[arg-type]


def test_connection_guard_checks_the_real_server(test_database_url: str) -> None:
    with psycopg.connect(test_database_url) as conn:
        assert_connected_to_test_database(conn)
    # A real connection to another database is refused. "postgres" is the
    # maintenance database; connecting is read-only and touches nothing.
    with psycopg.connect(test_database_url.replace("ltd_test", "postgres")) as conn:
        with pytest.raises(UnsafeTestDatabaseError):
            assert_connected_to_test_database(conn)
