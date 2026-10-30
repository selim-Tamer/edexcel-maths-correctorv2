-- schema/postgres_v6_result_narrowing.sql
--
-- 0006: narrow the runtime UPDATE privilege on `results` to the ONE column the
-- shipped code actually writes (`result_revision`).
--
-- DRAFT artifact: NOT applied anywhere. Applying it needs review of the exact
-- SQL AND separate explicit user authorization (see schema/MIGRATIONS.md).
--
-- Why this exists:
--   0002 granted `UPDATE` on the whole `results` row to `marker_app` and
--   `marker_worker`. The only shipped PostgreSQL statement that updates
--   `results` is `postgres_store.commit()`:
--       UPDATE results SET result_revision = %s WHERE ...
--   A table-wide UPDATE also permits rewriting the immutable original result
--   (digest, marks-only projection, owner/tenant keys), which no product code
--   path needs. PostgreSQL column privileges express the real need exactly.
--
-- Effect:
--   * `marker_app` / `marker_worker` lose table-wide `UPDATE` on `results`;
--   * they keep `SELECT`, `INSERT` and `UPDATE (result_revision)`;
--   * `UPDATE results SET original_scores = ...` (or any other column) is
--     refused by the server with InsufficientPrivilege;
--   * no DELETE is granted (unchanged: none anywhere).
--
-- Idempotent: every statement can run twice. The rollback restores the 0002
-- table-wide grant; it never touches rows.

-- 0. Preflight: the base table, the roles and their 0002 shape must exist.
DO $$
BEGIN
    IF to_regclass('public.results') IS NULL THEN
        RAISE EXCEPTION 'schema v1 missing: apply schema/postgres_v1.sql first';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles
                   WHERE rolname = 'marker_app' AND NOT rolcanlogin) THEN
        RAISE EXCEPTION 'marker_app missing or wrong shape: apply 0002 first';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles
                   WHERE rolname = 'marker_worker' AND NOT rolcanlogin) THEN
        RAISE EXCEPTION 'marker_worker missing or wrong shape: apply 0002 first';
    END IF;
END $$;

-- 1. Remove the table-wide verb, then grant only the column the code writes.
--    REVOKE of a table-level privilege does not remove column-level grants,
--    so this pair is stable when re-run.
REVOKE UPDATE ON results FROM marker_app, marker_worker;
GRANT UPDATE (result_revision) ON results TO marker_app, marker_worker;

-- 2. Version marker.
INSERT INTO schema_meta (key, value)
VALUES ('results_update_version', '1')
ON CONFLICT (key) DO NOTHING;
