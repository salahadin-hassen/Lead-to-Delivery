-- Lead-to-Delivery: database roles for one agency deployment.
-- Run once as the PostgreSQL administrator (managed providers: the default
-- admin user), connected to the server, BEFORE the first migration.
-- Passwords are set separately (\password ltd_owner / \password ltd_app) or by
-- the provider; never put them in this file.
--
--   ltd_owner  owns the schema and runs migrations (release step only).
--   ltd_app    used by the API and the worker: read/write data, no DDL.
--
-- Verified on PostgreSQL 16 (staging, 2026-11-10): migrations as ltd_owner,
-- application as ltd_app; ltd_app cannot create or drop tables.

CREATE ROLE ltd_owner LOGIN;
CREATE ROLE ltd_app LOGIN;
CREATE DATABASE ltd OWNER ltd_owner;

\connect ltd
REVOKE ALL ON DATABASE ltd FROM PUBLIC;
GRANT CONNECT ON DATABASE ltd TO ltd_owner, ltd_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO ltd_owner;

-- Run the following as ltd_owner AFTER the first `python -m app.migrations`
-- (the default privileges then cover every later migration):
--
--   GRANT USAGE ON SCHEMA public TO ltd_app;
--   GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ltd_app;
--   GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ltd_app;
--   ALTER DEFAULT PRIVILEGES FOR ROLE ltd_owner IN SCHEMA public
--       GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ltd_app;
--   ALTER DEFAULT PRIVILEGES FOR ROLE ltd_owner IN SCHEMA public
--       GRANT USAGE, SELECT ON SEQUENCES TO ltd_app;
