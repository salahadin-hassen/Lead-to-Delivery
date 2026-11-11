"""Regression harness for scripts/neon-staging-setup.sh - run via run.sh only.

Drives the helper through a pseudo-terminal against a disposable local cluster.
Neon's control plane is SIMULATED by the local superuser: it creates roles and
sets generated passwords as SCRAM verifiers (never plaintext). Prints results
only - never the throwaway passwords. Exit status 0 only if every case passes.
"""
import argparse, base64, glob, hashlib, hmac, os, pty, re, select, signal, struct, subprocess, sys, textwrap, time
import urllib.error, urllib.request
from urllib.parse import quote

ap = argparse.ArgumentParser()
for a in ("--repo", "--work", "--port", "--sock"):
    ap.add_argument(a, required=True)
A = ap.parse_args()
REPO, WORK, PORT, SOCK = A.repo, A.work, int(A.port), A.sock
REAL, COPY, LOG = f"{REPO}/scripts/neon-staging-setup.sh", f"{WORK}/setup-testcopy.sh", f"{WORK}/postgres.log"
PY = f"{REPO}/backend/.venv/bin/python"
HOST = "ep-test-123456.eu-central-1.aws.scratch.test"
ENV = {k: v for k, v in os.environ.items() if not k.startswith("PG") and k != "DATABASE_URL"}
ENV["PGHOSTADDR"] = "127.0.0.1"   # honoured only by the generated test copy
RUNDIRS = [os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "/tmp"]
SECRETS: list[str] = []


def url(user, pw, host=HOST, db="ltd", query="sslmode=require", port=None):
    return (f"postgresql://{quote(user, safe='')}:{quote(pw, safe='')}@{host}:{port or PORT}/{db}"
            + (f"?{query}" if query else ""))


def leftovers():
    return [d for base in RUNDIRS for d in glob.glob(f"{base}/ltd-setup.*")]


def run(script, args, answers, tty=True, timeout=180, cwd="/"):
    """Runs the helper from an unrelated directory (cwd=/) on a pseudo-terminal."""
    if not tty:
        p = subprocess.run(["bash", script, *args], input="x\n", capture_output=True, text=True, env=ENV,
                           timeout=timeout, cwd=cwd)
        return p.returncode, p.stdout + p.stderr
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        os.execvpe("bash", ["bash", script, *args], ENV)
    out, i, t0 = b"", 0, time.time()
    while True:
        r, _, _ = select.select([fd], [], [], 0.3)
        if r:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                chunk = b""
            if not chunk:
                break
            out += chunk
        text = out.decode(errors="replace")
        if i < len(answers) and re.search(answers[i][0], text[-400:]):
            reply = answers[i][1]
            os.write(fd, (reply(text) if callable(reply) else reply).encode() + b"\n")
            i += 1
        if time.time() - t0 > timeout:
            os.kill(pid, signal.SIGKILL)
            break
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), out.decode(errors="replace")


def owner(sub, script=COPY):
    return run(script, [sub], [(r"URL \(input hidden\)", url("neondb_owner", OWNER_PW)), (r"Type yes", "yes")])


def app(sub, pw, extra=(), args=()):
    return run(COPY, [sub, *args], [(r"URL \(input hidden\)", url("ltd_app", pw)), (r"Type yes", "yes"), *extra])


results = []


def case(name, ok, detail=""):
    left = leftovers()
    good = bool(ok) and not left
    results.append(good)
    print(f"{'PASS' if good else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else "")
          + (f"  LEFTOVER {left}" if left else ""), flush=True)


def psql(user, sql, db="ltd", file=None):
    cmd = ["psql", "-h", SOCK, "-p", str(PORT), "-U", user, "-d", db, "-XAtq", "-v", "ON_ERROR_STOP=1"]
    cmd += ["-f", file] if file else ["-c", sql]
    return subprocess.run(cmd, capture_output=True, text=True)


def su(sql, db="ltd"):
    p = psql("ltd_super", sql, db)
    if p.returncode:
        raise RuntimeError(p.stderr.strip())
    return p.stdout.strip()


def scram(pw):
    """SCRAM-SHA-256 verifier, as Neon's control plane would store it (simulation)."""
    salt, it = os.urandom(16), 4096
    salted = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, it)
    ck = hmac.new(salted, b"Client Key", "sha256").digest()
    sk = hmac.new(salted, b"Server Key", "sha256").digest()
    b = lambda x: base64.b64encode(x).decode()
    return f"SCRAM-SHA-256${it}:{b(salt)}${b(hashlib.sha256(ck).digest())}:{b(sk)}"


