"""Client-side TLS verification of a LIVE database connection.

Why client-side: a managed provider such as Neon terminates TLS at its proxy
and talks to PostgreSQL over its own internal link, so the server's view
(``pg_stat_ssl.ssl``) is ``false`` even for a fully encrypted client
connection. The only reliable statement about *our* connection is libpq's
own state, ``PQsslInUse``, which psycopg exposes as ``PGconn.ssl_in_use``.
A connection string that merely *asks* for ``sslmode=require`` is not proof.

Three outcomes, fail closed: only ``ENCRYPTED`` is acceptable; anything that
cannot be established (closed connection, missing attribute, unexpected
value, an error while asking) is ``UNVERIFIABLE``.

Command-line use (setup helper; connection settings from the libpq PG*
environment and PGPASSFILE, never from arguments)::

    python -m app.dbtls check
    -> prints "<database> <user> <tcp:true|false> encrypted" and exits 0, or
       prints "refused: ..." and exits 1. Never prints a connection string,
       password or driver message.
"""

import enum
import sys
from typing import Any

from psycopg import pq


class TlsState(enum.Enum):
    ENCRYPTED = "encrypted"
    UNENCRYPTED = "unencrypted"
    UNVERIFIABLE = "unverifiable"


def client_tls_state(pgconn: Any) -> tuple[TlsState, str]:
    """Classify the TLS state of an open libpq connection (``conn.pgconn``)."""
    try:
        status = pgconn.status
        in_use = pgconn.ssl_in_use
    except Exception as exc:  # noqa: BLE001 - any failure to ask is "cannot verify"
        return TlsState.UNVERIFIABLE, f"libpq did not report the TLS state ({type(exc).__name__})"
    if status != pq.ConnStatus.OK:
        return TlsState.UNVERIFIABLE, "the connection is not open"
    if in_use is True:
        return TlsState.ENCRYPTED, "TLS is in use on this client connection"
    if in_use is False:
        return TlsState.UNENCRYPTED, "this client connection is not encrypted"
    return TlsState.UNVERIFIABLE, f"unexpected TLS state value ({type(in_use).__name__})"


def _check() -> int:
    import psycopg

    try:
        # Empty conninfo: libpq takes host, port, user, database, sslmode and
        # PGPASSFILE from the environment prepared by the caller.
        conn = psycopg.connect("", connect_timeout=20)
    except Exception as exc:  # noqa: BLE001 - never print the driver message (may echo the host/user)
        print(f"refused: cannot connect ({type(exc).__name__})")
        return 1
    with conn:
        state, reason = client_tls_state(conn.pgconn)
        if state is not TlsState.ENCRYPTED:
            print(f"refused: {state.value}: {reason}")
            return 1
        database, user, tcp = conn.execute(
            "SELECT current_database(), current_user, inet_server_addr() IS NOT NULL").fetchone()
    print(f"{database} {user} {'true' if tcp else 'false'} {state.value}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args != ["check"]:
        print("usage: python -m app.dbtls check", file=sys.stderr)
        return 2
    return _check()


if __name__ == "__main__":
    raise SystemExit(main())
