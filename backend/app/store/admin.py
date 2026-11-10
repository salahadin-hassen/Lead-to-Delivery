"""SQL for administrator accounts, MFA recovery codes, sessions and login throttling."""

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


def account_failures(conn: Connection, account_key: str, window_seconds: int) -> dict[str, Any]:
    """Failed logins for one account name (email as typed) since its last success."""
    return conn.execute(
        """
        WITH win AS (SELECT now() - make_interval(secs => %(w)s) AS since),
        last_ok AS (SELECT max(created_at) AS at FROM admin_login_attempts, win
                    WHERE account_key = %(a)s AND succeeded AND created_at > win.since)
        SELECT count(*) AS failures, min(a.created_at) AS oldest, now() AS now
        FROM admin_login_attempts a, win, last_ok
        WHERE a.account_key = %(a)s AND NOT a.succeeded AND a.created_at > win.since
          AND (last_ok.at IS NULL OR a.created_at > last_ok.at)
        """,
        {"a": account_key, "w": window_seconds},
    ).fetchone()


def lock_login_account(conn: Connection, account_key: str) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('ltd-admin-account:' || %s, 0))", (account_key,))


def record_login_attempt(conn: Connection, source_key: str, succeeded: bool, account_key: str | None = None) -> None:
    conn.execute("INSERT INTO admin_login_attempts (source_key, succeeded, account_key) VALUES (%s, %s, %s)",
                 (source_key, succeeded, account_key))


# ------------------------------------------------------------- accounts
def find_user_by_email(conn: Connection, email: str, *, for_update: bool = False) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT id, email, display_name, password_hash, totp_secret_enc, totp_last_step, disabled_at "
        "FROM admin_users WHERE email = %s" + (" FOR UPDATE" if for_update else ""),
        (email,),
    ).fetchone()


def list_users(conn: Connection) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT u.id, u.email, u.display_name, u.disabled_at, u.last_login_at, u.created_at, "
        "(SELECT count(*) FROM admin_recovery_codes r WHERE r.admin_user_id = u.id AND r.used_at IS NULL) "
        "  AS recovery_codes_left, "
        "(SELECT count(*) FROM admin_sessions s WHERE s.admin_user_id = u.id AND s.revoked_at IS NULL "
        "   AND s.expires_at > now()) AS active_sessions "
        "FROM admin_users u ORDER BY u.email"
    ).fetchall()


def create_user(conn: Connection, *, email: str, display_name: str, password_hash: str, totp_secret_enc: str,
                totp_last_step: int) -> UUID:
    return conn.execute(
        "INSERT INTO admin_users (email, display_name, password_hash, totp_secret_enc, totp_last_step) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (email, display_name, password_hash, totp_secret_enc, totp_last_step),
    ).fetchone()["id"]


def update_user(conn: Connection, user_id: UUID, **fields: Any) -> int:
    allowed = {"password_hash", "totp_secret_enc", "totp_last_step", "disabled_at", "last_login_at", "display_name"}
    assert fields and set(fields) <= allowed, fields
    sets = ", ".join(f"{name} = %({name})s" for name in fields)
    return conn.execute(f"UPDATE admin_users SET {sets} WHERE id = %(id)s", {**fields, "id": user_id}).rowcount


def replace_recovery_codes(conn: Connection, user_id: UUID, code_hashes: list[str]) -> None:
    conn.execute("DELETE FROM admin_recovery_codes WHERE admin_user_id = %s", (user_id,))
    for code_hash in code_hashes:
        conn.execute("INSERT INTO admin_recovery_codes (admin_user_id, code_hash) VALUES (%s, %s)",
                     (user_id, code_hash))


def use_recovery_code(conn: Connection, user_id: UUID, code_hash: str) -> bool:
    """Mark an unused code of THIS account as used. Single use, race-free."""
    return conn.execute(
        "UPDATE admin_recovery_codes SET used_at = now() "
        "WHERE admin_user_id = %s AND code_hash = %s AND used_at IS NULL",
        (user_id, code_hash),
    ).rowcount == 1


def recovery_codes_left(conn: Connection, user_id: UUID) -> int:
    return conn.execute("SELECT count(*) AS n FROM admin_recovery_codes WHERE admin_user_id = %s AND used_at IS NULL",
                        (user_id,)).fetchone()["n"]


# ------------------------------------------------------------- sessions
def create_session(conn: Connection, token_hash: str, lifetime_seconds: int, admin_user_id: UUID) -> datetime:
    return conn.execute(
        "INSERT INTO admin_sessions (token_hash, expires_at, admin_user_id, last_seen_at) "
        "VALUES (%s, now() + make_interval(secs => %s), %s, now()) RETURNING expires_at",
        (token_hash, lifetime_seconds, admin_user_id),
    ).fetchone()["expires_at"]


def find_active_session(conn: Connection, token_hash: str, idle_seconds: int) -> dict[str, Any] | None:
    """A live session of an enabled administrator, not idle for longer than idle_seconds."""
    return conn.execute(
        "SELECT s.id, s.expires_at, s.last_seen_at, u.id AS admin_user_id, u.email, u.display_name "
        "FROM admin_sessions s JOIN admin_users u ON u.id = s.admin_user_id "
        "WHERE s.token_hash = %s AND s.revoked_at IS NULL AND s.expires_at > now() AND u.disabled_at IS NULL "
        "  AND coalesce(s.last_seen_at, s.created_at) > now() - make_interval(secs => %s)",
        (token_hash, idle_seconds),
    ).fetchone()


def revoke_user_sessions(conn: Connection, user_id: UUID) -> int:
    return conn.execute(
        "UPDATE admin_sessions SET revoked_at = now() WHERE admin_user_id = %s AND revoked_at IS NULL",
        (user_id,),
    ).rowcount


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