def control_plane_password(role):
    """Simulates `neon roles create` / `reset-password`: generated, shown once, stored as a verifier."""
    pw = "npg_" + base64.urlsafe_b64encode(os.urandom(24)).decode().rstrip("=")
    su(f"ALTER ROLE {role} PASSWORD '{scram(pw)}'", "postgres")
    SECRETS.append(pw)
    return pw


def acl_snapshot():
    return su("""SELECT (SELECT datacl::text FROM pg_database WHERE datname='ltd') || '|' ||
                        coalesce((SELECT nspacl::text FROM pg_namespace WHERE nspname='public'), '') || '|' ||
                        coalesce((SELECT string_agg(defaclacl::text, ',' ORDER BY oid) FROM pg_default_acl), '') || '|' ||
                        coalesce((SELECT string_agg(relname || relacl::text, ',' ORDER BY relname) FROM pg_class
                                  WHERE relnamespace = 'public'::regnamespace AND relacl IS NOT NULL), '') || '|' ||
                        coalesce((SELECT rolconnlimit || rolcreatedb::text FROM pg_roles WHERE rolname='ltd_app'), '')""")


def reset_db():
    su("DROP DATABASE IF EXISTS ltd", "postgres")
    su("DROP ROLE IF EXISTS ltd_app", "postgres")
    su("CREATE DATABASE ltd OWNER neondb_owner", "postgres")   # PUBLIC keeps the defaults (CONNECT, TEMP)


# ---------- bootstrap: Neon-like owner role (member of neon_superuser, CREATEDB/CREATEROLE)
su("CREATE ROLE neon_superuser NOLOGIN CREATEDB CREATEROLE BYPASSRLS REPLICATION", "postgres")
su("CREATE ROLE neondb_owner LOGIN CREATEDB CREATEROLE IN ROLE neon_superuser", "postgres")
OWNER_PW = control_plane_password("neondb_owner")
LOG_START = os.path.getsize(LOG)

# ---------- 0. real helper: location independence, URL validation, TTY guard
code, text = run(REAL, ["password"], [])
case("password (run from /): prints the Neon CLI reset procedure, never prompts or connects",
     code == 0 and "neon roles reset-password ltd_app --project-id <project-id> --branch <branch-id>" in text
     and "URL (input hidden)" not in text)
code, text = run(REAL, [], [])
case("no subcommand (run from /): usage incl. the mandatory --access read_write --schema public command",
     code == 2 and "--access read_write --database ltd" in text and "--schema public" in text)
neon = "ep-a-1.eu-central-1.aws.neon.tech"
bad = {
    "localhost": url("neondb_owner", "x", host="localhost"),
    "127.0.0.1": url("neondb_owner", "x", host="127.0.0.1"),
    "local socket URL": "postgresql:///ltd",
    "non-Neon host": url("neondb_owner", "x", host="db.example.com"),
    "lookalike suffix": url("neondb_owner", "x", host="neon.tech.evil.example"),
    "pooled endpoint": url("neondb_owner", "x", host="ep-a-1-pooler.eu-central-1.aws.neon.tech"),
    "database neondb": url("neondb_owner", "x", host=neon, db="neondb"),
    "no sslmode": url("neondb_owner", "x", host=neon, query=""),
    "sslmode=prefer": url("neondb_owner", "x", host=neon, query="sslmode=prefer"),
    "no password": f"postgresql://neondb_owner@{neon}/ltd?sslmode=require",
    "extra parameter": url("neondb_owner", "x", host=neon, query="sslmode=require&host=/tmp"),
}
for name, u in bad.items():
    code, text = run(REAL, ["migrate"], [(r"URL \(input hidden\)", u)])
    case(f"real helper refuses {name} before connecting", code != 0 and "refused:" in text and "Target:" not in text)
code, text = run(REAL, ["roles"], [(r"URL \(input hidden\)", url("neondb_owner", "s3cr3t-not-shown", host=neon)),
                                   (r"Type yes to continue", "no")])
case("real helper shows target host/role only (no credentials) and aborts unless 'yes'",
     code != 0 and f"Target: role neondb_owner on host {neon}:{PORT}, database ltd" in text
     and "s3cr3t-not-shown" not in text and "refused: not confirmed" in text)
code, text = run(REAL, ["roles"], [], tty=False)
case("real helper refuses a non-TTY stdin", code != 0 and "interactive terminal" in text)

# ---------- 1. ltd_app missing -> refused with the exact CLI command
reset_db()
code, text = owner("roles")
case("roles without ltd_app: refused with the --access read_write --schema public command",
     code != 0 and "does not exist" in text and "--access read_write --database ltd --schema public" in text)

