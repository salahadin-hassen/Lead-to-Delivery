# Build and release

How Lead-to-Delivery is built reproducibly, tested in CI and packaged as one
production image. Prepared 2026-11-11. Nothing here deploys anything.

## Python dependencies

| File | Contents | Used by |
|---|---|---|
| `backend/pyproject.toml` | Direct dependencies (pinned) and the `test` extra | Source of truth for what the project needs |
| `backend/requirements.lock` | All runtime packages, transitive included (30), with SHA-256 hashes | Production image |
| `backend/requirements-dev.lock` | Runtime + test packages (35), with hashes | Local development, CI |

The locks were first generated pinned to exactly the versions the full
backend suite had passed with (no upgrades); `requirements-dev.lock` equals
that environment package for package.

**Install (clean, hash-verified, wheels only):**

```bash
cd backend
python3.12 -m venv .venv
.venv/bin/pip install --require-hashes --only-binary=:all: -r requirements-dev.lock   # development / tests
# production image: same command with requirements.lock
```

The project itself is not pip-installed: uvicorn and `python -m` import
`app` from `backend/` (pytest is configured with `pythonpath = ["."]`).

**Regenerate after changing `pyproject.toml`** (pip-tools 7.5.x, Python 3.12):

```bash
python3.12 -m venv /tmp/piptools && /tmp/piptools/bin/pip install "pip-tools==7.5.2"
cd backend
/tmp/piptools/bin/pip-compile --generate-hashes --strip-extras --no-emit-index-url \
    --output-file=requirements.lock pyproject.toml
/tmp/piptools/bin/pip-compile --generate-hashes --strip-extras --no-emit-index-url \
    --extra=test --output-file=requirements-dev.lock pyproject.toml
```

pip-compile keeps versions already in the lock unless they conflict, so
running these two commands on an unchanged `pyproject.toml` reproduces both
lock files byte for byte (checked 2026-11-11; the header line it writes also
shows `--no-index`, which is pip-compile's own rendering, not an option to
pass). `--upgrade-package NAME` upgrades one package deliberately. Review the
diff, then run the full suite from a clean install before committing.

The hashes cover every file PyPI publishes for each pinned version; for the
image platform (CPython 3.12, glibc 2.36, x86_64) all 30 runtime packages
resolve to hashed wheels (`pip download --only-binary=:all: --platform
manylinux_2_36_x86_64 … --python-version 3.12 --abi cp312 --require-hashes`).

## Frontend dependencies

npm with the committed `frontend/package-lock.json` (lockfile v3) is
authoritative. Node version: `frontend/.nvmrc` = `24.22.2`, exactly the
Dockerfile's build stage and the version CI installs.

```bash
cd frontend
npm ci --no-audit --no-fund   # never `npm install` for builds; fails if the lock and package.json disagree
npm run typecheck
npm test
npm run build                 # typecheck + Vite build into frontend/dist
```

## Local tests

```bash
cd backend && .venv/bin/python -m pytest -q        # needs TEST_DATABASE_URL -> a LOCAL database named ltd_test
cd frontend && npm test
```

The backend suite refuses any database other than a local `ltd_test`,
ignores `.env` credentials and refuses `APP_ENV=production`.

## CI (`.github/workflows/ci.yml`)

Runs on every pull request and every push to `main` (superseded pull-request
runs are cancelled; runs on `main` always complete). `pull_request` only (no
`pull_request_target`), read-only token, no repository secrets, actions
pinned to commit SHAs, no deployment. Toolchain is exactly the image's:
Python 3.12.16 (checked by a step) and Node 24.22.2 from `.nvmrc` (checked).

| Job | What it does |
|---|---|
| `workflow-lint` | Downloads actionlint 1.7.12 (checksum-verified) and lints the workflow, including shellcheck on its `run:` blocks; shellcheck (warnings and errors) on `.github/ci/*.sh` and `deploy/release.sh` |
| `backend` | PostgreSQL 16 service container with database `ltd_test` (no password, reachable only from the job); installs `requirements-dev.lock` with hashes; `pip check`; full pytest suite (Node for the n8n workflow tests) |
| `frontend` | `npm ci`; fails if the lockfile changed; typecheck; tests; production build |
| `image` | `docker build` of the real Dockerfile, then [`.github/ci/image-smoke.sh`](../.github/ci/image-smoke.sh) on that image (below); an `if: always()` step removes leftover containers and networks |

The image smoke test (`.github/ci/image-smoke.sh <image>`):

1. Creates a private Docker network and a throwaway PostgreSQL 16 container,
   then an isolated database `ltd_smoke` owned by `ltd_owner`, plus a
   restricted `ltd_app` role (the [deploy/db-roles.sql](../deploy/db-roles.sql) model).
2. Checks the image: UID 10001, no Node/npm, built frontend and all six
   migrations present, no frontend sources, tests, docs, n8n, `.env`,
   key/dump/database files, test packages or secret-bearing variables.
3. Production refuses to start without its security settings.
4. Starts the API with the image's **default command**, publishing container
   port 8000 to the runner; `/api/health` answers **503 from outside the
   container** (proves the `0.0.0.0` binding) and the database is still
   empty (API start-up does not migrate).
5. Runs the explicit migration (`python -m app.migrations` as `ltd_owner`)
   and checks schema version 6; grants `ltd_app` and checks it cannot create
   tables.
6. Health 200; the portal page, a hashed asset, CSP, HSTS and
   `Referrer-Policy` are served; an invalid host gets 400; `/docs` 404;
   `/api/admin/me` 401.
7. Starts the worker from the same image (`python -m app.jobs.worker`, as
   `ltd_app`); `/api/health/worker` becomes 200.
