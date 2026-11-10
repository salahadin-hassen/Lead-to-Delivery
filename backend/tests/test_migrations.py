"""Versioned migrations: ordering, tracking, repeatability, locking, failures.

Each test migrates its own throwaway schema (search_path), so the session
schema used by other tests is never touched.
"""

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import psycopg
import pytest

from app.errors import (
    MigrationChecksumError,
    MigrationFailedError,
    MigrationFileError,
    MigrationLockTimeoutError,
)
from app.migrations import ADVISORY_LOCK_KEY, SQL_DIR, discover, run_migrations
from tests.conftest import connect_to_schema

BACKEND_DIR = Path(__file__).resolve().parents[1]
ALL_LABELS = ["0001_core_schema", "0002_events_and_integrations", "0003_seed_project_templates",
              "0004_admin_login_attempts", "0005_admin_accounts_mfa"]
EXPECTED_TABLES = {
    "schema_migrations", "clients", "onboarding_invitations", "client_access_tokens",
    "onboarding_submissions", "projects", "project_templates", "milestones", "tasks",
    "webhook_events", "integration_jobs", "integration_attempts", "activity_events",
    "admin_sessions", "admin_login_attempts", "admin_users", "admin_recovery_codes",
}


def _object_counts(conn: psycopg.Connection, schema: str) -> dict[str, int]:
    """Tables, indexes, constraints, triggers and functions in one schema."""
    row = conn.execute(
        """
        SELECT
          (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = %(s)s AND c.relkind = 'r') AS tables,
          (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = %(s)s AND c.relkind = 'i') AS indexes,
          (SELECT count(*) FROM pg_constraint k JOIN pg_namespace n ON n.oid = k.connamespace
             WHERE n.nspname = %(s)s) AS constraints,
          (SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
             JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = %(s)s AND NOT t.tgisinternal) AS triggers,
          (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname = %(s)s) AS functions,
          (SELECT count(*) FROM %(t)s) AS templates
        """.replace("%(t)s", f'"{schema}".project_templates'),
        {"s": schema},
    ).fetchone()
    return dict(zip(("tables", "indexes", "constraints", "triggers", "functions", "templates"), row))


def _recorded(conn: psycopg.Connection) -> list[tuple[int, str]]:
    return conn.execute("SELECT version, name FROM schema_migrations ORDER BY version").fetchall()


def test_fresh_schema_applies_every_migration_in_order(migrated_database, scratch_schema) -> None:
    with connect_to_schema(migrated_database, scratch_schema) as conn:
        report = run_migrations(conn)
        assert report.applied == ALL_LABELS
        assert report.already_applied == []
        assert report.latest_version == 5
        assert _recorded(conn) == [
            (1, "core_schema"), (2, "events_and_integrations"), (3, "seed_project_templates"),
            (4, "admin_login_attempts"), (5, "admin_accounts_mfa"),
        ]
        checksums = conn.execute("SELECT checksum FROM schema_migrations ORDER BY version").fetchall()
        assert [c[0] for c in checksums] == [m.checksum for m in discover()]
        tables = {
            r[0] for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = %s", (scratch_schema,)
            ).fetchall()
        }
        assert tables == EXPECTED_TABLES


def test_second_run_applies_nothing_and_creates_no_duplicates(migrated_database, scratch_schema) -> None:
    with connect_to_schema(migrated_database, scratch_schema) as conn:
        run_migrations(conn)
        before = _object_counts(conn, scratch_schema)
        report = run_migrations(conn)
        after = _object_counts(conn, scratch_schema)
    assert report.applied == []
    assert report.already_applied == ALL_LABELS
    assert after == before
    assert before["tables"] == len(EXPECTED_TABLES)
    assert before["templates"] == 3


def test_failed_migration_is_rolled_back_and_not_recorded(
    migrated_database, scratch_schema, tmp_path: Path
) -> None:
    shutil.copy(SQL_DIR / "0001_core_schema.sql", tmp_path)
    bad = tmp_path / "0002_broken.sql"
    bad.write_text("CREATE TABLE partial_marker (id int);\nSELECT 1 / 0;\n")
    with connect_to_schema(migrated_database, scratch_schema) as conn:
        with pytest.raises(MigrationFailedError) as excinfo:
            run_migrations(conn, sql_dir=tmp_path)
        err = excinfo.value
        assert (err.version, err.name, err.sqlstate) == (2, "broken", "22012")
        assert "0002_broken" in str(err) and "rolled back" in str(err)
        assert _recorded(conn) == [(1, "core_schema")]
        assert conn.execute("SELECT to_regclass('partial_marker')").fetchone()[0] is None
        # The lock was released: fixing the file lets the next run continue.
        bad.write_text("CREATE TABLE partial_marker (id int);\n")
        assert run_migrations(conn, sql_dir=tmp_path).applied == ["0002_broken"]