# ---------- 2. Console/API/default-CLI role (neon_superuser member) -> refused, nothing changed
su("CREATE ROLE ltd_app LOGIN CREATEDB CREATEROLE IN ROLE neon_superuser", "postgres")
control_plane_password("ltd_app")
before = acl_snapshot()
code, text = owner("roles")
case("admin-mode role (member of neon_superuser): refused before any change",
     code != 0 and "member of: neon_superuser" in text and "neon roles delete ltd_app" in text and acl_snapshot() == before)
code, text = owner("migrate")
case("migrate refuses while ltd_app is over-privileged (no table created)",
     code != 0 and su("SELECT count(*) FROM pg_tables WHERE schemaname='public'") == "0")

# ---------- 3. attributes the owner cannot remove
for attr in ("BYPASSRLS", "REPLICATION"):
    reset_db()
    su(f"CREATE ROLE ltd_app LOGIN {attr}", "postgres")
    before = acl_snapshot()
    code, text = owner("roles")
    case(f"{attr} role: refused, nothing changed", code != 0 and "cannot remove" in text and acl_snapshot() == before)

# ---------- 4. attribute fix not permitted (owner lacks ADMIN) -> whole transaction rolled back
reset_db()
su("CREATE ROLE ltd_app LOGIN CREATEDB", "postgres")
before = acl_snapshot()
code, text = owner("roles")
case("unfixable attribute: rolled back, PUBLIC lock-down and grants not half-applied",
     code != 0 and "rolled back" in text and acl_snapshot() == before)

# ---------- 5. simulated `neon roles create --access read_write` (worst-case grant set, created by the DB owner)
reset_db()
p = psql("neondb_owner", """CREATE ROLE ltd_app LOGIN;
  GRANT CONNECT, TEMP, CREATE ON DATABASE ltd TO ltd_app;
  GRANT USAGE, CREATE ON SCHEMA public TO ltd_app;
  ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO ltd_app;
  ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO ltd_app;
  ALTER DEFAULT PRIVILEGES GRANT ALL ON TABLES TO ltd_app;""")
assert p.returncode == 0, p.stderr
APP_PW = control_plane_password("ltd_app")
code, text = app("verify", APP_PW)
chk = psql("ltd_app", None, file=f"{REPO}/deploy/db-app-check.sql")
case("before narrowing: verify fails closed; db-app-check.sql flags the broad grant set",
     code != 0 and "VERIFY PASSED" not in text and chk.returncode != 0 and "TEMP on the database" in chk.stderr
     and "extra default privilege TRUNCATE" in chk.stderr and "connlimit=-1" in chk.stderr)
code, text = owner("roles")
case("roles: narrowed to the exact matrix (connection limit fixed)",
     code == 0 and "exact least-privilege policy (fixed: CONNECTION LIMIT 20)" in text)
snap = acl_snapshot()
code, text = owner("roles")
case("roles re-run: idempotent", code == 0 and acl_snapshot() == snap and "(fixed" not in text)

# ---------- 6. migrate + verify
code, text = owner("migrate")
case("migrate: schema 0006, grants re-applied and checked",
     code == 0 and "schema at version 0006" in text and "granted and checked" in text)
code, text = app("verify", APP_PW)
case("verify as ltd_app: exact matrix, 7 permission refusals, DML works",
     code == 0 and "VERIFY PASSED" in text and "exact privilege matrix: ok" in text
     and len(re.findall(r"refused \(permission\)", text)) == 7)
snap = acl_snapshot()
code, text = owner("migrate")
case("migrate re-run: no widening", code == 0 and acl_snapshot() == snap)

