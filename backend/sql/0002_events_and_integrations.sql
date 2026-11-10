-- 0002: inbound webhook events, integration outbox/queue, attempt history,
-- activity timeline and admin sessions.

-- --------------------------------------------------------- webhook_events
-- Only requests with a valid signature are stored (enforced by intake code).
CREATE TABLE webhook_events (
    id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    source            text        NOT NULL CHECK (source ~ '^[a-z0-9][a-z0-9_-]*$'),
    external_event_id text        NOT NULL CHECK (btrim(external_event_id) <> ''),
    event_type        text        NOT NULL CHECK (btrim(event_type) <> ''),
    payload           jsonb       NOT NULL,
    status            text        NOT NULL DEFAULT 'received'
                                  CHECK (status IN ('received', 'processed', 'rejected', 'failed')),
    error_code        text,
    duplicate_count   integer     NOT NULL DEFAULT 0 CHECK (duplicate_count >= 0),
    last_duplicate_at timestamptz,
    processed_at      timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    -- Event-level duplicate guard.
    CONSTRAINT webhook_events_source_event_key UNIQUE (source, external_event_id)
);
CREATE INDEX webhook_events_created_idx ON webhook_events (created_at DESC);
CREATE INDEX webhook_events_status_idx ON webhook_events (status, created_at DESC);
CREATE TRIGGER webhook_events_set_updated_at BEFORE UPDATE ON webhook_events
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ------------------------------------------------------- integration_jobs
-- Transactional outbox + durable queue. Rows are inserted in the same
-- transaction as the business change that requires them; the worker claims
-- them with FOR UPDATE SKIP LOCKED and calls external APIs outside any
-- transaction.
--
-- idempotency_key: operation-specific and permanent, e.g.
--   clickup.create_task:{task_id}
--   slack.notify:project_created:{project_id}
--   sheets.upsert_project:{project_id}:{sync_version}
-- coalesce_key (optional): groups jobs that may be merged while waiting,
--   e.g. sheets.upsert_project:{project_id}. At most ONE *pending* job per
--   coalesce_key; running/succeeded/dead jobs do not block a new pending one,
--   so later sync versions are never prevented.
CREATE TABLE integration_jobs (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    job_type        text        NOT NULL CHECK (job_type ~ '^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$'),
    idempotency_key text        NOT NULL CHECK (btrim(idempotency_key) <> ''),
    coalesce_key    text        CHECK (coalesce_key IS NULL OR btrim(coalesce_key) <> ''),
    subject_type    text        NOT NULL CHECK (subject_type ~ '^[a-z][a-z_]*$'),
    subject_id      uuid        NOT NULL,
    payload         jsonb       NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(payload) = 'object'),
    status          text        NOT NULL DEFAULT 'pending'
                                CHECK (status IN ('pending', 'running', 'succeeded', 'dead', 'cancelled')),
    attempts        integer     NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts    integer     NOT NULL DEFAULT 5 CHECK (max_attempts > 0),
    next_run_at     timestamptz NOT NULL DEFAULT now(),
    locked_by       text,
    locked_until    timestamptz,
    last_error_code text,
    last_error      text        CHECK (last_error IS NULL OR char_length(last_error) <= 1000),
    completed_at    timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT integration_jobs_idempotency_key_key UNIQUE (idempotency_key),
    CONSTRAINT integration_jobs_attempts_within_max CHECK (attempts <= max_attempts),
    -- A running job always carries its lease; other states never do.
    CONSTRAINT integration_jobs_lease_consistent
        CHECK ((status = 'running') = (locked_by IS NOT NULL AND locked_until IS NOT NULL)),
    CONSTRAINT integration_jobs_completed_consistent
        CHECK ((status IN ('succeeded', 'dead', 'cancelled')) = (completed_at IS NOT NULL))
);
CREATE UNIQUE INDEX integration_jobs_pending_coalesce_key
    ON integration_jobs (coalesce_key) WHERE status = 'pending' AND coalesce_key IS NOT NULL;
-- Worker claim paths: due pending jobs, and expired leases.
CREATE INDEX integration_jobs_due_idx ON integration_jobs (next_run_at) WHERE status = 'pending';
CREATE INDEX integration_jobs_lease_idx ON integration_jobs (locked_until) WHERE status = 'running';
-- Admin views and per-subject lookups.
CREATE INDEX integration_jobs_status_updated_idx ON integration_jobs (status, updated_at DESC);
CREATE INDEX integration_jobs_subject_idx ON integration_jobs (subject_type, subject_id);
CREATE TRIGGER integration_jobs_set_updated_at BEFORE UPDATE ON integration_jobs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- --------------------------------------------------- integration_attempts
-- One row per execution attempt. Inserted when the attempt starts (outcome
-- NULL) and completed afterwards, so a crash mid-call leaves visible evidence
-- of an attempt whose outcome is unknown.
CREATE TABLE integration_attempts (
    id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id         uuid        NOT NULL REFERENCES integration_jobs (id) ON DELETE CASCADE,
    attempt_number integer     NOT NULL CHECK (attempt_number > 0),
    worker_id      text,
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz,
    outcome        text        CHECK (outcome IN ('success', 'retryable', 'permanent', 'unknown')),
    http_status    integer     CHECK (http_status IS NULL OR http_status BETWEEN 100 AND 599),
    error_code     text,
    error_message  text        CHECK (error_message IS NULL OR char_length(error_message) <= 1000),
    external_id    text,
    duration_ms    integer     CHECK (duration_ms IS NULL OR duration_ms >= 0),
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT integration_attempts_job_attempt_key UNIQUE (job_id, attempt_number),
    CONSTRAINT integration_attempts_finished_consistent
        CHECK ((outcome IS NULL) = (finished_at IS NULL))
);
CREATE TRIGGER integration_attempts_set_updated_at BEFORE UPDATE ON integration_attempts
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- -------------------------------------------------------- activity_events
-- Append-only timeline (created_at only).
CREATE TABLE activity_events (
    id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id      uuid        REFERENCES clients (id),
    project_id     uuid        REFERENCES projects (id) ON DELETE CASCADE,
    actor_type     text        NOT NULL CHECK (actor_type IN ('system', 'admin', 'client', 'crm', 'worker')),
    event_type     text        NOT NULL CHECK (event_type ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$'),
    summary        text        NOT NULL CHECK (btrim(summary) <> ''),
    data           jsonb       NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(data) = 'object'),
    -- Only events flagged here may ever be shown in the client portal.
    client_visible boolean     NOT NULL DEFAULT false,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX activity_events_project_idx ON activity_events (project_id, created_at DESC);
CREATE INDEX activity_events_client_idx ON activity_events (client_id, created_at DESC);
CREATE INDEX activity_events_created_idx ON activity_events (created_at DESC);

-- --------------------------------------------------------- admin_sessions
-- Single-administrator sessions. Only the SHA-256 of the session token is stored.
CREATE TABLE admin_sessions (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash   text        NOT NULL CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    expires_at   timestamptz NOT NULL,
    revoked_at   timestamptz,
    last_seen_at timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT admin_sessions_token_hash_key UNIQUE (token_hash)
);
CREATE INDEX admin_sessions_expires_idx ON admin_sessions (expires_at);
CREATE TRIGGER admin_sessions_set_updated_at BEFORE UPDATE ON admin_sessions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
