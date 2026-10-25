-- M5 production hardening: schema v1 -> RLS + least-privilege roles.
--
-- APPLIED EXPLICITLY by an operator (never by the application, never in CI
-- against a real project). Nothing here is provisioned or paid for.
--
-- What this migration does
-- ------------------------
-- 1. Creates two NOLOGIN group roles:
--      marker_app    - the API runtime
--      marker_worker - the background marking worker
--    Login roles are created separately by the operator with a password that
--    is stored only in Secret Manager (see docs/BETA_DEPLOY_SETUP.md). No
--    password ever appears in this repository.
-- 2. Grants least privilege on the v1 tables: USAGE on the schema, SELECT on
--    schema_meta, and only the verbs the shipped adapter issues on results
--    (SELECT/INSERT/UPDATE) and audit_events (SELECT/INSERT). No DDL, no
--    TRUNCATE, no DELETE, no audit_events UPDATE, no REFERENCES, no TRIGGER,
--    no ownership.
-- 3. Enables ROW LEVEL SECURITY with FORCE on every tenant-bearing table.
--    FORCE makes the policy apply to the table OWNER too. It does NOT
--    constrain a superuser or a role with BYPASSRLS - PostgreSQL always
--    bypasses RLS for those. This migration therefore does not *claim* that
--    privileged access is constrained: the runtime roles are verified
--    NOSUPERUSER/NOBYPASSRLS (step 2), and the privileged path is authorised
--    in application code by explicit tenant_id predicates derived from the
--    verified membership. tests/test_postgres_rls_contract.py demonstrates
--    both sides: the table owner under FORCE sees only its bound tenant, a
--    BYPASSRLS role sees every tenant. The policy compares tenant_id with the
--    transaction-local setting app.tenant_id, which the application sets from
--    the VERIFIED token's membership - never from a client-supplied value.
-- 4. Fails closed when app.tenant_id is unset: current_setting(..., true)
--    returns NULL, the comparison is not true, and zero rows are visible.
-- 5. Revokes any Supabase anon/authenticated grants on these tables when
--    those roles exist in the cluster. The project-level Data API is already
--    disabled; this is defence in depth for a project that turns it back on.
--
-- Schema version note
-- -------------------
-- The TABLE SHAPE does not change, so schema_meta.schema_version stays '1'
-- (postgres_store fails closed on anything else). Hardening state is recorded
-- separately as rls_version='1' so the adapter keeps working unchanged.
--
-- Rollback: schema/postgres_v2_tenant_rls_rollback.sql (reviewed, exact
-- inverse; it does NOT restore third-party default grants - disclosed there).
--
-- Runbook: schema/MIGRATIONS.md (ordered steps, per-step backout, mandatory
-- pre-migration pg_dump - Supabase Free has NO platform backups/PITR - and
-- the SECURITY DEFINER inventory that must be rerun on the real project).
--
-- Idempotent: safe to re-run.

BEGIN;

-- 1. Preflight: v1 must already be applied. Refuse anything unexpected.
DO $$
DECLARE
    version text;
BEGIN
    IF to_regclass('public.schema_meta') IS NULL THEN
        RAISE EXCEPTION 'schema v1 missing: apply schema/postgres_v1.sql first';
    END IF;
    SELECT value INTO version FROM schema_meta WHERE key = 'schema_version';
    IF version IS DISTINCT FROM '1' THEN
        RAISE EXCEPTION
            'unexpected schema_version "%": this migration targets v1',
            coalesce(version, '<missing>');
    END IF;
END $$;

-- 2. Group roles (NOLOGIN: they carry privileges, never credentials).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'marker_app') THEN
        CREATE ROLE marker_app NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'marker_worker') THEN
        CREATE ROLE marker_worker NOLOGIN;
    END IF;
END $$;

