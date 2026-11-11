-- Lead-to-Delivery: proves that `ltd_app` has EXACTLY the policy of
-- deploy/db-app-grants.sql and the attributes of deploy/db-roles.sql - nothing
-- more. Read-only; any role may run it (owner after granting, or ltd_app
-- itself). Fails with an error listing every deviation; silent on success.
--
-- Checks: role attributes (no SUPERUSER/CREATEROLE/CREATEDB/REPLICATION/
-- BYPASSRLS, LOGIN, CONNECTION LIMIT 20); membership in NO other role (so not
-- neon_superuser, pg_read_all_data, ...); owns nothing in any database; holds
-- privileges on nothing except this database, schema public and the tables
-- and sequences in it; exact privilege sets on each of those; exact default
-- privileges; no column grants, no grant options; no TEMP or CREATE through
-- PUBLIC.
DO $$
DECLARE
    app oid := (SELECT oid FROM pg_roles WHERE rolname = 'ltd_app');
    db  oid := (SELECT oid FROM pg_database WHERE datname = current_database());
    dba oid := (SELECT datdba FROM pg_database WHERE datname = current_database());
    problems text[] := '{}';
    r record;
BEGIN
    IF app IS NULL THEN
        RAISE EXCEPTION 'ltd_app policy check: role ltd_app does not exist';
    END IF;

    SELECT * INTO r FROM pg_roles WHERE oid = app;
    IF r.rolsuper OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication OR r.rolbypassrls
       OR NOT r.rolcanlogin OR r.rolconnlimit <> 20 THEN
        problems := problems || format('attributes: super=%s createrole=%s createdb=%s replication=%s '
            'bypassrls=%s login=%s connlimit=%s (want false x5, login true, 20)', r.rolsuper,
            r.rolcreaterole, r.rolcreatedb, r.rolreplication, r.rolbypassrls, r.rolcanlogin, r.rolconnlimit);
    END IF;

    FOR r IN SELECT g.rolname FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid
             WHERE m.member = app LOOP
        problems := problems || format('member of role %s', r.rolname);
    END LOOP;

    -- Shared dependencies cover every database of the cluster.
    FOR r IN SELECT d.dbid, d.classid::regclass AS cls, d.objid, d.deptype FROM pg_shdepend d
             WHERE d.refclassid = 'pg_authid'::regclass AND d.refobjid = app
               AND NOT (d.deptype = 'a' AND (
                     (d.dbid = 0 AND d.classid = 'pg_database'::regclass AND d.objid = db)
                  OR (d.dbid = db AND d.classid = 'pg_namespace'::regclass AND d.objid = 'public'::regnamespace)
                  OR (d.dbid = db AND d.classid = 'pg_default_acl'::regclass)
                  OR (d.dbid = db AND d.classid = 'pg_class'::regclass AND d.objsubid = 0
                      AND d.objid IN (SELECT c.oid FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace
                                      AND c.relkind IN ('r', 'p', 'S'))))) LOOP
        problems := problems || format('%s on %s object %s in database oid %s',
            CASE r.deptype WHEN 'o' THEN 'owns' WHEN 'a' THEN 'privilege' ELSE 'dependency ' || r.deptype END,
            r.cls, r.objid, r.dbid);
    END LOOP;

    -- Exact privilege sets (no grant options anywhere).
    FOR r IN
        WITH acl AS (
            SELECT 'database ' || datname AS obj, a.privilege_type AS p, a.is_grantable AS g
              FROM pg_database, aclexplode(datacl) a WHERE oid = db AND a.grantee = app
            UNION ALL
            SELECT 'schema public', a.privilege_type, a.is_grantable
              FROM pg_namespace, aclexplode(nspacl) a WHERE oid = 'public'::regnamespace AND a.grantee = app
            UNION ALL
            SELECT 'relation ' || c.relname, a.privilege_type, a.is_grantable
              FROM pg_class c, aclexplode(c.relacl) a
             WHERE c.relnamespace = 'public'::regnamespace AND a.grantee = app),
        want AS (
            SELECT 'database ' || current_database() AS obj, 'CONNECT' AS p
            UNION ALL SELECT 'schema public', 'USAGE'
            UNION ALL
            SELECT 'relation ' || c.relname, p
              FROM pg_class c,
                   unnest(CASE WHEN c.relkind = 'S' THEN ARRAY['SELECT', 'USAGE']
                               WHEN c.relname IN ('schema_migrations', 'project_templates') THEN ARRAY['SELECT']
                               ELSE ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE'] END) p
             WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p', 'S'))
        SELECT 'extra ' || obj || ': ' || p AS msg FROM (SELECT obj, p FROM acl EXCEPT SELECT obj, p FROM want) x
        UNION ALL
        SELECT 'missing ' || obj || ': ' || p FROM (SELECT obj, p FROM want EXCEPT SELECT obj, p FROM acl) y
        UNION ALL
        SELECT 'grant option ' || obj || ': ' || p FROM acl WHERE g
    LOOP
        problems := problems || r.msg;
    END LOOP;

    FOR r IN SELECT c.relname, a.attname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid,
                    aclexplode(a.attacl) x WHERE x.grantee = app LOOP
        problems := problems || format('column grant on %s.%s', r.relname, r.attname);
    END LOOP;

    -- Default privileges: exactly the two entries of db-app-grants.sql, set by
    -- the database owner (the role that runs migrations) for schema public.
    FOR r IN
        WITH have AS (
            SELECT d.defaclrole, d.defaclnamespace, d.defaclobjtype, a.privilege_type AS p, a.is_grantable AS g
              FROM pg_default_acl d, aclexplode(d.defaclacl) a WHERE a.grantee = app),
        want AS (
            SELECT dba AS defaclrole, 'public'::regnamespace::oid AS defaclnamespace, t, p
              FROM (VALUES ('r'::"char", 'SELECT'), ('r', 'INSERT'), ('r', 'UPDATE'), ('r', 'DELETE'),
                           ('S', 'SELECT'), ('S', 'USAGE')) v(t, p))
        SELECT format('extra default privilege %s on objtype %s (by %s, schema %s)', p, defaclobjtype,
                      defaclrole::regrole, NULLIF(defaclnamespace, 0)::regnamespace) AS msg
          FROM (SELECT defaclrole, defaclnamespace, defaclobjtype, p FROM have
                EXCEPT SELECT defaclrole, defaclnamespace, t, p FROM want) x
        UNION ALL
        SELECT format('missing default privilege %s on objtype %s', p, t)
          FROM (SELECT defaclrole, defaclnamespace, t, p FROM want
                EXCEPT SELECT defaclrole, defaclnamespace, defaclobjtype, p FROM have) y
        UNION ALL
        SELECT 'default privilege with grant option' FROM have WHERE g
    LOOP
        problems := problems || r.msg;
    END LOOP;

    -- Effective rights, including anything granted to PUBLIC.
    IF has_database_privilege(app, db, 'CREATE') THEN problems := problems || 'CREATE on the database'::text; END IF;
    IF has_database_privilege(app, db, 'TEMP') THEN problems := problems || 'TEMP on the database (revoke from PUBLIC)'::text; END IF;
    IF has_schema_privilege(app, 'public', 'CREATE') THEN problems := problems || 'CREATE on schema public (revoke from PUBLIC)'::text; END IF;

    IF cardinality(problems) > 0 THEN
        RAISE EXCEPTION 'ltd_app policy check FAILED (% problem(s)): %', cardinality(problems),
            array_to_string(problems, '; ');
    END IF;
END $$;
