-- schema/postgres_v8_job_assignment_linkage.sql
--
-- 0008: server-validated linkage from a marking job to a teacher assignment.
--
-- DRAFT artifact: NOT applied anywhere. Applying it needs review of the exact
-- SQL AND separate explicit user authorization (see schema/MIGRATIONS.md).
--
-- Why this exists:
--   The assignments slice (0007) and the jobs slice (0003) are independent.
--   An end-to-end assignment needs its marking job to reference the assignment
--   it belongs to, so the client can follow one assignment through marking.
--   The client sends an `assignment_id`; the API verifies that the assignment
--   belongs to the SAME verified tenant+owner before this row is written. The
--   database then enforces the reference too.
--
-- Design:
--   * nullable column: existing/unknown jobs stay valid and unlinked;
--   * composite FK (tenant_id, owner_subject, assignment_id) -> assignments:
--     cross-owner/cross-tenant linkage is impossible even with a code bug;
--   * no UPDATE/DELETE grant change: the column is written at INSERT time;
--   * index for the eventual "jobs for this assignment" read (no route yet);
--   * idempotent; rollback drops only the linkage, never job rows.

-- 0. Preflight: jobs (0003) and assignments (0007) must exist and be marked.
DO $$
BEGIN
    IF to_regclass('public.marking_jobs') IS NULL THEN
        RAISE EXCEPTION 'jobs missing: apply schema/postgres_v3_jobs.sql first';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM schema_meta
                   WHERE key = 'jobs_version' AND value = '1') THEN
        RAISE EXCEPTION 'jobs not marked: apply schema/postgres_v3_jobs.sql first';
    END IF;
    IF to_regclass('public.assignments') IS NULL THEN
        RAISE EXCEPTION
            'assignments missing: apply schema/postgres_v7_assignments.sql first';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM schema_meta
                   WHERE key = 'assignments_version' AND value = '1') THEN
        RAISE EXCEPTION
            'assignments not marked: apply schema/postgres_v7_assignments.sql first';
    END IF;
END $$;

-- 1. The nullable linkage column.
ALTER TABLE marking_jobs ADD COLUMN IF NOT EXISTS assignment_id text;

-- 2. Composite reference: only an assignment owned by the same tenant+owner
--    can be linked. Guarded so a re-run is a no-op.
DO $$
BEGIN
    IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'fk_marking_jobs_assignment'
              AND conrelid = 'public.marking_jobs'::regclass) THEN
        ALTER TABLE marking_jobs
            ADD CONSTRAINT fk_marking_jobs_assignment
            FOREIGN KEY (tenant_id, owner_subject, assignment_id)
            REFERENCES assignments (tenant_id, owner_subject, assignment_id)
            ON UPDATE CASCADE ON DELETE RESTRICT;
    END IF;
END $$;

-- 3. Read path for "jobs for one assignment" (additive later; no route yet).
CREATE INDEX IF NOT EXISTS ix_marking_jobs_assignment
    ON marking_jobs (tenant_id, owner_subject, assignment_id)
    WHERE assignment_id IS NOT NULL;

-- 4. Version marker.
INSERT INTO schema_meta (key, value)
VALUES ('job_assignment_linkage_version', '1')
ON CONFLICT (key) DO NOTHING;
