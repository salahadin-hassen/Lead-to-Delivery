"""Versioned SQL migrations tracked in ``schema_migrations``.

Files live in ``backend/sql`` and are named ``NNNN_snake_case_name.sql``.

Guarantees:

* Migrations run in ascending version order, each in its own transaction,
  and are recorded (version, name, SHA-256 checksum) in the same transaction.
  A failed migration is rolled back completely and not recorded.
* Applied migrations are never re-run. Editing an applied file is detected by
  checksum and stops the run: write a new migration instead.
* A PostgreSQL session advisory lock serialises concurrent runners
  (two deploys, API + CLI); the second waits, then finds nothing to do.

Command::

    python -m app.migrations            # apply pending migrations
    python -m app.migrations --status   # show applied / pending, change nothing
"""

import argparse
import hashlib
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg.rows import tuple_row

from app.errors import (
    MigrationChecksumError,
    MigrationError,
    MigrationFailedError,
    MigrationFileError,
    MigrationLockTimeoutError,
)

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"

# Arbitrary fixed 64-bit key ("LTD_MIGR"); identical for every runner.
ADVISORY_LOCK_KEY = 0x4C54445F4D494752
DEFAULT_LOCK_WAIT_SECONDS = 60.0
# Inside a migration, give up instead of queueing forever behind a table lock.
MIGRATION_LOCK_TIMEOUT = "10s"

_FILENAME_RE = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9]+(?:_[a-z0-9]+)*)\.sql$")
# Migrations are wrapped in a transaction by the runner, so they must not manage
# their own, and CONCURRENTLY operations cannot run inside one.
_FORBIDDEN_RE = re.compile(
    r"^\s*(BEGIN|COMMIT|ROLLBACK|START\s+TRANSACTION|END)\b|\bCONCURRENTLY\b",
    re.IGNORECASE | re.MULTILINE,
)