-- Hardening check: these roles must never escalate or bypass RLS. The
-- attributes are set at CREATE ROLE time (defaults are already NOSUPERUSER /
-- NOBYPASSRLS / NOCREATEDB / NOCREATEROLE / NOREPLICATION); verifying instead
-- of ALTERing keeps this migration runnable by a project owner that is not a
-- full superuser (Supabase's `postgres` role is not one).
DO $$
DECLARE
    bad text;
BEGIN
    SELECT string_agg(rolname, ', ') INTO bad
    FROM pg_roles
    WHERE rolname IN ('marker_app', 'marker_worker')
      AND (rolsuper OR rolbypassrls OR rolcreatedb OR rolcreaterole
           OR rolreplication);
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION
            'roles % already have elevated attributes: drop/recreate them '
            'as NOLOGIN group roles before applying this migration', bad;
    END IF;
END $$;

-- 3. Least privilege for the runtime and the worker.
REVOKE ALL ON SCHEMA public FROM marker_app, marker_worker;
GRANT USAGE ON SCHEMA public TO marker_app, marker_worker;

REVOKE ALL ON TABLE schema_meta, tenants, tenant_membership, results,
    audit_events, pdf_references FROM marker_app, marker_worker;

-- schema_meta is read-only at runtime (schema version check only).
GRANT SELECT ON schema_meta TO marker_app, marker_worker;

-- Exactly the verbs the SHIPPED adapter issues (postgres_store.py):
--   results       SELECT, INSERT, UPDATE (result_revision advancement)
--   audit_events  SELECT, INSERT
-- Nothing else. In particular there is NO DELETE anywhere and NO UPDATE on
-- audit_events: the audit trail is append-only, so a compromised runtime
-- credential must not be able to rewrite verification history.
--
-- tenants / tenant_membership / pdf_references get no grant yet: no shipped
-- PostgreSQL statement touches them (membership and documents are still the
-- development store). Their slices add their own numbered grants when their
-- code lands - least privilege for what exists, not for what might.
--
-- Retention/purge is NOT a runtime verb. The storage/retention slice must
-- create a separate elevated role for it (see schema/MIGRATIONS.md §4.1 and
-- §7); widening marker_app/marker_worker is not an acceptable route.
GRANT SELECT, INSERT, UPDATE ON results TO marker_app, marker_worker;
GRANT SELECT, INSERT ON audit_events TO marker_app, marker_worker;

-- 4. Defence in depth for Supabase's API roles, when they exist.
DO $$
DECLARE
    api_role text;
BEGIN
    FOREACH api_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = api_role) THEN
            EXECUTE format(
                'REVOKE ALL ON TABLE schema_meta, tenants, tenant_membership, '
                'results, audit_events, pdf_references FROM %I', api_role);
        END IF;
    END LOOP;
END $$;

-- 5. Row level security. FORCE so the owner is subject too; the only way to
--    see tenant rows is a verified, transaction-local app.tenant_id.
ALTER TABLE tenants          ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants          FORCE  ROW LEVEL SECURITY;
ALTER TABLE tenant_membership ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_membership FORCE  ROW LEVEL SECURITY;
ALTER TABLE results          ENABLE ROW LEVEL SECURITY;
ALTER TABLE results          FORCE  ROW LEVEL SECURITY;
ALTER TABLE audit_events     ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_events     FORCE  ROW LEVEL SECURITY;
ALTER TABLE pdf_references   ENABLE ROW LEVEL SECURITY;
ALTER TABLE pdf_references   FORCE  ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON tenants;
CREATE POLICY tenant_isolation ON tenants
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''));

DROP POLICY IF EXISTS tenant_isolation ON tenant_membership;
CREATE POLICY tenant_isolation ON tenant_membership
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''));

DROP POLICY IF EXISTS tenant_isolation ON results;
CREATE POLICY tenant_isolation ON results
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''));

DROP POLICY IF EXISTS tenant_isolation ON audit_events;
CREATE POLICY tenant_isolation ON audit_events
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''));

DROP POLICY IF EXISTS tenant_isolation ON pdf_references;
CREATE POLICY tenant_isolation ON pdf_references
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''));

-- Hardening state (NOT the adapter's schema_version, which stays '1').
INSERT INTO schema_meta (key, value) VALUES ('rls_version', '1')
    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;

COMMIT;
