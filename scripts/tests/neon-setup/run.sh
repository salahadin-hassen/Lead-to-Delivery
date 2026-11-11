#!/usr/bin/env bash
# Regression tests for scripts/neon-staging-setup.sh on a DISPOSABLE local
# PostgreSQL cluster. This is NOT a Neon test: Neon's control plane (role
# creation, generated passwords, neon_superuser) is simulated by the local
# superuser; see docs/free-staging-options.md for what remains to be verified
# on a real Neon project.
#
#   scripts/tests/neon-setup/run.sh            # needs initdb/pg_ctl (PostgreSQL 16+),
#                                              # psql, openssl, python3, backend/.venv
#   PG_BIN=/usr/lib/postgresql/16/bin PORT=55435 scripts/tests/neon-setup/run.sh
#
# Creates a throwaway cluster (TLS, SCRAM, statement logging) in a fresh
# temporary directory on 127.0.0.1:$PORT with its own socket directory, runs
# the harness, then stops the cluster and deletes the directory. It never
# connects to any other server or database (the developer's `ltd` included).
# The helper under test is a generated copy whose only differences are the
# accepted host suffix (.scratch.test) and honouring PGHOSTADDR=127.0.0.1.
set -Eeuo pipefail
umask 077

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
REPO=$(cd -- "$HERE/../../.." && pwd -P)
PG_BIN=${PG_BIN:-$(pg_config --bindir 2>/dev/null || echo /usr/lib/postgresql/16/bin)}
PORT=${PORT:-55435}
for tool in "$PG_BIN/initdb" "$PG_BIN/pg_ctl"; do [ -x "$tool" ] || { echo "missing $tool (set PG_BIN)" >&2; exit 2; }; done
for tool in psql openssl python3; do command -v "$tool" >/dev/null || { echo "missing $tool" >&2; exit 2; }; done
[ -x "$REPO/backend/.venv/bin/python" ] || { echo "missing backend/.venv" >&2; exit 2; }
if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then echo "port $PORT is in use; set PORT" >&2; exit 2; fi

T=$(mktemp -d "${TMPDIR:-/tmp}/ltd-neon-setup-test.XXXXXXXX")
cleanup() {
  local status=$?
  "$PG_BIN/pg_ctl" -D "$T/data" -m fast -w stop >/dev/null 2>&1 || true
  rm -rf -- "$T"
  exit "$status"
}
trap cleanup EXIT INT TERM

mkdir -p "$T/sock"
"$PG_BIN/initdb" -D "$T/data" -U ltd_super --auth-local=trust --auth-host=scram-sha-256 -E UTF8 >/dev/null
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=scratch.test" \
  -keyout "$T/data/server.key" -out "$T/data/server.crt" 2>/dev/null
chmod 600 "$T/data/server.key"
"$PG_BIN/pg_ctl" -D "$T/data" -l "$T/postgres.log" -w \
  -o "-p $PORT -k $T/sock -c listen_addresses=127.0.0.1 -c ssl=on -c log_statement=all" start >/dev/null

# Test copy of the helper: accepts *.scratch.test, honours PGHOSTADDR, uses this checkout.
sed -e 's/^NEON_SUFFIX=\.neon\.tech$/NEON_SUFFIX=.scratch.test/' \
    -e "s/grep -E '^(PG\[A-Z_\]\*|DATABASE_URL)\$' || true)/grep -E '^(PG[A-Z_]*|DATABASE_URL)\$' | grep -v '^PGHOSTADDR\$' || true)/" \
    -e "s|^REPO=.*|REPO=$REPO|" \
    "$REPO/scripts/neon-staging-setup.sh" > "$T/setup-testcopy.sh"
[ "$(diff "$REPO/scripts/neon-staging-setup.sh" "$T/setup-testcopy.sh" | grep -c '^>')" = 3 ] \
  || { echo "test copy differs unexpectedly from the helper" >&2; exit 2; }

"$REPO/backend/.venv/bin/python" "$HERE/harness.py" --repo "$REPO" --work "$T" --port "$PORT" --sock "$T/sock"
