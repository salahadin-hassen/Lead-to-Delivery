"""Security primitives shared by later tasks (admin login, portal tokens, webhooks).

Standard library only. Nothing here touches the database or the network.

* Passwords: scrypt, stored as ``scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>``.
* Opaque tokens (portal links, admin sessions): 256-bit random, URL-safe; only
  the SHA-256 hex digest is ever stored.
* Comparisons of secrets use ``hmac.compare_digest``.

Generate an admin password hash for ``ADMIN_PASSWORD_HASH`` with::

    python -m app.security hash-password
"""

import base64
import binascii
import hashlib
import hmac
import re
import secrets
import sys

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SALT_BYTES = 16
_MAXMEM = 64 * 1024 * 1024

_HASH_RE = re.compile(
    r"^scrypt\$(?P<n>\d+)\$(?P<r>\d+)\$(?P<p>\d+)\$(?P<salt>[A-Za-z0-9+/=]+)\$(?P<hash>[A-Za-z0-9+/=]+)$"
)
TOKEN_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

TOKEN_BYTES = 32  # 256 bits of entropy


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=_MAXMEM, dklen=_SCRYPT_DKLEN
    )


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = _scrypt(password, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    b64 = lambda raw: base64.b64encode(raw).decode("ascii")  # noqa: E731
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${b64(salt)}${b64(digest)}"


def is_valid_password_hash(stored: str) -> bool:
    match = _HASH_RE.match(stored)
    if match is None:
        return False
    try:
        base64.b64decode(match["salt"], validate=True)
        base64.b64decode(match["hash"], validate=True)
    except (binascii.Error, ValueError):
        return False
    n = int(match["n"])
    return n >= 2 and n & (n - 1) == 0 and int(match["r"]) >= 1 and int(match["p"]) >= 1


def verify_password(password: str, stored: str | None) -> bool:
    """True only for a correct password. A missing or malformed hash is False.

    There is deliberately no "no hash configured, allow everyone" path.
    """
    if not password or not stored:
        return False
    match = _HASH_RE.match(stored)
    if match is None:
        return False
    try:
        salt = base64.b64decode(match["salt"], validate=True)
        expected = base64.b64decode(match["hash"], validate=True)
        actual = _scrypt(password, salt, int(match["n"]), int(match["r"]), int(match["p"]))
    except (binascii.Error, ValueError):
        return False
    return hmac.compare_digest(actual, expected)


def generate_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# --- Webhook HMAC -----------------------------------------------------------
#
#   X-LTD-Timestamp: <unix seconds>
#   X-LTD-Signature: sha256=<hex HMAC-SHA256(secret, timestamp + "." + raw_body)>

MIN_WEBHOOK_SECRET_LENGTH = 32
WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS = 300
_WEBHOOK_TIMESTAMP_RE = re.compile(r"^[0-9]{1,12}$")
_WEBHOOK_SIGNATURE_RE = re.compile(r"^sha256=([0-9a-fA-F]{64})$")


def webhook_signature(secret: str, timestamp: str, body: bytes) -> str:
    """The ``X-LTD-Signature`` header value for this timestamp and exact body."""
    mac = hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def verify_webhook_signature(
    *,
    secret: str,
    timestamp: str | None,
    signature: str | None,
    body: bytes,
    now: float,
    tolerance_seconds: int = WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS,
) -> None:
    """Raise WebhookAuthError unless the signature over the RAW body is valid.

    Codes: missing_signature, malformed_timestamp, timestamp_out_of_window,
    malformed_signature, invalid_signature. The body is never re-serialised.
    """
    from app.errors import WebhookAuthError

    if not timestamp or not signature:
        raise WebhookAuthError("missing_signature")
    timestamp = timestamp.strip()
    if not _WEBHOOK_TIMESTAMP_RE.match(timestamp):
        raise WebhookAuthError("malformed_timestamp")
    if abs(now - int(timestamp)) > tolerance_seconds:
        raise WebhookAuthError("timestamp_out_of_window")
    match = _WEBHOOK_SIGNATURE_RE.match(signature.strip())
    if match is None:
        raise WebhookAuthError("malformed_signature")
    expected = webhook_signature(secret, timestamp, body).removeprefix("sha256=")
    if not hmac.compare_digest(expected, match.group(1).lower()):
        raise WebhookAuthError("invalid_signature")


def _main(argv: list[str]) -> int:
    if argv != ["hash-password"]:
        print("usage: python -m app.security hash-password", file=sys.stderr)
        return 2
    import getpass

    first = getpass.getpass("Admin password: ")
    if len(first) < 12:
        print("Refusing: use at least 12 characters.", file=sys.stderr)
        return 1
    if getpass.getpass("Repeat: ") != first:
        print("Passwords do not match.", file=sys.stderr)
        return 1
    print(hash_password(first))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
