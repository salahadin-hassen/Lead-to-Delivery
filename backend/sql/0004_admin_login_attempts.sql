-- 0004: durable login throttling for the single administrator.
--
-- Why a new table: throttling must survive API restarts and work across
-- several API processes, so it cannot live in memory, and no existing table
-- records failed logins (admin_sessions only holds successful ones).
--
-- source_key is the TCP peer address seen by the API (never a client-supplied
-- header such as X-Forwarded-For). Append-only; old rows can be deleted at any
-- time without affecting anything but the throttle window.

CREATE TABLE admin_login_attempts (
    id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    source_key text        NOT NULL CHECK (char_length(source_key) BETWEEN 1 AND 100),
    succeeded  boolean     NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX admin_login_attempts_source_idx ON admin_login_attempts (source_key, created_at DESC);
CREATE INDEX admin_login_attempts_created_idx ON admin_login_attempts (created_at DESC);
