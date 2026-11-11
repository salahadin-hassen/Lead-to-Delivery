#!/usr/bin/env bash
# Lead-to-Delivery: Neon Free staging setup (docs/free-staging-options.md
# section 5). Run by the OWNER in their own interactive terminal - never
# through chat, CI or a log file. Works from any current directory.
#
#   roles     narrow ltd_app (created beforehand with the Neon CLI, see below)
#             to the exact least-privilege policy, or refuse if it cannot be
#   password  how to rotate the ltd_app password (Neon CLI; no connection)
#   migrate   narrow ltd_app, apply migrations as the Neon owner, re-grant and
#             prove the exact policy (deploy/db-app-check.sql)
#   verify    prove ltd_app: exact policy, TLS, schema current, DML works, DDL refused
#   admin     create the first administrator (MFA enrolment, recovery codes);
#             refuses unless ltd_app passes the exact policy check
#   whoami    show which roles are connected (expect ltd_app / ltd-api)
#
# ltd_app itself is created by Neon's control plane, which generates its
# password and prints it once - the password never passes through SQL you
# write, this script, a command line or an environment variable:
#   neon roles create --name ltd_app --access read_write --database ltd \
#        --schema public --project-id <project-id> --branch <branch-id>
# `--access read_write` is mandatory: without it (and in the Console or API)
# the role joins neon_superuser, which this script refuses. Neon documents
# that SQL-set passwords must be plaintext ("hashed passwords are not
# supported"), so this script never sets a password itself. Whether the CLI
# behaves exactly as documented on the Free plan is verified by `roles` on
# the first real run: it inspects the role before changing anything.
#
# Credential handling:
# * URLs are typed at silent prompts and handed to Python on stdin; they are
#   never exported, never on a command line, never in shell history.
# * Passwords reach psql and psycopg ONLY through a temporary PGPASSFILE
#   (mode 0600, private mode-0700 directory) that is shredded and removed on
#   every exit path (normal exit, error, Ctrl-C, SIGTERM, SIGHUP).
# * Only a direct (non-pooler) TLS endpoint on *.neon.tech, database `ltd`,
#   is accepted, and the server is re-checked after connecting (TCP, TLS,
#   current_database = ltd, expected role). A local database such as the
#   developer's `ltd` can never be targeted: every PG* variable is cleared,
#   ~/.pgpass and ~/.psqlrc are not used.
#
# Usage:  scripts/neon-staging-setup.sh roles|password|migrate|verify|whoami
#         scripts/neon-staging-setup.sh admin you@example.com "Your Name"
# Needs: psql, and backend/.venv (cd backend && python3 -m venv .venv &&
# .venv/bin/pip install --require-hashes -r requirements.lock).
set -Eeuo pipefail
set +x
umask 077

REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
PY=$REPO/backend/.venv/bin/python
NEON_SUFFIX=.neon.tech
[ -f "$REPO/deploy/db-app-check.sql" ] && [ -x "$PY" ] \
  || { echo "refused: run from a Lead-to-Delivery checkout with backend/.venv installed" >&2; exit 1; }
cd "$REPO/backend"   # migrations and the admin tool run from backend/

# Start from a clean libpq environment: nothing may redirect the connection.
while IFS= read -r v; do unset "$v"; done < <(compgen -e | grep -E '^(PG[A-Z_]*|DATABASE_URL)$' || true)
export LTD_DISABLE_DOTENV=1    # never read the repository .env (it points at local ltd)
export PSQL_PAGER=cat PAGER=cat   # never block in a pager

