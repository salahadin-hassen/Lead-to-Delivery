-- Lead-to-Delivery: privileges of the application role `ltd_app`.
--
-- Run as the schema OWNER (ltd_owner, or the provider's owner role such as
-- Neon's neondb_owner) AFTER migrations, in ONE transaction:
--     psql -1 -v ON_ERROR_STOP=1 -f deploy/db-app-grants.sql -f deploy/db-app-check.sql
-- Idempotent: run it again after every migration session. Used verbatim by the
-- Neon setup helper and the CI image smoke test, and referenced by
-- deploy/db-roles.sql and docs/render-staging.md.
--
-- The file first REMOVES every privilege ltd_app holds in this database (and
-- every default privilege the owner set for it), then grants exactly the
-- policy below. So it also narrows a role that a provider created with a
-- broader grant set (e.g. `neon roles create --access read_write`), and a
-- re-run can never widen anything. deploy/db-app-check.sql then proves the
-- result is exactly this policy.
--
-- Policy (derived from the runtime SQL in backend/app; see
-- docs/render-staging.md section 6):
-- * CONNECT on the database; USAGE (not CREATE) on schema public; no TEMP.
-- * DML on the operational tables: the API and worker insert, update, delete
--   and lock rows (SELECT ... FOR UPDATE needs UPDATE privilege).
-- * schema_migrations and project_templates are READ-ONLY: only migrations
--   write them, and no runtime query locks them.
-- * Never TRUNCATE, REFERENCES, TRIGGER or MAINTAIN; never ownership, CREATE
--   or any other DDL right (ltd_app owns nothing).
-- * Default privileges give tables and sequences created by later migrations
--   the same DML access; a migration that adds a table which must stay
--   read-only for the application must be listed in the REVOKE below.
-- Role attributes and memberships are not changed here (that needs the role's
-- creator); deploy/db-app-check.sql asserts them.

-- 1. Reset: nothing left over from earlier runs or from the provider.
DO $$
BEGIN
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM ltd_app', current_database());
END $$;
REVOKE ALL ON SCHEMA public FROM ltd_app;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM ltd_app;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM ltd_app;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM ltd_app;
ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER REVOKE ALL ON TABLES FROM ltd_app;
ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER REVOKE ALL ON SEQUENCES FROM ltd_app;
ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER REVOKE ALL ON FUNCTIONS FROM ltd_app;
ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER REVOKE ALL ON TYPES FROM ltd_app;
ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER REVOKE ALL ON SCHEMAS FROM ltd_app;
ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER IN SCHEMA public REVOKE ALL ON TABLES FROM ltd_app;
ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER IN SCHEMA public REVOKE ALL ON SEQUENCES FROM ltd_app;
ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM ltd_app;
ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER IN SCHEMA public REVOKE ALL ON TYPES FROM ltd_app;

-- 2. Grant exactly the policy.
DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO ltd_app', current_database());
END $$;
GRANT USAGE ON SCHEMA public TO ltd_app;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ltd_app;
-- Read-only tables (skipped before the first migration, when they do not exist yet).
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['schema_migrations', 'project_templates'] LOOP
        IF to_regclass(format('public.%I', t)) IS NOT NULL THEN
            EXECUTE format('REVOKE INSERT, UPDATE, DELETE ON public.%I FROM ltd_app', t);
        END IF;
    END LOOP;
END $$;

-- The schema currently has no sequences (UUID keys); kept for later migrations.
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ltd_app;

ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ltd_app;
ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO ltd_app;
