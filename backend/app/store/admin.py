"""SQL for administrator sessions and login throttling."""

from datetime import datetime
from typing import Any
from uuid import UUID

from app.db import Connection


def lock_login_source(conn: Connection, source_key: str) -> None:
    """Serialise login attempts from one source (transaction-scoped advisory lock)."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('ltd-admin-login:' || %s, 0))", (source_key,))


def failure_counts(conn: Connection, source_key: str, window_seconds: int) -> dict[str, Any]:
    """Failures in the window: for this source since its last success, and overall."""
    return conn.execute(
        """
        WITH win AS (SELECT now() - make_interval(secs => %(w)s) AS since),
        last_ok AS (
            SELECT max(created_at) AS at FROM admin_login_attempts, win
            WHERE source_key = %(s)s AND succeeded AND created_at > win.since
        )
        SELECT
          (SELECT count(*) FROM admin_login_attempts a, win, last_ok
            WHERE a.source_key = %(s)s AND NOT a.succeeded AND a.created_at > win.since
              AND (last_ok.at IS NULL OR a.created_at > last_ok.at)) AS source_failures,
          (SELECT min(a.created_at) FROM admin_login_attempts a, win, last_ok
            WHERE a.source_key = %(s)s AND NOT a.succeeded AND a.created_at > win.since
              AND (last_ok.at IS NULL OR a.created_at > last_ok.at)) AS source_oldest,
          (SELECT count(*) FROM admin_login_attempts a, win
            WHERE NOT a.succeeded AND a.created_at > win.since) AS global_failures,
          (SELECT min(a.created_at) FROM admin_login_attempts a, win
            WHERE NOT a.succeeded AND a.created_at > win.since) AS global_oldest,
          now() AS now
        """,
        {"s": source_key, "w": window_seconds},
    ).fetchone()


def record_login_attempt(conn: Connection, source_key: str, succeeded: bool) -> None:
    conn.execute("INSERT INTO admin_login_attempts (source_key, succeeded) VALUES (%s, %s)", (source_key, succeeded))


def create_session(conn: Connection, token_hash: str, lifetime_seconds: int) -> datetime:
    return conn.execute(
        "INSERT INTO admin_sessions (token_hash, expires_at) VALUES (%s, now() + make_interval(secs => %s)) "
        "RETURNING expires_at",
        (token_hash, lifetime_seconds),
    ).fetchone()["expires_at"]


def find_active_session(conn: Connection, token_hash: str) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT id, expires_at, last_seen_at FROM admin_sessions "
        "WHERE token_hash = %s AND revoked_at IS NULL AND expires_at > now()",
        (token_hash,),
    ).fetchone()


def touch_session(conn: Connection, session_id: UUID) -> None:
    """Record activity at most once a minute (avoids a write per request)."""
    conn.execute(
        "UPDATE admin_sessions SET last_seen_at = now() WHERE id = %s "
        "AND (last_seen_at IS NULL OR last_seen_at < now() - interval '1 minute')",
        (session_id,),
    )


def revoke_session(conn: Connection, token_hash: str) -> int:
    return conn.execute(
        "UPDATE admin_sessions SET revoked_at = now() WHERE token_hash = %s AND revoked_at IS NULL",
        (token_hash,),
    ).rowcount
