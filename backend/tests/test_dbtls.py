"""Client-side TLS verification (app.dbtls), used by the Neon setup helper and
the staging worker preflight. Encrypted connections over a real TLS server and
a TLS-terminating proxy (Neon's topology) are covered by
scripts/tests/neon-setup/run.sh; here: every classification branch, plus real
unencrypted and closed connections to the local ltd_test."""

import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest
from psycopg import pq
from psycopg.conninfo import conninfo_to_dict

from app import dbtls
from app.dbtls import TlsState, client_tls_state

BACKEND = Path(__file__).resolve().parents[1]


class FakePGconn:
    def __init__(self, ssl_in_use=None, status=pq.ConnStatus.OK, error=None):
        self._ssl, self.status, self._error = ssl_in_use, status, error

    @property
    def ssl_in_use(self):
        if self._error:
            raise self._error
        return self._ssl


def test_encrypted_connection_is_accepted():
    assert client_tls_state(FakePGconn(True))[0] is TlsState.ENCRYPTED


def test_unencrypted_connection_is_refused():
    state, reason = client_tls_state(FakePGconn(False))
    assert state is TlsState.UNENCRYPTED and "not encrypted" in reason


@pytest.mark.parametrize(
    "pgconn",
    [
        FakePGconn(True, status=pq.ConnStatus.BAD),               # closed / broken connection
        FakePGconn(error=psycopg.OperationalError("gone")),      # libpq call fails
        FakePGconn(error=AttributeError("ssl_in_use")),          # driver without the attribute
        FakePGconn(None),                                        # no answer
        FakePGconn(1),                                           # truthy but not a boolean
        FakePGconn("true"),
        object(),                                                # not a libpq connection at all
    ],
)
def test_state_that_cannot_be_verified_is_never_accepted(pgconn):
    state, reason = client_tls_state(pgconn)
    assert state is TlsState.UNVERIFIABLE
    assert reason


def test_reason_never_contains_driver_messages():
    secret = "postgresql://u:hunter2@db.example/ltd"  # fake  # pragma: allowlist secret
    _, reason = client_tls_state(FakePGconn(error=psycopg.OperationalError(secret)))
    assert "hunter2" not in reason and "db.example" not in reason


def test_real_unencrypted_connection_is_classified_unencrypted(test_database_url):
    with psycopg.connect(test_database_url, sslmode="disable") as conn:
        assert client_tls_state(conn.pgconn)[0] is TlsState.UNENCRYPTED


def test_real_closed_connection_is_unverifiable(test_database_url):
    conn = psycopg.connect(test_database_url, sslmode="disable")
    conn.close()
    assert client_tls_state(conn.pgconn)[0] is TlsState.UNVERIFIABLE


def _libpq_env(url: str, **extra: str) -> dict[str, str]:
    parts = conninfo_to_dict(url)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG") and k != "DATABASE_URL"}
    for key, var in (("host", "PGHOST"), ("port", "PGPORT"), ("user", "PGUSER"),
                     ("dbname", "PGDATABASE"), ("password", "PGPASSWORD")):
        if parts.get(key):
            env[var] = str(parts[key])
    env.update(extra)
    return env


def test_cli_refuses_a_real_unencrypted_connection(test_database_url):
    proc = subprocess.run([sys.executable, "-m", "app.dbtls", "check"], cwd=BACKEND, capture_output=True,
                          text=True, env=_libpq_env(test_database_url, PGSSLMODE="disable"), timeout=60)
    assert proc.returncode == 1
    assert proc.stdout.strip() == "refused: unencrypted: this client connection is not encrypted"
    assert "ltd_test" not in proc.stdout + proc.stderr  # no connection details echoed


def test_cli_refuses_when_it_cannot_connect():
    env = _libpq_env("postgresql://nobody:TopSecretPw@127.0.0.1:1/ltd", PGCONNECT_TIMEOUT="2")
    proc = subprocess.run([sys.executable, "-m", "app.dbtls", "check"], cwd=BACKEND, capture_output=True,
                          text=True, env=env, timeout=60)
    assert proc.returncode == 1
    assert proc.stdout.strip() == "refused: cannot connect (OperationalError)"
    assert "TopSecretPw" not in proc.stdout + proc.stderr


def test_cli_usage():
    assert dbtls.main([]) == 2
    assert dbtls.main(["check", "extra"]) == 2
