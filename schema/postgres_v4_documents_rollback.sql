-- schema/postgres_v4_documents_rollback.sql
--
-- Exact inverse of schema/postgres_v4_documents.sql (0004). Reviewable
-- artifact: NOT applied anywhere without IR approval + explicit user
-- authorization.
--
-- Disclosures, because this backout is NOT symmetric in data:
--   * it does NOT delete document objects from the Supabase bucket. Object
--     deletion is a storage-side action with its own service credential and
--     its own retention decision;
--   * it does NOT delete `pdf_references` rows. They were created by the
--     adapter under 0001's table; rolling back 0004 only removes the ACCESS.
--     A project holding real metadata should take the schema/MIGRATIONS.md 2
--     dump first and run the 2.1 drill.
--   * it restores 0002's posture exactly: `pdf_references` carries no
--     runtime grant at all, and `marker_purge` is gone.

-- 0. Preconditions.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM schema_meta
                   WHERE key = 'docs_version' AND value = '1') THEN
        RAISE EXCEPTION 'docs_version marker absent: 0004 was not applied here';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'marker_purge') THEN
        RAISE EXCEPTION 'marker_purge is absent: nothing to roll back';
    END IF;
END $$;

-- 1. The elevated role goes first: revoke, then drop only with no dependents
--    (the operator login holds membership; revoke that first).
REVOKE ALL ON TABLE pdf_references FROM marker_purge;
DROP ROLE marker_purge;

-- 2. Runtime roles return to the 0002 posture: no grant on pdf_references.
REVOKE ALL ON TABLE pdf_references FROM marker_app, marker_worker;

-- 3. Re-assert 0002's least privilege on the rest, so nothing this
--    migration's life cycle touched can widen the audit or results surface.
REVOKE ALL ON TABLE schema_meta, tenants, tenant_membership, results,
    audit_events, marking_jobs FROM marker_app, marker_worker;
GRANT SELECT ON schema_meta TO marker_app, marker_worker;
GRANT SELECT, INSERT, UPDATE ON results TO marker_app, marker_worker;
GRANT SELECT, INSERT ON audit_events TO marker_app, marker_worker;
GRANT SELECT, INSERT, UPDATE ON marking_jobs TO marker_app, marker_worker;

-- 4. Version marker.
DELETE FROM schema_meta WHERE key = 'docs_version';