# ---------- 7. the workflow's own preflight code (extracted from staging-worker.yml, run without Docker)
wf = open(f"{REPO}/.github/workflows/staging-worker.yml").read()
pre = textwrap.dedent(re.search(r"<<'PY'\n(.*?)\n\s*PY\n", wf, re.S).group(1))
pre = pre.replace("/policy/db-app-check.sql", f"{REPO}/deploy/db-app-check.sql")
passfile = f"{WORK}/app.pgpass"
fd = os.open(passfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.write(fd, f"127.0.0.1:{PORT}:ltd:ltd_app:{APP_PW}\n".encode())
os.close(fd)
AENV = {**{k: v for k, v in ENV.items() if k != "PGHOSTADDR"}, "PGPASSFILE": passfile, "LTD_DISABLE_DOTENV": "1",
        "DATABASE_URL": f"postgresql://ltd_app@127.0.0.1:{PORT}/ltd?sslmode=require", "WORKER_POLL_SECONDS": "1"}


def preflight():
    p = subprocess.run([PY, "-c", pre], cwd=f"{REPO}/backend", env=AENV, capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout + p.stderr


code, text = preflight()
case("workflow preflight: passes on the narrowed role", code == 0 and "preflight ok: role ltd_app, TLS, exact privilege policy" in text)

# ---------- 8. drift: detected by verify, the workflow preflight and admin; repaired by roles
su("GRANT TRUNCATE ON clients TO ltd_app; GRANT INSERT ON schema_migrations TO ltd_app")
code, text = app("verify", APP_PW)
case("drift -> verify fails", code != 0 and "extra relation clients: TRUNCATE" in text)
code, text = preflight()
case("drift -> workflow preflight refuses to start the worker",
     code != 0 and "refusing to start the worker" in text and "privilege policy" in text and APP_PW not in text)
code, text = app("admin", APP_PW, args=("drift@example.com", "Drift"))
case("drift -> admin bootstrap refused before asking for the MFA key",
     code != 0 and "exactly the least-privilege policy" in text and "ADMIN_MFA_ENCRYPTION_KEY" not in text)
code, _ = owner("roles")
code2, text2 = app("verify", APP_PW)
case("roles repairs drift; verify passes again", code == 0 and code2 == 0 and "VERIFY PASSED" in text2)

# ---------- 9. rotation via the (simulated) control plane
OLD = APP_PW
APP_PW = control_plane_password("ltd_app")
code, text = app("verify", OLD)
code2, text2 = app("verify", APP_PW)
case("rotation: old password rejected, new one verifies, privileges unchanged",
     code != 0 and "cannot connect" in text and code2 == 0 and acl_snapshot() == snap)
os.remove(passfile)
fd = os.open(passfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.write(fd, f"127.0.0.1:{PORT}:ltd:ltd_app:{APP_PW}\n".encode())
os.close(fd)

# ---------- 10. the application as ltd_app: API health, worker claim loop and heartbeats, admin bootstrap
api = subprocess.Popen([PY, "-m", "uvicorn", "app.main:app", "--port", str(PORT + 1)], cwd=f"{REPO}/backend",
                       env=AENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
wrk = subprocess.Popen([PY, "-m", "app.jobs.worker"], cwd=f"{REPO}/backend", env=AENV,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
status = wstatus = None
try:
    for _ in range(40):
        time.sleep(0.5)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT + 1}/api/health", timeout=3) as r:
                status = r.status
                break
        except Exception:
            pass
    time.sleep(3)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT + 1}/api/health/worker", timeout=3) as r:
            wstatus = r.status
    except urllib.error.HTTPError as e:
        wstatus = e.code
    except Exception:
        pass
finally:
    for p in (api, wrk):
        p.send_signal(signal.SIGTERM)
    outs = [p.communicate(timeout=30)[0].decode(errors="replace") for p in (api, wrk)]
    os.remove(passfile)
denied = any("permission denied" in o.lower() or "InsufficientPrivilege" in o for o in outs)
case("API /api/health 200 and /api/health/worker 200 as ltd_app; worker stopped cleanly; no permission errors",
     status == 200 and wstatus == 200 and "worker stopped" in outs[1] and not denied, f"health={status} worker={wstatus}")


def totp(secret):
    key = base64.b32decode(secret, casefold=True)
    h = hmac.new(key, struct.pack(">Q", int(time.time() // 30)), hashlib.sha1).digest()
    o = h[-1] & 15
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000).zfill(6)


mfa_key = base64.b64encode(os.urandom(32)).decode()
admin_pw = base64.urlsafe_b64encode(os.urandom(18)).decode()
SECRETS += [mfa_key, admin_pw]
code, text = app("admin", APP_PW, args=("qa.admin@example.com", "Scratch Admin"), extra=(
    (r"ADMIN_MFA_ENCRYPTION_KEY", mfa_key), (r"New password", admin_pw), (r"Repeat", admin_pw),
    (r"code the app shows now", lambda t: totp(re.findall(r"key: ([A-Z2-7]+)", t)[-1]))))
case("admin bootstrap as the narrowed ltd_app (MFA, recovery codes)",
     code == 0 and "Administrator qa.admin@example.com created." in text and mfa_key not in text)

# ---------- 11. no password ever reached SQL text from the helper or the app
log = open(LOG, errors="replace").read()[LOG_START:]
pw_stmts = re.findall(r"(?:ALTER|CREATE) (?:ROLE|USER)[^\n]*PASSWORD[^\n]*", log, re.I)
plaintext = [s for s in pw_stmts if "SCRAM-SHA-256$" not in s]
case("statement log: every PASSWORD statement is a simulated control-plane verifier; none from the helper",
     not plaintext, f"{len(pw_stmts)} verifier statements, {len(plaintext)} plaintext")
case("server log contains none of the passwords or the MFA key", all(s not in log for s in SECRETS))

print(f"\n{sum(results)}/{len(results)} passed (disposable PostgreSQL; Neon simulated, not a real Neon test)")
sys.exit(0 if all(results) else 1)
