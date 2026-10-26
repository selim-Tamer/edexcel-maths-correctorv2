-- schema/postgres_v3_jobs_rollback.sql
--
-- Exact inverse of schema/postgres_v3_jobs.sql (0003). Reviewable artifact:
-- NOT applied anywhere without IR approval + explicit user authorization.
--
-- Disclosure: this backout DROPS marking_jobs, so it drops every job row
-- (queued/running/succeeded/failed history). Take the schema/MIGRATIONS.md 2
-- pg_dump first and run the 2.1 backout drill on a scratch copy. A project
-- holding real jobs must not run this file as a "reset".
--
-- It restores the pre-0003 shape: no marking_jobs table, no policy, no
-- grants, no jobs_version marker. 0002's roles and grants on the five tenant
-- tables are untouched.

-- 0. Preconditions: 0003 applied, and 0002 still present.
DO $$
BEGIN
    IF to_regclass('public.marking_jobs') IS NULL THEN
        RAISE EXCEPTION 'marking_jobs is absent: 0003 was not applied here';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'marker_app') THEN
        RAISE EXCEPTION
            'marker_app missing: run the 0002 rollback in its documented order';
    END IF;
END $$;

-- 1. Grants first (the table drop would remove them too; explicit is better
--    evidence).
REVOKE ALL ON TABLE marking_jobs FROM marker_app, marker_worker;

-- 2. Policy and RLS.
DROP POLICY IF EXISTS tenant_isolation ON marking_jobs;
ALTER TABLE marking_jobs DISABLE ROW LEVEL SECURITY;
ALTER TABLE marking_jobs NO FORCE ROW LEVEL SECURITY;

-- 3. Version marker.
DELETE FROM schema_meta WHERE key = 'jobs_version';

-- 4. The table itself (and its index).
DROP TABLE IF EXISTS marking_jobs;

-- 5. Re-assert 0002's least privilege so the audit trail cannot be rewritten
--    by anything this migration's life cycle touched.
REVOKE ALL ON TABLE schema_meta, tenants, tenant_membership, results,
    audit_events, pdf_references FROM marker_app, marker_worker;
GRANT SELECT ON schema_meta TO marker_app, marker_worker;
GRANT SELECT, INSERT, UPDATE ON results TO marker_app, marker_worker;
GRANT SELECT, INSERT ON audit_events TO marker_app, marker_worker;
