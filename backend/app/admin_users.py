"""Administrator accounts: bootstrap and recovery, run on the server.

    python -m app.admin_users create --email you@agency.example --name "Your Name"
    python -m app.admin_users list
    python -m app.admin_users reset-password --email ...   # revokes their sessions
    python -m app.admin_users reset-mfa --email ...        # new authenticator + recovery codes, revokes sessions
    python -m app.admin_users new-recovery-codes --email ...
    python -m app.admin_users revoke-sessions --email ...
    python -m app.admin_users disable --email ... | enable --email ...

Passwords are read with getpass (never from arguments, so they stay out of
shell history and process listings). Enrolling or resetting MFA shows the TOTP
secret once and requires a current code from the authenticator app before
anything is saved; recovery codes are shown once and stored only as hashes.
There are no default accounts or credentials. Uses DATABASE_URL and
ADMIN_MFA_ENCRYPTION_KEY from the environment / .env, like the API.
"""

import argparse
import getpass
import sys
from datetime import UTC, datetime

from app import db, mfa
from app.config import Settings, get_settings
from app.security import hash_password
from app.services.admin_auth import normalize_email
from app.store import admin as admin_store

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024


class AdminUserError(Exception):
    """A refusal with a message that is safe to print."""


def check_password(password: str, email: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AdminUserError(f"Use at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise AdminUserError("Password is too long.")
    if password.strip().lower() in (email, email.split("@")[0]):
        raise AdminUserError("The password must not be the email address.")


def _check_email(email: str) -> str:
    email = normalize_email(email)
    local, _, domain = email.partition("@")
    if not local or "." not in domain or any(c.isspace() for c in email):
        raise AdminUserError("Enter a valid email address.")
    return email


def _confirm(secret: str, code: str) -> int:
    step = mfa.verify_totp(secret, code, last_step=0)
    if step is None:
        raise AdminUserError("That authenticator code is not correct; nothing was saved.")
    return step


def _new_codes(conn, user_id) -> list[str]:
    codes = mfa.new_recovery_codes()
    admin_store.replace_recovery_codes(conn, user_id, [mfa.hash_recovery_code(c) for c in codes])
    return codes


def _user(conn, email: str, *, for_update: bool = True) -> dict:
    user = admin_store.find_user_by_email(conn, normalize_email(email), for_update=for_update)
    if user is None:
        raise AdminUserError("No administrator with that email.")
    return user


# ------------------------------------------------------------ operations
def create_admin(settings: Settings, *, email: str, display_name: str, password: str, totp_secret: str,
                 confirmation_code: str) -> list[str]:
    """Create an administrator with MFA already confirmed; returns the recovery codes."""
    email = _check_email(email)
    display_name = display_name.strip()
    if not 1 <= len(display_name) <= 100:
        raise AdminUserError("Enter a display name (up to 100 characters).")
    check_password(password, email)
    key = settings.mfa_key()
    step = _confirm(totp_secret, confirmation_code)
    with db.transaction() as conn:
        if admin_store.find_user_by_email(conn, email) is not None:
            raise AdminUserError("An administrator with that email already exists.")
        user_id = admin_store.create_user(conn, email=email, display_name=display_name,
                                          password_hash=hash_password(password),
                                          totp_secret_enc=mfa.encrypt_secret(totp_secret, key), totp_last_step=step)
        return _new_codes(conn, user_id)


def reset_password(settings: Settings, *, email: str, password: str) -> int:
    check_password(password, normalize_email(email))
    with db.transaction() as conn:
        user = _user(conn, email)
        admin_store.update_user(conn, user["id"], password_hash=hash_password(password))
        return admin_store.revoke_user_sessions(conn, user["id"])


def reset_mfa(settings: Settings, *, email: str, totp_secret: str, confirmation_code: str) -> list[str]:
    key = settings.mfa_key()
    step = _confirm(totp_secret, confirmation_code)
    with db.transaction() as conn:
        user = _user(conn, email)
        admin_store.update_user(conn, user["id"], totp_secret_enc=mfa.encrypt_secret(totp_secret, key),
                                totp_last_step=step)
        admin_store.revoke_user_sessions(conn, user["id"])
        return _new_codes(conn, user["id"])


def new_recovery_codes(settings: Settings, *, email: str) -> list[str]:
    with db.transaction() as conn:
        return _new_codes(conn, _user(conn, email)["id"])


def revoke_sessions(settings: Settings, *, email: str) -> int:
    with db.transaction() as conn:
        return admin_store.revoke_user_sessions(conn, _user(conn, email)["id"])


def set_disabled(settings: Settings, *, email: str, disabled: bool) -> None:
    with db.transaction() as conn:
        user = _user(conn, email)
        admin_store.update_user(conn, user["id"], disabled_at=datetime.now(UTC) if disabled else None)
        if disabled:
            admin_store.revoke_user_sessions(conn, user["id"])


def list_admins(settings: Settings) -> list[dict]:
    with db.connection() as conn:
        return admin_store.list_users(conn)


# ------------------------------------------------------------------- CLI
def _ask_password(email: str) -> str:
    first = getpass.getpass("New password (12+ characters): ")
    check_password(first, normalize_email(email))
    if getpass.getpass("Repeat: ") != first:
        raise AdminUserError("The passwords do not match.")
    return first


def _enroll(email: str) -> tuple[str, str]:
    secret = mfa.new_totp_secret()
    print("\nAdd this account to an authenticator app (scan or paste the link, or type the key):")
    print(f"  {mfa.provisioning_uri(secret, normalize_email(email))}")
    print(f"  key: {secret}\n")
    code = input("Enter the 6-digit code the app shows now: ").strip()
    return secret, code


def _show_codes(codes: list[str]) -> None:
    print("\nRecovery codes (each works once; store them offline; they are not shown again):")
    for code in codes:
        print(f"  {code}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.admin_users", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--email", required=True)
    create.add_argument("--name", required=True)
    sub.add_parser("list")
    for name in ("reset-password", "reset-mfa", "new-recovery-codes", "revoke-sessions", "disable", "enable"):
        sub.add_parser(name).add_argument("--email", required=True)
    args = parser.parse_args(argv)

    settings = get_settings()
    db.configure_pool(settings.database_url.get_secret_value())
    try:
        if args.command == "create":
            password = _ask_password(args.email)
            secret, code = _enroll(args.email)
            _show_codes(create_admin(settings, email=args.email, display_name=args.name, password=password,
                                     totp_secret=secret, confirmation_code=code))
            print(f"\nAdministrator {normalize_email(args.email)} created.")
        elif args.command == "list":
            for row in list_admins(settings):
                state = "disabled" if row["disabled_at"] else "active"
                print(f"{row['email']}\t{row['display_name']}\t{state}\tlast login {row['last_login_at'] or '-'}"
                      f"\trecovery codes left {row['recovery_codes_left']}\tactive sessions {row['active_sessions']}")
        elif args.command == "reset-password":
            revoked = reset_password(settings, email=args.email, password=_ask_password(args.email))
            print(f"Password changed; {revoked} session(s) signed out.")
        elif args.command == "reset-mfa":
            secret, code = _enroll(args.email)
            _show_codes(reset_mfa(settings, email=args.email, totp_secret=secret, confirmation_code=code))
            print("\nAuthenticator replaced; all sessions signed out.")
        elif args.command == "new-recovery-codes":
            _show_codes(new_recovery_codes(settings, email=args.email))
            print("\nPrevious recovery codes no longer work.")
        elif args.command == "revoke-sessions":
            print(f"{revoke_sessions(settings, email=args.email)} session(s) signed out.")
        else:
            set_disabled(settings, email=args.email, disabled=args.command == "disable")
            print(f"Administrator {args.command}d.")
    except (AdminUserError, mfa.MfaKeyError) as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
