-- 0006: worker liveness. Each worker process records a heartbeat at start and
-- at least every 15 seconds while it runs, so the API (a separate service)
-- can report whether any worker is alive: GET /api/health/worker.
-- One row per worker process; rows older than 7 days are pruned by workers.

CREATE TABLE worker_heartbeats (
    worker_id      text        PRIMARY KEY CHECK (char_length(worker_id) BETWEEN 1 AND 100),
    started_at     timestamptz NOT NULL DEFAULT now(),
    last_seen_at   timestamptz NOT NULL DEFAULT now(),
    stopped_at     timestamptz,
    jobs_processed bigint      NOT NULL DEFAULT 0 CHECK (jobs_processed >= 0)
);
CREATE INDEX worker_heartbeats_seen_idx ON worker_heartbeats (last_seen_at DESC);
