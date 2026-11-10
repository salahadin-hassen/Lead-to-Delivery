"""PostgreSQL connection pool and transaction helpers.

Usage rules for every later task:

* Reads:   ``with db.connection() as conn: ...``
* Writes:  ``with db.transaction() as conn: ...``  (commit on success,
  rollback on any exception)
* External HTTP calls must happen OUTSIDE both blocks. Integration code calls
  :func:`assert_no_connection_held` before any request; it raises
  :class:`ConnectionHeldError` if the current thread/task still holds a pooled
  connection.

All rows are returned as ``dict`` (``dict_row``). Driver connection failures
surface as :class:`DatabaseUnavailableError`, whose message never contains the
DSN.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import psycopg
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from app.errors import ConnectionHeldError, DatabaseUnavailableError

logger = logging.getLogger(__name__)

POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 5
# How long a request waits for a free pooled connection before giving up.
POOL_CHECKOUT_TIMEOUT_SECONDS = 10.0
# /api/health must answer quickly even when PostgreSQL is down.
HEALTH_CHECK_TIMEOUT_SECONDS = 3.0
# Server-side guard against runaway queries on application connections.
STATEMENT_TIMEOUT_MS = 15_000
# Visible in pg_stat_activity; lets operators (and tests) see pool connections.
APPLICATION_NAME = "ltd-api"

Connection = psycopg.Connection[DictRow]

_pool: ConnectionPool[Connection] | None = None
_connections_held: ContextVar[int] = ContextVar("ltd_db_connections_held", default=0)


def _configure_connection(conn: psycopg.Connection[Any]) -> None:
    # set_config(..., false) is session-level; commit so the pool receives the
    # connection idle, as psycopg_pool requires.
    conn.execute(
        "SELECT set_config('statement_timeout', %s, false)", (str(STATEMENT_TIMEOUT_MS),)
    )
    conn.commit()


def configure_pool(database_url: str) -> None:
    """Create and open the application pool. Does not wait for PostgreSQL.

    Opening without waiting lets the API start while the database is down;
    /api/health then reports 503 until PostgreSQL becomes reachable.
    """
    global _pool
    close_pool()
    pool: ConnectionPool[Connection] = ConnectionPool(
        conninfo=database_url,
        min_size=POOL_MIN_SIZE,
        max_size=POOL_MAX_SIZE,
        kwargs={"row_factory": dict_row, "application_name": APPLICATION_NAME},
        configure=_configure_connection,
        # Validate a connection before handing it out, so a PostgreSQL restart
        # does not surface as a failed request.
        check=ConnectionPool.check_connection,
        timeout=POOL_CHECKOUT_TIMEOUT_SECONDS,
        name="ltd-app",
        open=False,
    )
    pool.open(wait=False)
    _pool = pool


def close_pool() -> None:
    """Close the pool and release every connection. Safe to call repeatedly."""
    global _pool
    pool, _pool = _pool, None
    if pool is not None:
        pool.close()


def is_pool_open() -> bool:
    return _pool is not None and not _pool.closed


def _get_pool() -> ConnectionPool[Connection]:
    if _pool is None:
        raise RuntimeError("Database pool is not configured; call configure_pool() first.")
    return _pool


def connection_held() -> bool:
    return _connections_held.get() > 0


def assert_no_connection_held() -> None:
    """Call before any external HTTP request."""
    if connection_held():
        raise ConnectionHeldError(
            "An external call was attempted while a database connection is held; "
            "finish the database block first."
        )


@contextmanager
def _checked_out(timeout: float | None) -> Iterator[Connection]:
    token = _connections_held.set(_connections_held.get() + 1)
    try:
        try:
            with _get_pool().connection(timeout=timeout) as conn:
                yield conn
        except PoolTimeout as exc:
            raise DatabaseUnavailableError("no database connection available") from exc
        except psycopg.errors.TransactionRollback:
            # Deadlock / serialization failure: the database is fine, the
            # transaction lost a race. Callers may retry; do not mask it.
            raise
        except psycopg.OperationalError as exc:
            raise DatabaseUnavailableError("database connection failed") from exc
    finally:
        _connections_held.reset(token)


@contextmanager
def connection(timeout: float | None = None) -> Iterator[Connection]:
    """Borrow a connection. Any implicit transaction is committed on exit,
    or rolled back if the block raises."""
    with _checked_out(timeout) as conn:
        yield conn


@contextmanager
def transaction(timeout: float | None = None) -> Iterator[Connection]:
    """Borrow a connection inside one explicit transaction."""
    with _checked_out(timeout) as conn:
        with conn.transaction():
            yield conn


def constraint_name(exc: psycopg.errors.IntegrityError) -> str | None:
    """Name of the violated constraint or unique index, for mapping to domain errors."""
    return exc.diag.constraint_name


def ping(timeout: float | None = None) -> None:
    """Raise DatabaseUnavailableError unless PostgreSQL answers ``SELECT 1``."""
    with connection(timeout=timeout or HEALTH_CHECK_TIMEOUT_SECONDS) as conn:
        conn.execute("SELECT 1")


def applied_schema_version(timeout: float | None = None) -> int | None:
    """Highest applied migration version, or None when the migrations table is missing."""
    with connection(timeout=timeout or HEALTH_CHECK_TIMEOUT_SECONDS) as conn:
        exists = conn.execute("SELECT to_regclass('schema_migrations') IS NOT NULL AS ok").fetchone()
        if not exists or not exists["ok"]:
            return None
        row = conn.execute("SELECT COALESCE(max(version), 0) AS v FROM schema_migrations").fetchone()
        return int(row["v"]) if row else 0
