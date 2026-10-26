-- Rollback for schema/postgres_v2_tenant_rls.sql (exact inverse of the
-- schema-level changes).
--
-- Disclosed asymmetry: this restores the v1 shape (no RLS, no marker roles,
-- no grants) but CANNOT restore Supabase's anon/authenticated default grants
-- that the forward migration revoked. Those tables are new to this project
-- and the Data API stays disabled, so there is nothing to restore; if a real
-- project ever needs them back, re-grant explicitly and record why.
--
-- Refuses to run unless the hardening actually recorded rls_version='1'.
-- Drops the group roles only when nothing depends on them; if the operator
-- created login roles, revoke the membership first (clear error otherwise).

BEGIN;

DO $$
DECLARE
    version text;
BEGIN
    SELECT value INTO version FROM schema_meta WHERE key = 'rls_version';
    IF version IS DISTINCT FROM '1' THEN
        RAISE EXCEPTION
            'rls_version is "%": nothing to roll back (expected 1)',
            coalesce(version, '<missing>');
    END IF;
END $$;

DROP POLICY IF EXISTS tenant_isolation ON tenants;
DROP POLICY IF EXISTS tenant_isolation ON tenant_membership;
DROP POLICY IF EXISTS tenant_isolation ON results;
DROP POLICY IF EXISTS tenant_isolation ON audit_events;
DROP POLICY IF EXISTS tenant_isolation ON pdf_references;

ALTER TABLE tenants           NO FORCE ROW LEVEL SECURITY;
ALTER TABLE tenants           DISABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_membership NO FORCE ROW LEVEL SECURITY;
ALTER TABLE tenant_membership DISABLE ROW LEVEL SECURITY;
ALTER TABLE results           NO FORCE ROW LEVEL SECURITY;
ALTER TABLE results           DISABLE ROW LEVEL SECURITY;
ALTER TABLE audit_events      NO FORCE ROW LEVEL SECURITY;
ALTER TABLE audit_events      DISABLE ROW LEVEL SECURITY;
ALTER TABLE pdf_references    NO FORCE ROW LEVEL SECURITY;
ALTER TABLE pdf_references    DISABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE schema_meta, tenants, tenant_membership, results,
    audit_events, pdf_references FROM marker_app, marker_worker;
REVOKE ALL ON SCHEMA public FROM marker_app, marker_worker;

DELETE FROM schema_meta WHERE key = 'rls_version';

DO $$
BEGIN
    DROP ROLE IF EXISTS marker_worker;
EXCEPTION
    WHEN dependent_objects_still_exist THEN
        RAISE EXCEPTION
            'marker_worker still has dependent grants/memberships: revoke '
            'them from the operator login role before rolling back';
END $$;

DO $$
BEGIN
    DROP ROLE IF EXISTS marker_app;
EXCEPTION
    WHEN dependent_objects_still_exist THEN
        RAISE EXCEPTION
            'marker_app still has dependent grants/memberships: revoke them '
            'from the operator login role before rolling back';
END $$;

COMMIT;
