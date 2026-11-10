"""Administrator MFA primitives: TOTP (RFC 6238), recovery codes, and
encryption of TOTP secrets at rest. Nothing here touches the database.

* TOTP: SHA-1, 6 digits, 30-second steps (what every authenticator app
  supports). One step of clock drift is accepted either side. The caller
  stores the highest accepted step so a code is never accepted twice.
* TOTP secrets are 160-bit random, base32. In the database they are stored
  only as ``v1:<base64(nonce || AES-256-GCM ciphertext)>`` under
  ADMIN_MFA_ENCRYPTION_KEY (32 random bytes, base64), which is configuration
  and never lives in the database.
* Recovery codes: 10 per account, single use, ``xxxx-xxxx-xxxx`` from a
  31-character alphabet (~74 bits each). Only their SHA-256 is stored.

Generate a key for ADMIN_MFA_ENCRYPTION_KEY with::

    python -m app.mfa generate-key
"""

import base64
import binascii
import hashlib
import hmac
import secrets
import struct
import sys
import time
from urllib.parse import quote

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

TOTP_DIGITS = 6
TOTP_PERIOD = 30
TOTP_DRIFT_STEPS = 1
RECOVERY_CODE_COUNT = 10
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i
_NONCE_BYTES = 12
_AAD = b"ltd-admin-totp-v1"


class MfaKeyError(ValueError):
    """ADMIN_MFA_ENCRYPTION_KEY is missing or malformed, or a secret cannot be decrypted."""


# ---------------------------------------------------------------- TOTP
def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii")


def _hotp(secret_b32: str, counter: int) -> str:
    key = base64.b32decode(secret_b32, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**TOTP_DIGITS).zfill(TOTP_DIGITS)


def current_step(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // TOTP_PERIOD)


def totp_code(secret_b32: str, step: int | None = None) -> str:
    return _hotp(secret_b32, current_step() if step is None else step)


def verify_totp(secret_b32: str, code: str, *, last_step: int, now: float | None = None) -> int | None:
    """The matching step (to store as the new last_step), or None.

    Codes for a step at or before ``last_step`` are refused (no replay).
    """
    code = code.strip().replace(" ", "")
    if len(code) != TOTP_DIGITS or not code.isdigit():
        return None
    step = current_step(now)
    for candidate in range(step - TOTP_DRIFT_STEPS, step + TOTP_DRIFT_STEPS + 1):
        if candidate > last_step and hmac.compare_digest(_hotp(secret_b32, candidate), code):
            return candidate
    return None


def provisioning_uri(secret_b32: str, account: str, issuer: str = "Lead-to-Delivery") -> str:
    label = quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?secret={secret_b32}&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"


# ---------------------------------------------------------- recovery codes
def new_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    def one() -> str:
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(12))
        return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"
    return [one() for _ in range(count)]


def normalize_recovery_code(code: str) -> str | None:
    cleaned = code.strip().lower().replace("-", "").replace(" ", "")
    if len(cleaned) != 12 or any(c not in _RECOVERY_ALPHABET for c in cleaned):
        return None
    return cleaned


def hash_recovery_code(code: str) -> str:
    normalized = normalize_recovery_code(code)
    if normalized is None:
        raise ValueError("not a recovery code")
    return hashlib.sha256(("ltd-recovery:" + normalized).encode("ascii")).hexdigest()


def looks_like_recovery_code(code: str) -> bool:
    return normalize_recovery_code(code) is not None


# ---------------------------------------------------- secret encryption
def parse_key(value: str | None) -> bytes:
    if not value:
        raise MfaKeyError("ADMIN_MFA_ENCRYPTION_KEY is not set")
    try:
        key = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MfaKeyError("ADMIN_MFA_ENCRYPTION_KEY is not valid base64") from exc
    if len(key) != 32:
        raise MfaKeyError("ADMIN_MFA_ENCRYPTION_KEY must decode to 32 bytes")
    return key


def encrypt_secret(secret_b32: str, key: bytes) -> str:
    nonce = secrets.token_bytes(_NONCE_BYTES)
    sealed = AESGCM(key).encrypt(nonce, secret_b32.encode("ascii"), _AAD)
    return "v1:" + base64.b64encode(nonce + sealed).decode("ascii")


def decrypt_secret(stored: str, key: bytes) -> str:
    if not stored.startswith("v1:"):
        raise MfaKeyError("unsupported TOTP secret format")
    try:
        raw = base64.b64decode(stored[3:], validate=True)
        return AESGCM(key).decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], _AAD).decode("ascii")
    except Exception as exc:  # wrong key, tampering, corruption: never echo details
        raise MfaKeyError("TOTP secret cannot be decrypted with ADMIN_MFA_ENCRYPTION_KEY") from exc


def _main(argv: list[str]) -> int:
    if argv != ["generate-key"]:
        print("usage: python -m app.mfa generate-key", file=sys.stderr)
        return 2
    print(base64.b64encode(secrets.token_bytes(32)).decode("ascii"))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
