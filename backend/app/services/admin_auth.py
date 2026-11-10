"""Single-administrator authentication: password login, server-side sessions,
durable login throttling.

* Login verifies the password with security.verify_password against
  ADMIN_PASSWORD_HASH. With no hash configured, login is refused (503); there
  is no unauthenticated mode.
* A session is a 256-bit random token; only its SHA-256 is stored in
  admin_sessions (12-hour lifetime). The raw token travels only in an
  HttpOnly cookie.
* Throttling (admin_login_attempts): at most 5 failures per source address
  since its last success and 50 failures overall, within 15 minutes. The
  source is the TCP peer address, never X-Forwarded-For.
"""

import logging
import math
from dataclasses import dataclass
from datetime import datetime

from app import db
from app.config import Settings
from app.errors import AdminAuthError, AdminLoginThrottledError, AdminNotConfiguredError
from app.security import generate_token, hash_token, verify_password
from app.store import admin as admin_store

logger = logging.getLogger("app.admin")

SESSION_LIFETIME_SECONDS = 12 * 3600
THROTTLE_WINDOW_SECONDS = 15 * 60
MAX_FAILURES_PER_SOURCE = 5
MAX_FAILURES_GLOBAL = 50


@dataclass(frozen=True)
class AdminSession:
    session_id: object
    expires_at: datetime


def _retry_after(oldest: datetime | None, now: datetime) -> int:
    if oldest is None:
        return THROTTLE_WINDOW_SECONDS
    remaining = THROTTLE_WINDOW_SECONDS - (now - oldest).total_seconds()
    return max(1, math.ceil(remaining))


def login(password: str, *, source_key: str, settings: Settings) -> tuple[str, datetime]:
    """Return (raw session token, expires_at) or raise.

    AdminNotConfiguredError, AdminLoginThrottledError(retry_after),
    AdminAuthError("invalid_credentials").
    """
    stored = settings.admin_password_hash.get_secret_value() if settings.admin_password_hash else None
    if not stored:
        raise AdminNotConfiguredError()
    source_key = (source_key or "unknown")[:100]
    with db.transaction() as conn:
        admin_store.lock_login_source(conn, source_key)
        counts = admin_store.failure_counts(conn, source_key, THROTTLE_WINDOW_SECONDS)
        if counts["source_failures"] >= MAX_FAILURES_PER_SOURCE:
            raise AdminLoginThrottledError(_retry_after(counts["source_oldest"], counts["now"]))
        if counts["global_failures"] >= MAX_FAILURES_GLOBAL:
            raise AdminLoginThrottledError(_retry_after(counts["global_oldest"], counts["now"]))
        ok = verify_password(password, stored)
        admin_store.record_login_attempt(conn, source_key, ok)
        if not ok:
            failed = True
        else:
            failed = False
            raw = generate_token()
            expires_at = admin_store.create_session(conn, hash_token(raw), SESSION_LIFETIME_SECONDS)
    # The failure is committed (outside the with block) before raising.
    if failed:
        logger.warning("admin login failed")
        raise AdminAuthError("invalid_credentials")
    logger.info("admin login succeeded")
    return raw, expires_at


def authenticate(raw_token: str | None) -> AdminSession:
    if not raw_token or len(raw_token) > 200:
        raise AdminAuthError("admin_auth_required")
    with db.transaction() as conn:
        row = admin_store.find_active_session(conn, hash_token(raw_token))
        if row is None:
            raise AdminAuthError("admin_auth_required")
        admin_store.touch_session(conn, row["id"])
    return AdminSession(session_id=row["id"], expires_at=row["expires_at"])


def logout(raw_token: str | None) -> None:
    if not raw_token or len(raw_token) > 200:
        return
    with db.transaction() as conn:
        if admin_store.revoke_session(conn, hash_token(raw_token)):
            logger.info("admin logged out")
