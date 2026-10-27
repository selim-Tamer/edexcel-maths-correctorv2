-- schema/postgres_v6_result_narrowing_rollback.sql
--
-- Exact inverse of schema/postgres_v6_result_narrowing.sql (0006).
--
-- It restores the 0002 shape: table-wide `UPDATE` on `results` for both
-- runtime roles. It touches no rows and no other migration's objects.
-- Refuses when 0006 is not marked as applied, so a rollback of something that
-- never happened cannot silently widen the grant.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM schema_meta
                   WHERE key = 'results_update_version' AND value = '1') THEN
        RAISE EXCEPTION 'results narrowing is absent: 0006 was not applied here';
    END IF;
END $$;

REVOKE UPDATE (result_revision) ON results FROM marker_app, marker_worker;
GRANT UPDATE ON results TO marker_app, marker_worker;

DELETE FROM schema_meta WHERE key = 'results_update_version';
