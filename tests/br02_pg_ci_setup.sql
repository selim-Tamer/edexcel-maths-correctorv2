-- Future CI execution only. No production or Windows role activation.
BEGIN;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE ROLE marker_app_login LOGIN INHERIT NOSUPERUSER NOBYPASSRLS
  NOCREATEDB NOCREATEROLE NOREPLICATION CONNECTION LIMIT 8;
CREATE ROLE marker_worker_login LOGIN INHERIT NOSUPERUSER NOBYPASSRLS
  NOCREATEDB NOCREATEROLE NOREPLICATION CONNECTION LIMIT 4;
CREATE ROLE br02_ci_owner LOGIN NOSUPERUSER NOBYPASSRLS
  NOCREATEDB NOCREATEROLE NOREPLICATION CONNECTION LIMIT 8;
GRANT marker_app TO marker_app_login;
GRANT marker_worker TO marker_worker_login;
GRANT CONNECT ON DATABASE br02_ci TO marker_app_login, marker_worker_login, br02_ci_owner;
GRANT USAGE ON SCHEMA public TO marker_app_login, marker_worker_login, br02_ci_owner;
DO $$
DECLARE item record;
BEGIN
  FOR item IN SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='public'
  LOOP EXECUTE format('ALTER TABLE public.%I OWNER TO br02_ci_owner', item.tablename); END LOOP;
END $$;
ALTER ROLE marker_app_login SET statement_timeout = '15s';
ALTER ROLE marker_worker_login SET statement_timeout = '15s';
ALTER ROLE br02_ci_owner SET statement_timeout = '15s';
ALTER ROLE marker_app_login SET lock_timeout = '3s';
ALTER ROLE marker_worker_login SET lock_timeout = '3s';
ALTER ROLE br02_ci_owner SET lock_timeout = '3s';
-- Passwords and run marker are parameterized by the future CI bootstrap.
COMMIT;
