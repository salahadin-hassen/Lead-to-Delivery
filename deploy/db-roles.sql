-- Lead-to-Delivery: database roles for one agency deployment (self-managed
-- PostgreSQL or a provider whose admin user may create databases).
-- Run once as the PostgreSQL administrator, BEFORE the first migration.
-- Neon: do NOT use this file. Neon accepts only plaintext SQL passwords, so
-- ltd_app is created by Neon's control plane (`neon roles create --access
-- read_write --schema public`) and narrowed to the same policy by
-- scripts/neon-staging-setup.sh (docs/free-staging-options.md section 5).
--
-- Passwords are set separately with psql's \password (ltd_owner, ltd_app), which
-- sends only a SCRAM verifier, or by the provider; never put them in this file.
--
--   ltd_owner  owns the database and the schema; runs migrations (release step only).
--   ltd_app    used by the API and the worker: DML only, no DDL, owns nothing.
--
-- After migrations, as ltd_owner (and again after every migration session):
--   psql -1 -v ON_ERROR_STOP=1 -f deploy/db-app-grants.sql -f deploy/db-app-check.sql

CREATE ROLE ltd_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE ltd_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    CONNECTION LIMIT 20;
CREATE DATABASE ltd OWNER ltd_owner;

\connect ltd
REVOKE ALL ON DATABASE ltd FROM PUBLIC;
GRANT CONNECT ON DATABASE ltd TO ltd_owner, ltd_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO ltd_owner;