WORKDIR=""
cleanup() {
  local status=$?
  if [ -n "$WORKDIR" ] && [ -d "$WORKDIR" ]; then
    find "$WORKDIR" -type f -exec shred -u -z {} + 2>/dev/null || find "$WORKDIR" -type f -delete
    rm -rf -- "$WORKDIR"
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
# A run killed with SIGKILL cannot clean up: shred such leftovers (older than 60 min) first.
while IFS= read -r -d '' stale; do
  find "$stale" -type f -exec shred -u -z {} + 2>/dev/null || true; rm -rf -- "$stale"
  echo "removed a stale setup directory from an interrupted run" >&2
done < <(find "${XDG_RUNTIME_DIR:-/tmp}" -maxdepth 1 -type d -name 'ltd-setup.*' -user "$(id -u)" -mmin +60 -print0 2>/dev/null)
WORKDIR=$(mktemp -d "${XDG_RUNTIME_DIR:-/tmp}/ltd-setup.XXXXXXXX")
chmod 700 "$WORKDIR"

die() { echo "refused: $*" >&2; exit 1; }

# Validates a URL read from stdin; writes its password to the passfile given
# as argv[1] (0600, created exclusively); prints ONLY non-secret settings.
VALIDATE_PY=$(cat <<'PY'
import os, shlex, sys
from urllib.parse import urlsplit, parse_qs, unquote
suffix, passfile = sys.argv[1], sys.argv[2]
raw = sys.stdin.readline().strip()
try:
    u = urlsplit(raw); q = parse_qs(u.query, keep_blank_values=True)
    host, port = (u.hostname or ""), (u.port or 5432)
except ValueError:
    sys.exit("refused: not a valid URL")
problems = []
if u.scheme not in ("postgresql", "postgres"): problems.append("not a postgresql:// URL")
if not host.endswith(suffix) or host.count(".") < 3: problems.append(f"host is not a Neon endpoint (*{suffix})")
if "-pooler" in host: problems.append("pooled endpoint: use the DIRECT connection string (no -pooler)")
if q.get("sslmode", [""])[0] != "require": problems.append("the URL must contain sslmode=require")
if not u.username or u.password is None or u.password == "": problems.append("user and password must be in the URL")
if (u.path or "/").lstrip("/") != "ltd": problems.append("database name must be ltd")
extra = set(q) - {"sslmode", "channel_binding"}
if extra: problems.append("unexpected URL parameters: " + ", ".join(sorted(extra)))
if problems: sys.exit("refused: " + "; ".join(problems))
user, password = unquote(u.username), unquote(u.password)
esc = lambda s: s.replace("\\", "\\\\").replace(":", "\\:")
fd = os.open(passfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as f:
    f.write(f"{esc(host)}:{port}:ltd:{esc(user)}:{esc(password)}\n")
out = {"T_HOST": host, "T_PORT": str(port), "T_USER": user,
       "T_CB": q.get("channel_binding", [""])[0]}
for k, v in out.items(): print(f"{k}={shlex.quote(v)}")
PY
)

# connect LABEL EXPECTED_USER(optional): prompt, validate, confirm, export libpq settings.
connect() {
  local label=$1 expect=${2:-} url settings answer passfile="$WORKDIR/pgpass.$1"
  [ -t 0 ] || die "run this in an interactive terminal (prompts need a TTY)"
  read -rsp "$label: Neon DIRECT connection URL (input hidden): " url; echo
  settings=$(printf '%s\n' "$url" | "$PY" -c "$VALIDATE_PY" "$NEON_SUFFIX" "$passfile") || { url=""; exit 1; }
  url=""; unset url
  eval "$settings"
  if [ -n "$expect" ] && [ "$T_USER" != "$expect" ]; then die "this step needs the $expect role, not $T_USER"; fi
  echo "Target: role $T_USER on host $T_HOST:$T_PORT, database ltd, TLS required."
  read -rp "Type yes to continue: " answer
  [ "$answer" = "yes" ] || die "not confirmed"
  export PGHOST=$T_HOST PGPORT=$T_PORT PGUSER=$T_USER PGDATABASE=ltd PGSSLMODE=require \
         PGPASSFILE=$passfile PGAPPNAME=ltd-staging-setup PGCONNECT_TIMEOUT=20
  [ -n "$T_CB" ] && export PGCHANNELBINDING=$T_CB
  # Password-free URL for the Python tools; libpq reads the password from PGPASSFILE.
  export DATABASE_URL="postgresql://$T_USER@$T_HOST:$T_PORT/ltd?sslmode=require"
  local check
  check=$(psql -XAtq -v ON_ERROR_STOP=1 -c "SELECT current_database() || ' ' || current_user || ' ' ||
          (inet_server_addr() IS NOT NULL) || ' ' || (SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid())") \
    || die "cannot connect (check the URL, region and password)"
  [ "$check" = "ltd $T_USER true true" ] || die "server check failed: expected 'ltd $T_USER true true', got '$check'"
  echo "connected: database ltd as $T_USER over TLS"
}

psql_x() { psql -X -q -P pager=off -v ON_ERROR_STOP=1 "$@"; }

CREATE_HINT="create it with the Neon CLI first:  neon roles create --name ltd_app --access read_write --database ltd --schema public --project-id <project-id> --branch <branch-id>"

# Narrows ltd_app to the exact policy in ONE transaction (all or nothing):
# attributes the owner may change, PUBLIC lock-down, deploy/db-app-grants.sql,
# then deploy/db-app-check.sql proves the result. Anything the owner cannot
# undo (superuser, replication, bypassrls, membership in any role such as
# neon_superuser) is refused BEFORE any change.
narrow_app() {
  local state su repl byp members createdb createrole connlimit alter=""
  state=$(psql_x -At -c "SELECT r.rolsuper, r.rolreplication, r.rolbypassrls,
      coalesce((SELECT string_agg(g.rolname, ',') FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid
                WHERE m.member = r.oid), '-'), r.rolcreatedb, r.rolcreaterole, r.rolconnlimit
      FROM pg_roles r WHERE r.rolname = 'ltd_app'" -F ' ')
  [ -n "$state" ] || die "role ltd_app does not exist; $CREATE_HINT"
  read -r su repl byp members createdb createrole connlimit <<<"$state"
  if [ "$su$repl$byp" != "fff" ] || [ "$members" != "-" ]; then
    echo "refused: ltd_app has rights the owner cannot remove (superuser=$su replication=$repl" >&2
    echo "bypassrls=$byp member of: $members). It was probably created without --access (Console," >&2
    echo "API or 'neon roles create' default = neon_superuser). Nothing was changed. Delete it with" >&2
    echo "  neon roles delete ltd_app --project-id <project-id> --branch <branch-id>" >&2
    echo "and re-create it with --access read_write --schema public." >&2
    exit 1
  fi
  [ "$createdb" = f ] || alter+=" NOCREATEDB"
  [ "$createrole" = f ] || alter+=" NOCREATEROLE"
  [ "$connlimit" = 20 ] || alter+=" CONNECTION LIMIT 20"
  { [ -z "$alter" ] || echo "ALTER ROLE ltd_app$alter;"
    echo "REVOKE ALL ON DATABASE ltd FROM PUBLIC;"
    echo "REVOKE CREATE ON SCHEMA public FROM PUBLIC;"
  } | psql_x -1 -f - -f "$REPO/deploy/db-app-grants.sql" -f "$REPO/deploy/db-app-check.sql" \
    || die "ltd_app could not be narrowed (transaction rolled back, nothing changed)"
  echo "ltd_app narrowed and checked: exact least-privilege policy${alter:+ (fixed:$alter)}"
}

case "${1:-}" in
  roles)
    connect "OWNER"
    [ "$T_USER" != "ltd_app" ] || die "narrowing ltd_app needs the owner role, not ltd_app"
    narrow_app
    ;;
  password)
    cat <<'TXT'
Rotate the ltd_app password with Neon's control plane (it generates the new
password and prints it once; existing connections are dropped):

  neon roles reset-password ltd_app --project-id <project-id> --branch <branch-id>

Run it in your own terminal (not in CI, not redirected to a file). Store the
value in your password manager, update STAGING_LTD_APP_DATABASE_URL (GitHub
environment staging-worker) and DATABASE_URL (Render), then run:  verify
TXT
    ;;
  migrate)
    connect "OWNER"
    [ "$T_USER" != "ltd_app" ] || die "migrations need the owner role, not ltd_app"
    narrow_app   # refuses an over-privileged ltd_app before any table exists
    "$PY" -m app.migrations
    "$PY" -m app.migrations --status | tail -n 2
    # Migrations create tables under the owner's default privileges; re-apply
    # the exact policy (schema_migrations/project_templates read-only) and prove it.
    psql_x -1 -f "$REPO/deploy/db-app-grants.sql" -f "$REPO/deploy/db-app-check.sql" \
      || die "grants after migration failed (rolled back); run: migrate again"
    echo "migrations applied; ltd_app granted and checked (schema_migrations/project_templates read-only)"
    ;;
  verify)
    connect "APP" ltd_app
    expected_schema=$("$PY" -c "from app import migrations; print(migrations.latest_version())")
    summary=$(psql -XAt -v ON_ERROR_STOP=1 -c "SELECT (SELECT max(version) FROM schema_migrations)
      || ' ' || rolsuper || ' ' || rolcreaterole || ' ' || rolcreatedb || ' ' || rolbypassrls
      || ' ' || has_schema_privilege('public', 'CREATE') || ' ' || has_database_privilege('ltd', 'CREATE')
      || ' ' || has_table_privilege('project_templates', 'INSERT,UPDATE,DELETE')
      || ' ' || has_table_privilege('schema_migrations', 'INSERT,UPDATE,DELETE')
      || ' ' || has_table_privilege('clients', 'TRUNCATE')
      FROM pg_roles WHERE rolname = current_user")
    want="$expected_schema false false false false false false false false false"
    echo "schema superuser createrole createdb bypassrls create_on_public create_on_db templates_writable migrations_writable truncate"
    echo "$summary"
    ok=1
    [ "$summary" = "$want" ] || { echo "FAIL: expected: $want"; ok=0; }
    if psql_x -f "$REPO/deploy/db-app-check.sql"; then echo "exact privilege matrix: ok (deploy/db-app-check.sql)"
    else echo "FAIL: privilege matrix (see above)"; ok=0; fi
    for stmt in "CREATE TABLE ddl_probe (i int)" "ALTER TABLE clients ADD COLUMN ddl_probe int" "DROP TABLE clients" \
                "TRUNCATE clients" "CREATE INDEX ddl_probe ON clients (name)" \
                "INSERT INTO schema_migrations (version, name, checksum, execution_ms) VALUES (999, 'x', repeat('0', 64), 0)" \
                "UPDATE project_templates SET name = name"; do
      if err=$(psql_x -c "BEGIN; $stmt; ROLLBACK;" 2>&1 >/dev/null); then
        echo "FAIL: allowed: $stmt"; ok=0
      elif printf '%s' "$err" | grep -qiE "permission denied|must be owner"; then
        echo "refused (permission): $stmt"
      else
        echo "FAIL: refused for another reason: $stmt"; ok=0
      fi
    done
    if psql_x -c "BEGIN; INSERT INTO worker_heartbeats (worker_id) VALUES ('setup-dml-probe');
                  UPDATE worker_heartbeats SET jobs_processed = 1 WHERE worker_id = 'setup-dml-probe';
                  DELETE FROM worker_heartbeats WHERE worker_id = 'setup-dml-probe';
                  SELECT count(*) FROM clients; SELECT count(*) FROM project_templates; ROLLBACK;" >/dev/null; then
      echo "runtime DML works (insert/update/delete rolled back; reads ok)"
    else echo "FAIL: runtime DML"; ok=0; fi
    [ "$ok" = 1 ] || exit 1
    echo "VERIFY PASSED"
    ;;
  admin)
    email=${2:?usage: admin you@example.com "Your Name"}; name=${3:?usage: admin you@example.com "Your Name"}
    connect "APP" ltd_app
    psql_x -f "$REPO/deploy/db-app-check.sql" \
      || die "ltd_app does not have exactly the least-privilege policy; run: roles (owner), then verify"
    # The MFA key is read with a hidden prompt INSIDE the Python process and
    # passed to the admin tool's settings in memory: it never enters the shell,
    # an environment variable, a command line or a file.
    "$PY" -c '
import getpass, sys
from app import admin_users
from app.config import Settings
from pydantic import ValidationError
key = getpass.getpass("The API ADMIN_MFA_ENCRYPTION_KEY (same value as on Render; input hidden): ")
try:
    settings = Settings(admin_mfa_encryption_key=key)
except ValidationError as exc:
    sys.exit("refused: " + "; ".join(e["msg"] for e in exc.errors()))
finally:
    key = None
admin_users.get_settings = lambda: settings
sys.exit(admin_users.main(["create", "--email", sys.argv[1], "--name", sys.argv[2]]))
' "$email" "$name"
    ;;
  whoami)
    connect "OWNER"
    psql -X -P pager=off -c "SELECT usename, application_name, count(*) FROM pg_stat_activity
                WHERE datname = 'ltd' GROUP BY 1, 2 ORDER BY 1, 2"
    ;;
  *) sed -n '2,/^set -Eeuo/p' "$0" | sed '$d'; exit 2 ;;
esac