8. `docker stop` (SIGTERM): the worker logs the signal and exits 0; the API
   shuts down gracefully (exit 0 or 143).
9. No generated secret appears in any log. A trap removes the containers
   and the network on every exit, after printing their last log lines on
   failure.

Throwaway values in the smoke test are generated during the run and masked
in the log; they are not repository secrets and protect nothing.

## Production image (`Dockerfile`)

Multi-stage: `node:24.22.2-bookworm-slim` builds the frontend;
`python:3.12.16-slim-bookworm` is the runtime (both pinned by digest). The
runtime contains only the hashed runtime lock, `backend/app`, `backend/sql`
and `frontend/dist`; it runs as the non-root user `ltd` (UID 10001); no
build arguments are declared and no secret is read at build time.
`.dockerignore` is an allowlist, so `.env`, `var/`, databases, logs and
caches never enter the build context.

```bash
docker build -t ltd .

# API (default command): uvicorn on 0.0.0.0:${PORT:-8000}
docker run --env-file prod.env -p 8000:8000 ltd

# Worker: the same image with another command
docker run --env-file prod.env ltd python -m app.jobs.worker

# Migrations: an explicit release step, with the schema-owner URL
docker run -e DATABASE_URL='<ltd_owner URL>' ltd python -m app.migrations
docker run -e DATABASE_URL='<ltd_owner URL>' ltd python -m app.migrations --status

# First administrator (interactive)
docker run -it --env-file prod.env ltd python -m app.admin_users create --email … --name "…"
```

Image environment set by the Dockerfile: `LTD_DISABLE_DOTENV=1`,
`FRONTEND_DIST_DIR=/app/frontend/dist`, `PYTHONUNBUFFERED=1`. Everything else
comes from the platform at run time (see [deployment-preflight.md](deployment-preflight.md)).
uvicorn reads two of its own variables: `FORWARDED_ALLOW_IPS` (proxy trust;
default `127.0.0.1`; set it to the platform's proxy range, never `*`) and
`WEB_CONCURRENCY` (process count; default 1).

Nothing migrates automatically: the API reports 503 on `/api/health` until
the schema is current, and the worker does not touch the schema.

Shutdown: the API (PID 1) finishes in-flight requests and exits on SIGTERM
(exit code 0 or 143 — uvicorn re-raises the signal after a clean shutdown);
the worker finishes its current job and exits 0. Give the worker a stop
timeout of at least 60 s.

## Why a Docker image (not a native build)

| Option | Verdict |
|---|---|
| Native Python build + Node | Render's documentation does not confirm Node in the Python build; installing Node by hand in the build command depends on undocumented build-environment details. Rejected. |
| CI-built frontend artifact | Getting `dist/` to a Git-based platform means committing build output or publishing an image to a registry with credentials — more moving parts. Rejected for now. |
| **Multi-stage Dockerfile** | Render documents Docker for web services and background workers (`runtime: docker`, per-service `dockerCommand`, pre-deploy command, multi-stage builds); the same image works on DigitalOcean, Railway, Fly or a VM. Node exists only in the build stage. **Selected.** |

## Provider-specific items still to verify

- The platform builds this Dockerfile from the repository root (Dockerfile
  path `./Dockerfile`, context `.`).
- `FORWARDED_ALLOW_IPS` = the platform's proxy source range (preflight D1).
- The platform passes `PORT`; otherwise the API listens on 8000.
- Platform environment variables passed as build arguments are ignored (no
  `ARG` in the Dockerfile); confirm in the first build log.
- Worker stop timeout ≥ 60 s (`maxShutdownDelaySeconds` on Render).
- Base-image digests are refreshed deliberately (security updates), not by a
  floating tag.

## Verification status (2026-11-11, pre-push audit)

| Check | Result |
|---|---|
| Lock regeneration with the documented commands | byte-identical to the committed locks (idempotent) |
| Runtime/dev lock consistency | 30 runtime pins ⊂ 35 dev pins, identical versions; every entry hashed |
| Runtime lock on the image platform (`pip download`, CPython 3.12, manylinux x86_64, `--require-hashes`, wheels only) | all 30 packages resolve |
| Clean backend install from `requirements-dev.lock` (new venv, no pip cache, hashes, wheels only) on local Python **3.12.3** | 35 packages; `pip check` clean |
| Full backend suite in that environment | 780 passed |
| Clean frontend install from the working tree (`npm ci`, empty cache) on local Node **24.21.0** | lockfile unchanged; typecheck passed; 27 tests passed; build passed |
| Dockerfile static review (no hadolint available) | digest-pinned bases; no `ARG`/`ADD`/`ENTRYPOINT`; numeric non-root `USER`; JSON `CMD` binding `0.0.0.0`; no migration in `CMD`/`RUN`; all `COPY` sources exist |
| `.dockerignore` (emulated matching) | 104 context files; `.env`, `var/`, caches, tests, docs, unexplained files excluded |
| Workflow | parses; validates against the SchemaStore GitHub-workflow JSON Schema (0 errors); all `run:` blocks and `image-smoke.sh` pass `bash -n`; actions SHA-pinned; expressions use only `github.ref`, `github.event_name` and a workflow constant |
| Earlier runtime-layout simulation (Task 12) | superseded as evidence by the image smoke test; it bound to 127.0.0.1 and did not use the image |
| **Not run locally** | `docker build`, the image smoke test, actionlint and shellcheck (no container runtime; downloading the actionlint binary was not permitted here). The first CI run performs them. Python 3.12.16 and Node 24.22.2 are exercised only in CI and the image |
