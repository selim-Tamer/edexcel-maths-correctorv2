-- schema/postgres_v7_assignments_rollback.sql
--
-- Exact inverse of schema/postgres_v7_assignments.sql (0007). Reviewable
-- artifact: NOT applied anywhere without IR approval + explicit user
-- authorization.
--
-- Disclosure: this backout DROPS assignments, so every server-owned
-- assignment row is lost. Take the schema/MIGRATIONS.md 2 pg_dump first and
-- run the 2.1 backout drill on a scratch copy. Assignments on a live beta
-- must not be reset this way.
--
-- It restores the pre-0007 shape: no assignments table, policy, grants or
-- assignments_version marker. 0001-0005 objects are untouched.

-- 0. Preconditions.
DO $$
BEGIN
    IF to_regclass('public.assignments') IS NULL THEN
        RAISE EXCEPTION 'assignments is absent: 0007 was not applied here';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'marker_app') THEN
        RAISE EXCEPTION
            'marker_app missing: run the 0002 rollback in its documented order';
    END IF;
END $$;

-- 1. Grants first (the table drop removes them too; explicit is better).
REVOKE ALL ON TABLE assignments FROM marker_app, marker_worker;

-- 2. Policy and RLS.
DROP POLICY IF EXISTS tenant_isolation ON assignments;
ALTER TABLE assignments DISABLE ROW LEVEL SECURITY;
ALTER TABLE assignments NO FORCE ROW LEVEL SECURITY;

-- 3. Version marker.
DELETE FROM schema_meta WHERE key = 'assignments_version';

-- 4. The table (and its index).
DROP TABLE IF EXISTS assignments;

-- 5. Re-assert 0002's least privilege for the tables this migration's life
--    cycle touched. assignment's grants are gone; nothing else changes.
REVOKE ALL ON TABLE schema_meta FROM marker_app, marker_worker;
GRANT SELECT ON schema_meta TO marker_app, marker_worker;
