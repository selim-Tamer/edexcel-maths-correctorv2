-- schema/postgres_v5_membership_access.sql
--
-- 0005: production membership RESOLUTION access (subject -> tenant).
--
-- DRAFT artifact: NOT applied anywhere. Applying it needs review of the exact
-- SQL AND separate explicit user authorization (see schema/MIGRATIONS.md).
--
-- Why this exists: 0002 gave `tenant_membership` an RLS policy keyed only by
-- `app.tenant_id`, and no runtime grant at all - so nothing could answer
-- "which tenant does this VERIFIED subject belong to?" without already
-- knowing the tenant (or using a privileged/admin connection). The API's
-- default resolver is still the local SQLite dev store; a production
-- composition needs one narrowly-scoped read.
--
-- Design:
--   * a SECOND, SELECT-only policy keyed by `app.subject` (the API sets it
--     from the verified token subject, transaction-locally);
--   * column-level SELECT for `marker_app` on the five columns the resolver
--     reads - no INSERT/UPDATE/DELETE, so the API cannot create or revive a
--     membership;
--   * `app.subject` is a policy input, NOT an authenticity proof. A
--     compromised database credential can set it; RLS is defence in depth.
--     The tests state that limit instead of hiding it.
--   * fail closed: with neither GUC set, no membership row is visible;
--     inactive rows are filtered by the resolver query, not by the policy.
--   * idempotent: every statement can run twice; no implicit migration.

-- 0. Preflight.
DO $$
BEGIN
    IF to_regclass('public.tenant_membership') IS NULL THEN
        RAISE EXCEPTION 'schema v1 missing: apply schema/postgres_v1.sql first';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM schema_meta
                   WHERE key = 'rls_version' AND value = '1') THEN
        RAISE EXCEPTION
            'hardening missing: apply schema/postgres_v2_tenant_rls.sql first';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles
                   WHERE rolname = 'marker_app' AND NOT rolcanlogin) THEN
        RAISE EXCEPTION 'marker_app missing or wrong shape: apply 0002 first';
    END IF;
END $$;

-- 1. Self-read policy. Permissive policies OR together, so this ADDS to
--    tenant_isolation: a tenant-bound connection still sees only its tenant,
--    while a subject-bound connection sees only rows for that subject.
DROP POLICY IF EXISTS membership_self_read ON tenant_membership;
CREATE POLICY membership_self_read ON tenant_membership
    FOR SELECT
    USING (subject = NULLIF(current_setting('app.subject', true), ''));

-- 2. Least privilege: read the five columns the resolver needs, nothing else.
GRANT SELECT (tenant_id, subject, role, active, updated_at)
    ON tenant_membership TO marker_app;

-- 3. Version marker.
INSERT INTO schema_meta (key, value) VALUES ('membership_access_version', '1')
    ON CONFLICT (key) DO NOTHING;