def test_editing_an_applied_migration_is_detected(migrated_database, scratch_schema, tmp_path: Path) -> None:
    shutil.copy(SQL_DIR / "0001_core_schema.sql", tmp_path)
    with connect_to_schema(migrated_database, scratch_schema) as conn:
        run_migrations(conn, sql_dir=tmp_path)
        edited = tmp_path / "0001_core_schema.sql"
        edited.write_text(edited.read_text() + "\n-- sneaky edit\n")
        with pytest.raises(MigrationChecksumError, match="0001_core_schema"):
            run_migrations(conn, sql_dir=tmp_path)


def test_missing_applied_migration_file_is_detected(migrated_database, scratch_schema, tmp_path: Path) -> None:
    shutil.copy(SQL_DIR / "0001_core_schema.sql", tmp_path)
    with connect_to_schema(migrated_database, scratch_schema) as conn:
        run_migrations(conn, sql_dir=tmp_path)
        (tmp_path / "0001_core_schema.sql").unlink()
        (tmp_path / "0002_next.sql").write_text("SELECT 1;\n")
        with pytest.raises(MigrationFileError, match="file is missing"):
            run_migrations(conn, sql_dir=tmp_path)


def test_runner_waits_for_the_advisory_lock_then_times_out(migrated_database, scratch_schema) -> None:
    with psycopg.connect(migrated_database, autocommit=True) as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
        try:
            with connect_to_schema(migrated_database, scratch_schema) as conn:
                with pytest.raises(MigrationLockTimeoutError):
                    run_migrations(conn, lock_wait_seconds=0.5)
                # Nothing was applied while the lock was held.
                assert conn.execute("SELECT to_regclass('schema_migrations')").fetchone()[0] is None
        finally:
            holder.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))


def test_concurrent_runners_apply_each_migration_exactly_once(migrated_database, scratch_schema) -> None:
    reports, errors = [], []
    barrier = threading.Barrier(3)

    def worker() -> None:
        try:
            with connect_to_schema(migrated_database, scratch_schema) as conn:
                barrier.wait()
                reports.append(run_migrations(conn, lock_wait_seconds=30))
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert errors == []
    applied = sorted(label for r in reports for label in r.applied)
    assert applied == ALL_LABELS
    with connect_to_schema(migrated_database, scratch_schema) as conn:
        assert len(_recorded(conn)) == 5


def test_runner_requires_an_autocommit_connection(migrated_database, scratch_schema) -> None:
    with psycopg.connect(migrated_database, options=f"-c search_path={scratch_schema}") as conn:
        with pytest.raises(ValueError, match="autocommit"):
            run_migrations(conn)


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        ("001_short.sql", "SELECT 1;", "invalid migration filename"),
        ("0001_Bad-Name.sql", "SELECT 1;", "invalid migration filename"),
        ("0000_zero.sql", "SELECT 1;", "reserved"),
        ("0001_tx.sql", "BEGIN;\nSELECT 1;\nCOMMIT;", "not allowed"),
        ("0001_idx.sql", "CREATE INDEX CONCURRENTLY i ON t (c);", "not allowed"),
        ("0001_empty.sql", "-- nothing here\n", "is empty"),
    ],
)
def test_discover_rejects_invalid_files(tmp_path: Path, filename: str, content: str, message: str) -> None:
    (tmp_path / filename).write_text(content)
    with pytest.raises(MigrationFileError, match=message):
        discover(tmp_path)


def test_discover_rejects_duplicate_versions(tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_text("SELECT 1;")
    (tmp_path / "0001_b.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationFileError, match="duplicate migration version"):
        discover(tmp_path)


def test_plpgsql_bodies_with_begin_end_are_allowed(tmp_path: Path) -> None:
    (tmp_path / "0001_fn.sql").write_text(
        "CREATE FUNCTION f() RETURNS int LANGUAGE plpgsql AS $$\nBEGIN\n  RETURN 1;\nEND;\n$$;\n"
    )
    assert [m.label for m in discover(tmp_path)] == ["0001_fn"]


def _cli(*args: str, database_url: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": database_url}
    return subprocess.run(
        [sys.executable, "-m", "app.migrations", *args],
        cwd=BACKEND_DIR, env=env, capture_output=True, text=True, timeout=60,
    )


def test_cli_status_and_apply_on_migrated_database(migrated_database) -> None:
    status = _cli("--status", database_url=migrated_database)
    assert status.returncode == 0, status.stderr
    assert "database: ltd_test" in status.stdout
    assert status.stdout.count("applied") == 5 and "PENDING" not in status.stdout

    apply = _cli(database_url=migrated_database)
    assert apply.returncode == 0, apply.stderr
    assert "already applied" in apply.stdout and "schema at version 0005" in apply.stdout


def test_cli_reports_connection_failure_without_secrets() -> None:
    result = _cli(database_url="postgresql://ltd:TopSecretPw@127.0.0.1:1/ltd_test?connect_timeout=2",
                  )
    assert result.returncode == 1
    assert "cannot connect to database" in result.stderr
    assert "TopSecretPw" not in result.stderr + result.stdout
