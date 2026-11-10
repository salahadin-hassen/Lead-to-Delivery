-- 0005: individual administrator accounts with mandatory TOTP MFA.
--
-- Replaces the single shared ADMIN_PASSWORD_HASH. Every administrator has
-- their own email, scrypt password hash and TOTP secret (AES-GCM encrypted
-- with ADMIN_MFA_ENCRYPTION_KEY, which never lives in the database).
-- Recovery codes are single-use and stored as SHA-256 hashes only.
-- Accounts are created and recovered with `python -m app.admin_users`
-- (server-side CLI); there are no default credentials.

CREATE TABLE admin_users (
    id                 uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    email              text        NOT NULL CHECK (email = lower(email) AND email ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$'
                                                   AND char_length(email) <= 254),
    display_name       text        NOT NULL CHECK (char_length(display_name) BETWEEN 1 AND 100),
    password_hash      text        NOT NULL CHECK (password_hash LIKE 'scrypt$%'),
    -- "v1:<base64 nonce||ciphertext>", AES-256-GCM over the base32 TOTP secret.
    totp_secret_enc    text        NOT NULL CHECK (totp_secret_enc LIKE 'v1:%'),
    -- Highest 30-second TOTP step accepted so far: a code is never accepted twice.
    totp_last_step     bigint      NOT NULL DEFAULT 0,
    disabled_at        timestamptz,
    last_login_at      timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX admin_users_email_key ON admin_users (email);

CREATE TABLE admin_recovery_codes (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    admin_user_id uuid        NOT NULL REFERENCES admin_users (id) ON DELETE CASCADE,
    code_hash     text        NOT NULL UNIQUE CHECK (code_hash ~ '^[0-9a-f]{64}$'),
    used_at       timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX admin_recovery_codes_user_idx ON admin_recovery_codes (admin_user_id) WHERE used_at IS NULL;

-- Sessions now belong to an administrator. Sessions issued under the shared
-- password have no owner and are revoked here.
ALTER TABLE admin_sessions ADD COLUMN admin_user_id uuid REFERENCES admin_users (id) ON DELETE CASCADE;
UPDATE admin_sessions SET revoked_at = now() WHERE revoked_at IS NULL;
CREATE INDEX admin_sessions_user_idx ON admin_sessions (admin_user_id) WHERE revoked_at IS NULL;

-- Per-account throttling (in addition to per-source and global). Lower-cased
-- email as typed; recorded even when no such account exists.
ALTER TABLE admin_login_attempts ADD COLUMN account_key text CHECK (char_length(account_key) <= 254);
CREATE INDEX admin_login_attempts_account_idx ON admin_login_attempts (account_key, created_at DESC)
    WHERE account_key IS NOT NULL;

CREATE TRIGGER admin_users_set_updated_at BEFORE UPDATE ON admin_users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
