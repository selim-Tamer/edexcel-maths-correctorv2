-- schema/postgres_v8_job_assignment_linkage_rollback.sql
--
-- Exact inverse of schema/postgres_v8_job_assignment_linkage.sql (0008).
--
-- It removes the linkage column, its constraint, its index and the marker.
-- It never deletes job rows; previously linked jobs simply lose the link.
-- Refuses when 0008 was never applied.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM schema_meta
                   WHERE key = 'job_assignment_linkage_version' AND value = '1') THEN
        RAISE EXCEPTION
            'job assignment linkage is absent: 0008 was not applied here';
    END IF;
END $$;

DROP INDEX IF EXISTS ix_marking_jobs_assignment;

ALTER TABLE marking_jobs
    DROP CONSTRAINT IF EXISTS fk_marking_jobs_assignment;

ALTER TABLE marking_jobs DROP COLUMN IF EXISTS assignment_id;

DELETE FROM schema_meta WHERE key = 'job_assignment_linkage_version';
