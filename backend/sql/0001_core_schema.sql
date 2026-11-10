-- 0001: clients, onboarding, projects, milestones, tasks.
--
-- Conventions
--   * UUID primary keys (gen_random_uuid() is built into PostgreSQL 13+).
--     UUIDs are identifiers, never authorization.
--   * timestamptz everywhere. Mutable tables have created_at + updated_at,
--     maintained by the set_updated_at() trigger.
--   * Status columns are text + CHECK (simpler to evolve than enum types).
--   * Composite foreign keys such as (invitation_id, client_id) make it
--     impossible for a row to point at another client's invitation: a schema
--     level guard against cross-client leakage.

CREATE FUNCTION set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

-- ---------------------------------------------------------------- clients
CREATE TABLE clients (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    name            text        NOT NULL CHECK (btrim(name) <> ''),
    company         text        CHECK (company IS NULL OR btrim(company) <> ''),
    email           text        CHECK (email IS NULL OR (email = btrim(email) AND email LIKE '%_@_%')),
    phone           text,
    crm_source      text        CHECK (crm_source IS NULL OR crm_source ~ '^[a-z0-9][a-z0-9_-]*$'),
    crm_external_id text        CHECK (crm_external_id IS NULL OR btrim(crm_external_id) <> ''),
    status          text        NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT clients_crm_identity_complete
        CHECK ((crm_source IS NULL) = (crm_external_id IS NULL)),
    -- One client per CRM identity. NULLs (manually created clients) are distinct.
    CONSTRAINT clients_crm_identity_key UNIQUE (crm_source, crm_external_id)
);
-- One client per normalized email; multiple clients without an email are allowed.
-- Matching by email must never merge a different CRM identity or company:
-- intake code treats such a match as a conflict, not a merge.
CREATE UNIQUE INDEX clients_email_lower_key ON clients (lower(email));
CREATE INDEX clients_status_created_idx ON clients (status, created_at DESC);
CREATE TRIGGER clients_set_updated_at BEFORE UPDATE ON clients
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ------------------------------------------------------ project_templates
CREATE TABLE project_templates (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    key         text        NOT NULL CHECK (key ~ '^[a-z0-9][a-z0-9_]*$'),
    name        text        NOT NULL CHECK (btrim(name) <> ''),
    description text,
    version     integer     NOT NULL DEFAULT 1 CHECK (version > 0),
    -- Milestones and tasks; shape validated by app.schemas.templates.TemplateDefinition.
    definition  jsonb       NOT NULL CHECK (jsonb_typeof(definition) = 'object'),
    active      boolean     NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT project_templates_key_key UNIQUE (key)
);
CREATE TRIGGER project_templates_set_updated_at BEFORE UPDATE ON project_templates
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ------------------------------------------------- onboarding_invitations
-- One per won deal (or per manual invitation by the admin).
CREATE TABLE onboarding_invitations (
    id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id        uuid        NOT NULL REFERENCES clients (id),
    crm_source       text        CHECK (crm_source IS NULL OR crm_source ~ '^[a-z0-9][a-z0-9_-]*$'),
    crm_deal_id      text        CHECK (crm_deal_id IS NULL OR btrim(crm_deal_id) <> ''),
    deal_name        text,
    deal_value_cents bigint      CHECK (deal_value_cents IS NULL OR deal_value_cents >= 0),
    currency         text        CHECK (currency IS NULL OR currency ~ '^[A-Z]{3}$'),
    service_type     text,
    template_id      uuid        REFERENCES project_templates (id),
    status           text        NOT NULL DEFAULT 'pending'
                                 CHECK (status IN ('pending', 'submitted', 'expired', 'revoked')),
    expires_at       timestamptz NOT NULL,
    submitted_at     timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT onboarding_invitations_crm_deal_complete
        CHECK ((crm_source IS NULL) = (crm_deal_id IS NULL)),
    CONSTRAINT onboarding_invitations_submitted_consistent
        CHECK ((status = 'submitted') = (submitted_at IS NOT NULL)),
    -- Business-level duplicate guard: one onboarding per CRM deal, even if the
    -- CRM resends the deal under a new event id.
    CONSTRAINT onboarding_invitations_crm_deal_key UNIQUE (crm_source, crm_deal_id),
    -- Target for composite foreign keys that pin rows to the same client.
    CONSTRAINT onboarding_invitations_id_client_key UNIQUE (id, client_id)
);
CREATE INDEX onboarding_invitations_client_idx ON onboarding_invitations (client_id, created_at DESC);
CREATE INDEX onboarding_invitations_status_idx ON onboarding_invitations (status, expires_at);
CREATE TRIGGER onboarding_invitations_set_updated_at BEFORE UPDATE ON onboarding_invitations
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- --------------------------------------------------- client_access_tokens
-- Portal tokens. Only the SHA-256 hex digest of the token is stored.
CREATE TABLE client_access_tokens (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id     uuid        NOT NULL REFERENCES clients (id),
    invitation_id uuid,
    token_hash    text        NOT NULL CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    expires_at    timestamptz NOT NULL,
    revoked_at    timestamptz,
    last_used_at  timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT client_access_tokens_token_hash_key UNIQUE (token_hash),
    CONSTRAINT client_access_tokens_invitation_same_client
        FOREIGN KEY (invitation_id, client_id) REFERENCES onboarding_invitations (id, client_id)
);
CREATE INDEX client_access_tokens_client_idx ON client_access_tokens (client_id);
CREATE INDEX client_access_tokens_invitation_idx ON client_access_tokens (invitation_id);
CREATE TRIGGER client_access_tokens_set_updated_at BEFORE UPDATE ON client_access_tokens
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ------------------------------------------------- onboarding_submissions
-- Immutable once written (created_at only).
CREATE TABLE onboarding_submissions (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    invitation_id uuid        NOT NULL,
    client_id     uuid        NOT NULL REFERENCES clients (id),
    answers       jsonb       NOT NULL CHECK (jsonb_typeof(answers) = 'object'),
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT onboarding_submissions_invitation_key UNIQUE (invitation_id),
    CONSTRAINT onboarding_submissions_invitation_same_client
        FOREIGN KEY (invitation_id, client_id) REFERENCES onboarding_invitations (id, client_id)
);
CREATE INDEX onboarding_submissions_client_idx ON onboarding_submissions (client_id, created_at DESC);

