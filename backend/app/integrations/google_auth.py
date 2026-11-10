"""Google service-account credentials for the Sheets adapter.

* Loads GOOGLE_SERVICE_ACCOUNT_FILE with google.oauth2.service_account
  (google-auth signs the JWT; nothing is signed by hand), scoped to
  https://www.googleapis.com/auth/spreadsheets only.
* Refreshes access tokens through ``HttpxAuthRequest``, an implementation of
  google-auth's documented transport interface (google.auth.transport.Request)
  on the app's httpx stack: explicit timeouts, TLS verification, no
  redirects, https://oauth2.googleapis.com only, and the
  no-database-connection guard before every network call.
* Tokens live only in process memory and are refreshed when expired.

Nothing here logs or returns the key, the service-account email, the file
path or a token.
"""

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from google.auth import exceptions as google_exceptions
from google.auth import transport as google_transport
from google.oauth2 import service_account

from app import db

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
ALLOWED_TOKEN_HOSTS = {"oauth2.googleapis.com"}
_REQUIRED_FIELDS = ("type", "client_email", "private_key", "token_uri")
_EMAIL_RE = re.compile(r"^[^@\s]+@[A-Za-z0-9.-]+\.iam\.gserviceaccount\.com$")
MAX_KEY_FILE_BYTES = 64 * 1024


class CredentialError(Exception):
    """Credentials unusable. ``code`` (safe to store) and ``retryable``."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def _token_uri_ok(uri: str) -> bool:
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    return (parts.scheme == "https" and (parts.hostname or "") in ALLOWED_TOKEN_HOSTS
            and parts.port in (None, 443) and not parts.username and not parts.query and not parts.fragment)


def load_service_account_info(path: Path | str | None) -> dict[str, Any]:
    """Read and validate the key file. Raises CredentialError (never echoes contents)."""
    if not path:
        raise CredentialError("SHEETS_NOT_CONFIGURED", "GOOGLE_SERVICE_ACCOUNT_FILE is not set.")
    file = Path(path)
    try:
        if not file.is_file():
            raise CredentialError("SHEETS_CREDENTIALS_FILE_NOT_FOUND", "The service-account key file does not exist.")
        if file.stat().st_size > MAX_KEY_FILE_BYTES:
            raise CredentialError("SHEETS_INVALID_CREDENTIALS", "The service-account key file is too large.")
        info = json.loads(file.read_text(encoding="utf-8"))
    except CredentialError:
        raise
    except (OSError, UnicodeDecodeError):
        raise CredentialError("SHEETS_INVALID_CREDENTIALS", "The service-account key file cannot be read.") from None
    except json.JSONDecodeError:
        raise CredentialError("SHEETS_INVALID_CREDENTIALS", "The service-account key file is not valid JSON.") from None
    if not isinstance(info, dict):
        raise CredentialError("SHEETS_INVALID_CREDENTIALS", "The service-account key file is not a JSON object.")
    missing = [f for f in _REQUIRED_FIELDS if not isinstance(info.get(f), str) or not info.get(f)]
    if missing:
        raise CredentialError("SHEETS_INVALID_CREDENTIALS",
                              f"The service-account key file lacks: {', '.join(missing)}.")
    if info["type"] != "service_account":
        raise CredentialError("SHEETS_INVALID_CREDENTIALS", "The key file is not a service-account key.")
    if not _EMAIL_RE.match(info["client_email"]):
        raise CredentialError("SHEETS_INVALID_CREDENTIALS", "The key file has an unexpected client_email.")
    if not _token_uri_ok(info["token_uri"]):
        # A tampered key file must not be able to send signed assertions elsewhere.
        raise CredentialError("SHEETS_INVALID_CREDENTIALS", "The key file's token_uri is not Google's token endpoint.")
    if "PRIVATE KEY" not in info["private_key"]:
        raise CredentialError("SHEETS_INVALID_CREDENTIALS", "The key file has no private key.")
    return info


class _AuthResponse(google_transport.Response):
    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    @property
    def status(self) -> int:
        return self._response.status_code

    @property
    def headers(self):
        return self._response.headers

    @property
    def data(self) -> bytes:
        return self._response.content


class HttpxAuthRequest(google_transport.Request):
    """google-auth transport over httpx (used only for token refresh)."""

    def __init__(self, client: httpx.Client, timeout: float) -> None:
        self._client = client
        self._timeout = timeout

    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):  # noqa: ANN001
        if not _token_uri_ok(url):
            raise google_exceptions.TransportError("refusing to contact a non-Google token endpoint")
        db.assert_no_connection_held()
        try:
            response = self._client.request(method, url, content=body, headers=headers,
                                            timeout=min(timeout or self._timeout, self._timeout))
        except httpx.HTTPError as exc:
            # Class name only: never the request (it carries the signed assertion).
            raise google_exceptions.TransportError(type(exc).__name__) from None
        return _AuthResponse(response)


class ServiceAccountTokenProvider:
    """Caches one Credentials object per process; refreshes on demand."""

    def __init__(self, key_file: Path | str | None, client: httpx.Client, timeout: float) -> None:
        self._key_file = key_file
        self._client = client
        self._timeout = timeout
        self._credentials: service_account.Credentials | None = None

    def validate(self) -> None:
        """Raise CredentialError if the key file is unusable (no network)."""
        info = load_service_account_info(self._key_file)
        try:
            service_account.Credentials.from_service_account_info(info, scopes=[SHEETS_SCOPE])
        except (ValueError, TypeError):
            raise CredentialError("SHEETS_INVALID_CREDENTIALS", "The private key could not be loaded.") from None

    def access_token(self) -> str:
        if self._credentials is None:
            info = load_service_account_info(self._key_file)
            try:
                self._credentials = service_account.Credentials.from_service_account_info(info, scopes=[SHEETS_SCOPE])
            except (ValueError, TypeError):
                raise CredentialError("SHEETS_INVALID_CREDENTIALS", "The private key could not be loaded.") from None
        if not self._credentials.valid:
            try:
                self._credentials.refresh(HttpxAuthRequest(self._client, self._timeout))
            except google_exceptions.TransportError:
                raise CredentialError("SHEETS_TOKEN_REFRESH_FAILED", "Could not reach Google's token endpoint.",
                                      retryable=True) from None
            except google_exceptions.RefreshError as exc:
                retryable = bool(getattr(exc, "retryable", False))
                raise CredentialError("SHEETS_TOKEN_REFRESH_FAILED" if retryable else "SHEETS_INVALID_CREDENTIALS",
                                      "Google refused to issue an access token.", retryable=retryable) from None
        token = self._credentials.token
        if not token:
            raise CredentialError("SHEETS_TOKEN_REFRESH_FAILED", "No access token was issued.", retryable=True)
        return token
