"""Administrator authentication: individual accounts, password + mandatory
TOTP (or a single-use recovery code), server-side sessions, durable throttling.

* Login needs email, password AND a second factor in one request. Any failure
  (unknown email, wrong password, wrong or replayed code, disabled account)
  is the same ``invalid_credentials``; an unknown email still costs one
  scrypt verification so timing does not reveal which accounts exist.
* A TOTP code is accepted at most once (``totp_last_step``). A recovery code
  is accepted once and then marked used.
* A session is a 256-bit random token; only its SHA-256 is stored. It belongs
  to one administrator, lasts at most 12 hours, ends after 60 minutes without
  activity, and ends at once when the account is disabled or its sessions are
  revoked. Each login issues a new token (no session fixation).
* Throttling (admin_login_attempts): within 15 minutes, at most 5 failures per
  source address and 5 per account name since their last success, and 50
  overall. The source is the TCP peer as seen by the API (behind a trusted
  proxy, uvicorn's --forwarded-allow-ips sets it from X-Forwarded-For).
"""

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app import db, mfa
from app.config import Settings
from app.errors import AdminAuthError, AdminLoginThrottledError, AdminNotConfiguredError
from app.security import generate_token, hash_password, hash_token, verify_password
from app.store import admin as admin_store

logger = logging.getLogger("app.admin")

SESSION_LIFETIME_SECONDS = 12 * 3600
SESSION_IDLE_SECONDS = 60 * 60
THROTTLE_WINDOW_SECONDS = 15 * 60
MAX_FAILURES_PER_SOURCE = 5
MAX_FAILURES_PER_ACCOUNT = 5
MAX_FAILURES_GLOBAL = 50

# Verified against when the email is unknown, so every attempt costs one scrypt.
_DUMMY_HASH = hash_password("timing-equaliser-not-a-real-password")


@dataclass(frozen=True)
class AdminSession:
    session_id: UUID
    admin_user_id: UUID
    email: str
    display_name: str
    expires_at: datetime


def normalize_email(email: str) -> str:
    return email.strip().lower()[:254]


def _retry_after(oldest: datetime | None, now: datetime) -> int:
    if oldest is None:
        return THROTTLE_WINDOW_SECONDS
    remaining = THROTTLE_WINDOW_SECONDS - (now - oldest).total_seconds()
    return max(1, math.ceil(remaining))


def _second_factor_ok(conn, user: dict, code: str, key: bytes) -> tuple[bool, str | None]:
    """(accepted, kind). Updates totp_last_step or marks the recovery code used."""
    if mfa.looks_like_recovery_code(code):
        if admin_store.use_recovery_code(conn, user["id"], mfa.hash_recovery_code(code)):
            return True, "recovery_code"
        return False, None
    secret = mfa.decrypt_secret(user["totp_secret_enc"], key)
    step = mfa.verify_totp(secret, code, last_step=user["totp_last_step"])
    if step is None:
        return False, None
    admin_store.update_user(conn, user["id"], totp_last_step=step)
    return True, "totp"


def login(email: str, password: str, code: str, *, source_key: str, settings: Settings) -> tuple[str, datetime]:
    """Return (raw session token, expires_at) or raise.

    AdminNotConfiguredError (no MFA key), AdminLoginThrottledError(retry_after),
    AdminAuthError("invalid_credentials").
    """
    try:
        key = settings.mfa_key()
    except mfa.MfaKeyError:
        raise AdminNotConfiguredError() from None
    account_key = normalize_email(email)
    source_key = (source_key or "unknown")[:100]
    used = None
    with db.transaction() as conn:
        admin_store.lock_login_source(conn, source_key)
        admin_store.lock_login_account(conn, account_key)
        counts = admin_store.failure_counts(conn, source_key, THROTTLE_WINDOW_SECONDS)
        account = admin_store.account_failures(conn, account_key, THROTTLE_WINDOW_SECONDS)
        if counts["source_failures"] >= MAX_FAILURES_PER_SOURCE:
            raise AdminLoginThrottledError(_retry_after(counts["source_oldest"], counts["now"]))
        if account["failures"] >= MAX_FAILURES_PER_ACCOUNT:
            raise AdminLoginThrottledError(_retry_after(account["oldest"], account["now"]))
        if counts["global_failures"] >= MAX_FAILURES_GLOBAL:
            raise AdminLoginThrottledError(_retry_after(counts["global_oldest"], counts["now"]))

        user = admin_store.find_user_by_email(conn, account_key, for_update=True)
        password_ok = verify_password(password, user["password_hash"] if user else _DUMMY_HASH)
        ok = bool(user) and password_ok and user["disabled_at"] is None
        if ok:
            try:
                ok, used = _second_factor_ok(conn, user, code, key)
            except mfa.MfaKeyError:
                logger.error("admin login refused: TOTP secret cannot be decrypted (wrong ADMIN_MFA_ENCRYPTION_KEY?)")
                raise AdminNotConfiguredError() from None
        admin_store.record_login_attempt(conn, source_key, ok, account_key)
        if ok:
            raw = generate_token()
            expires_at = admin_store.create_session(conn, hash_token(raw), SESSION_LIFETIME_SECONDS, user["id"])
            admin_store.update_user(conn, user["id"], last_login_at=datetime.now().astimezone())
            remaining = admin_store.recovery_codes_left(conn, user["id"]) if used == "recovery_code" else None
    # A failure is committed (outside the with block) before raising.
    if not ok:
        logger.warning("admin login failed")
        raise AdminAuthError("invalid_credentials")
    if used == "recovery_code":
        logger.warning("admin login with a recovery code (admin %s, %s codes left)", user["id"], remaining)
    else:
        logger.info("admin login succeeded (admin %s)", user["id"])
    return raw, expires_at


def authenticate(raw_token: str | None) -> AdminSession:
    if not raw_token or len(raw_token) > 200:
        raise AdminAuthError("admin_auth_required")
    with db.transaction() as conn:
        row = admin_store.find_active_session(conn, hash_token(raw_token), SESSION_IDLE_SECONDS)
        if row is None:
            raise AdminAuthError("admin_auth_required")
        admin_store.touch_session(conn, row["id"])
    return AdminSession(session_id=row["id"], admin_user_id=row["admin_user_id"], email=row["email"],
                        display_name=row["display_name"], expires_at=row["expires_at"])


def logout(raw_token: str | None) -> None:
    if not raw_token or len(raw_token) > 200:
        return
    with db.transaction() as conn:
        if admin_store.revoke_session(conn, hash_token(raw_token)):
            logger.info("admin logged out")


def logout_everywhere(session: AdminSession) -> int:
    with db.transaction() as conn:
        count = admin_store.revoke_user_sessions(conn, session.admin_user_id)
    logger.info("admin %s revoked all %s of their sessions", session.admin_user_id, count)
    return count
