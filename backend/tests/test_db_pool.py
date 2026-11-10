"""Connection pool lifecycle, transactions and the no-connection-during-HTTP guard."""

import time

import psycopg
import pytest

from app import db
from app.errors import ConnectionHeldError, DatabaseUnavailableError

UNREACHABLE = "postgresql://ltd:TopSecretPw@127.0.0.1:1/ltd_test?connect_timeout=1"


def _backends(url: str) -> int:
    with psycopg.connect(url, autocommit=True) as conn:
        return conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND application_name = %s",
            (db.APPLICATION_NAME,),
        ).fetchone()[0]


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_pool_returns_dict_rows_and_sets_statement_timeout(db_conn, migrated_database, pool_cleanup) -> None:
    db.configure_pool(migrated_database)
    with db.connection() as conn:
        row = conn.execute("SELECT current_database() AS name, current_setting('statement_timeout') AS st").fetchone()
    assert row == {"name": "ltd_test", "st": "15s"}


def test_transaction_commits_on_success_and_rolls_back_on_error(db_conn, migrated_database, pool_cleanup) -> None:
    db.configure_pool(migrated_database)
    with db.transaction() as conn:
        conn.execute("INSERT INTO clients (name, email) VALUES ('Kept', 'kept@x.com')")
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute("INSERT INTO clients (name, email) VALUES ('Lost', 'lost@x.com')")
            raise RuntimeError("boom")
    names = [r["name"] for r in db_conn.execute("SELECT name FROM clients ORDER BY name").fetchall()]
    assert names == ["Kept"]


def test_connection_guard_blocks_external_calls_inside_db_blocks(migrated_database, pool_cleanup) -> None:
    db.configure_pool(migrated_database)
    assert not db.connection_held()
    db.assert_no_connection_held()
    with db.transaction():
        assert db.connection_held()
        with pytest.raises(ConnectionHeldError):
            db.assert_no_connection_held()
    with db.connection():
        with pytest.raises(ConnectionHeldError):
            db.assert_no_connection_held()
    assert not db.connection_held()
    # The guard is released even when the block fails.
    with pytest.raises(ValueError):
        with db.connection():
            raise ValueError
    assert not db.connection_held()


def test_close_pool_releases_every_server_connection(migrated_database, pool_cleanup) -> None:
    db.configure_pool(migrated_database)
    with db.connection() as conn:
        conn.execute("SELECT 1")
    assert db.is_pool_open()
    assert _wait_for(lambda: _backends(migrated_database) >= 1)

    db.close_pool()
    assert not db.is_pool_open()
    assert _wait_for(lambda: _backends(migrated_database) == 0)
    with pytest.raises(RuntimeError, match="not configured"):
        with db.connection():
            pass
    db.close_pool()  # idempotent


def test_reconfiguring_replaces_and_closes_the_previous_pool(migrated_database, pool_cleanup) -> None:
    db.configure_pool(migrated_database)
    first = db._pool
    db.configure_pool(migrated_database)
    assert first is not None and first.closed
    assert db.is_pool_open()


def test_unreachable_database_raises_generic_error(pool_cleanup) -> None:
    db.configure_pool(UNREACHABLE)  # must not block or raise at startup
    started = time.monotonic()
    with pytest.raises(DatabaseUnavailableError) as excinfo:
        db.ping(timeout=0.5)
    assert time.monotonic() - started < 5
    assert "TopSecretPw" not in str(excinfo.value)
    assert "127.0.0.1" not in str(excinfo.value)
    assert not db.connection_held()