_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version      integer     PRIMARY KEY CHECK (version > 0),
    name         text        NOT NULL,
    checksum     text        NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),
    applied_at   timestamptz NOT NULL DEFAULT now(),
    execution_ms integer     NOT NULL CHECK (execution_ms >= 0)
)
"""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str
    checksum: str

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"


@dataclass(frozen=True)
class MigrationReport:
    applied: list[str]
    already_applied: list[str]

    @property
    def latest_version(self) -> int:
        labels = self.applied + self.already_applied
        return max((int(label[:4]) for label in labels), default=0)


def _strip_sql_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", "", sql)


def _strip_for_checks(sql: str) -> str:
    """Comments and dollar-quoted bodies removed: PL/pgSQL BEGIN/END are fine."""
    sql = _strip_sql_comments(sql)
    return re.sub(r"\$([A-Za-z_]*)\$.*?\$\1\$", "''", sql, flags=re.DOTALL)


def discover(sql_dir: Path = SQL_DIR) -> list[Migration]:
    """Read and validate every migration file, sorted by version."""
    if not sql_dir.is_dir():
        raise MigrationFileError(f"migration directory not found: {sql_dir}")
    found: dict[int, Migration] = {}
    for path in sorted(sql_dir.iterdir()):
        if path.suffix != ".sql":
            continue
        match = _FILENAME_RE.match(path.name)
        if match is None:
            raise MigrationFileError(
                f"invalid migration filename {path.name!r}; expected NNNN_snake_case_name.sql"
            )
        version = int(match["version"])
        if version == 0:
            raise MigrationFileError(f"{path.name}: version 0000 is reserved")
        if version in found:
            raise MigrationFileError(
                f"duplicate migration version {version:04d}: {found[version].path.name}, {path.name}"
            )
        raw = path.read_bytes()
        sql = raw.decode("utf-8")
        forbidden = _FORBIDDEN_RE.search(_strip_for_checks(sql))
        if forbidden:
            raise MigrationFileError(
                f"{path.name}: {forbidden.group(0).strip()!r} is not allowed; the runner "
                "wraps each migration in its own transaction"
            )
        if not _strip_sql_comments(sql).strip():
            raise MigrationFileError(f"{path.name} is empty")
        found[version] = Migration(
            version=version,
            name=match["name"],
            path=path,
            sql=sql,
            checksum=hashlib.sha256(raw).hexdigest(),
        )
    return [found[v] for v in sorted(found)]


def latest_version(sql_dir: Path = SQL_DIR) -> int:
    migrations = discover(sql_dir)
    return migrations[-1].version if migrations else 0


def _acquire_lock(conn: psycopg.Connection, wait_seconds: float) -> None:
    deadline = time.monotonic() + wait_seconds
    with conn.cursor(row_factory=tuple_row) as cur:
        while True:
            row = cur.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_KEY,)).fetchone()
            if row and row[0]:
                return
            if time.monotonic() >= deadline:
                raise MigrationLockTimeoutError(
                    f"another migration run held the lock for more than {wait_seconds:.0f}s"
                )
            time.sleep(0.2)


def _release_lock(conn: psycopg.Connection) -> None:
    if not conn.closed:
        conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))


def _applied(conn: psycopg.Connection) -> dict[int, tuple[str, str]]:
    with conn.cursor(row_factory=tuple_row) as cur:
        rows = cur.execute("SELECT version, name, checksum FROM schema_migrations").fetchall()
    return {version: (name, checksum) for version, name, checksum in rows}


def _check_history(migrations: list[Migration], applied: dict[int, tuple[str, str]]) -> None:
    on_disk = {m.version: m for m in migrations}
    for version, (name, checksum) in sorted(applied.items()):
        migration = on_disk.get(version)
        if migration is None:
            raise MigrationFileError(
                f"migration {version:04d}_{name} is recorded as applied but its file is missing"
            )
        if migration.checksum != checksum:
            raise MigrationChecksumError(
                f"{migration.label} was modified after it was applied; "
                "revert the edit and add a new migration instead"
            )


def run_migrations(
    conn: psycopg.Connection,
    sql_dir: Path = SQL_DIR,
    lock_wait_seconds: float = DEFAULT_LOCK_WAIT_SECONDS,
) -> MigrationReport:
    """Apply pending migrations on an autocommit connection.

    The connection's ``search_path`` decides which schema is migrated (the
    tests use this to migrate throwaway schemas).
    """
    if not conn.autocommit:
        raise ValueError("run_migrations requires an autocommit connection")
    migrations = discover(sql_dir)
    _acquire_lock(conn, lock_wait_seconds)
    try:
        conn.execute(_BOOTSTRAP_SQL)
        applied = _applied(conn)
        _check_history(migrations, applied)
        done: list[str] = []
        for migration in migrations:
            if migration.version in applied:
                continue
            started = time.monotonic()
            try:
                with conn.transaction():
                    conn.execute(f"SET LOCAL lock_timeout = '{MIGRATION_LOCK_TIMEOUT}'")
                    conn.execute(migration.sql)
                    conn.execute(
                        "INSERT INTO schema_migrations (version, name, checksum, execution_ms) "
                        "VALUES (%s, %s, %s, %s)",
                        (
                            migration.version,
                            migration.name,
                            migration.checksum,
                            int((time.monotonic() - started) * 1000),
                        ),
                    )
            except psycopg.Error as exc:
                detail = (exc.diag.message_primary or type(exc).__name__) if exc.diag else str(exc)
                raise MigrationFailedError(
                    migration.version, migration.name, exc.sqlstate, detail
                ) from exc
            done.append(migration.label)
        return MigrationReport(
            applied=done,
            already_applied=[m.label for m in migrations if m.version in applied],
        )
    finally:
        _release_lock(conn)


def status(conn: psycopg.Connection, sql_dir: Path = SQL_DIR) -> tuple[list[str], list[str]]:
    """(applied, pending) labels. Read-only: creates nothing."""
    migrations = discover(sql_dir)
    with conn.cursor(row_factory=tuple_row) as cur:
        row = cur.execute("SELECT to_regclass('schema_migrations') IS NOT NULL").fetchone()
    applied = _applied(conn) if row and row[0] else {}
    _check_history(migrations, applied)
    return (
        [m.label for m in migrations if m.version in applied],
        [m.label for m in migrations if m.version not in applied],
    )


def migrate_database(database_url: str, lock_wait_seconds: float = DEFAULT_LOCK_WAIT_SECONDS) -> MigrationReport:
    with psycopg.connect(database_url, autocommit=True, connect_timeout=10) as conn:
        return run_migrations(conn, lock_wait_seconds=lock_wait_seconds)


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.migrations")
    parser.add_argument("--status", action="store_true", help="show applied and pending migrations")
    args = parser.parse_args(argv)

    from app.config import get_settings

    url = get_settings().database_url.get_secret_value()
    try:
        if args.status:
            with psycopg.connect(url, autocommit=True, connect_timeout=10) as conn:
                dbname = conn.info.dbname
                applied, pending = status(conn)
            print(f"database: {dbname}")
            for label in applied:
                print(f"  applied  {label}")
            for label in pending:
                print(f"  PENDING  {label}")
            return 1 if pending else 0
        report = migrate_database(url)
    except MigrationError as exc:
        print(f"migration error: {exc}", file=sys.stderr)
        return 1
    except psycopg.OperationalError as exc:
        # Driver connection messages are generic enough (host/port), but never
        # include the password; still, print only the class and primary text.
        primary = exc.diag.message_primary if exc.diag and exc.diag.message_primary else ""
        print(f"cannot connect to database: {type(exc).__name__} {primary}".rstrip(), file=sys.stderr)
        return 1
    for label in report.already_applied:
        print(f"  ok       {label} (already applied)")
    for label in report.applied:
        print(f"  applied  {label}")
    print(f"schema at version {report.latest_version:04d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