-- --------------------------------------------------------------- projects
CREATE TABLE projects (
    id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id        uuid        NOT NULL REFERENCES clients (id),
    invitation_id    uuid,
    template_id      uuid        REFERENCES project_templates (id),
    template_version integer     CHECK (template_version IS NULL OR template_version > 0),
    name             text        NOT NULL CHECK (btrim(name) <> ''),
    status           text        NOT NULL DEFAULT 'active'
                                 CHECK (status IN ('active', 'on_hold', 'completed', 'cancelled')),
    start_date       date,
    due_date         date,
    -- Incremented whenever data mirrored to external systems changes; used in
    -- per-version sync idempotency keys (sheets.upsert_project:{id}:{sync_version}).
    sync_version     bigint      NOT NULL DEFAULT 1 CHECK (sync_version > 0),
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT projects_dates_ordered CHECK (due_date IS NULL OR start_date IS NULL OR due_date >= start_date),
    -- One project per invitation; manually created projects have no invitation.
    CONSTRAINT projects_invitation_key UNIQUE (invitation_id),
    CONSTRAINT projects_invitation_same_client
        FOREIGN KEY (invitation_id, client_id) REFERENCES onboarding_invitations (id, client_id)
);
CREATE INDEX projects_client_idx ON projects (client_id, created_at DESC);
CREATE INDEX projects_status_idx ON projects (status, created_at DESC);
CREATE TRIGGER projects_set_updated_at BEFORE UPDATE ON projects
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ------------------------------------------------------------- milestones
CREATE TABLE milestones (
    id                     uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id             uuid        NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    template_milestone_key text,
    name                   text        NOT NULL CHECK (btrim(name) <> ''),
    position               integer     NOT NULL CHECK (position >= 0),
    due_date               date,
    status                 text        NOT NULL DEFAULT 'pending'
                                       CHECK (status IN ('pending', 'in_progress', 'done')),
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now(),
    -- Deferrable so a future reorder can swap positions inside one transaction.
    CONSTRAINT milestones_project_position_key UNIQUE (project_id, position)
        DEFERRABLE INITIALLY IMMEDIATE,
    CONSTRAINT milestones_project_template_key UNIQUE (project_id, template_milestone_key),
    CONSTRAINT milestones_id_project_key UNIQUE (id, project_id)
);
CREATE TRIGGER milestones_set_updated_at BEFORE UPDATE ON milestones
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ------------------------------------------------------------------ tasks
CREATE TABLE tasks (
    id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id        uuid        NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    milestone_id      uuid,
    -- Set for template-generated tasks; NULL for tasks added by hand.
    template_task_key text,
    title             text        NOT NULL CHECK (btrim(title) <> ''),
    description       text,
    status            text        NOT NULL DEFAULT 'todo' CHECK (status IN ('todo', 'in_progress', 'done')),
    position          integer     NOT NULL DEFAULT 0 CHECK (position >= 0),
    assignee_name     text,
    assignee_email    text        CHECK (assignee_email IS NULL OR assignee_email LIKE '%_@_%'),
    due_date          date,
    client_visible    boolean     NOT NULL DEFAULT true,
    completed_at      timestamptz,
    clickup_task_id   text        CHECK (clickup_task_id IS NULL OR btrim(clickup_task_id) <> ''),
    clickup_url       text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    -- A milestone must belong to the same project; deleting it un-assigns the
    -- task (only milestone_id is nulled, never project_id).
    CONSTRAINT tasks_milestone_same_project
        FOREIGN KEY (milestone_id, project_id) REFERENCES milestones (id, project_id)
        ON DELETE SET NULL (milestone_id),
    -- Project generation can never create the same template task twice.
    CONSTRAINT tasks_project_template_task_key UNIQUE (project_id, template_task_key),
    CONSTRAINT tasks_clickup_task_id_key UNIQUE (clickup_task_id)
);
CREATE INDEX tasks_project_position_idx ON tasks (project_id, position);
CREATE INDEX tasks_milestone_idx ON tasks (milestone_id);
CREATE TRIGGER tasks_set_updated_at BEFORE UPDATE ON tasks
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
