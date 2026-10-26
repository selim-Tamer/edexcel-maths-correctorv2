-- schema/postgres_v4_documents.sql
--
-- 0004: private document metadata access + the SEPARATE elevated purge role.
--
-- DRAFT artifact: NOT applied anywhere. Applying it needs IR approval of the
-- exact SQL AND separate explicit user authorization (see schema/MIGRATIONS.md).
--
-- What 0002 deliberately left out: `pdf_references` had no grants, because no
-- shipped PostgreSQL statement touched it. The private storage adapter now
-- writes and reads those rows, so it gets exactly the verbs the adapter
-- issues - and the retention/deletion path gets its OWN role instead of
-- widening the runtime roles (0002 4.1 explicitly rejected that).
--
-- Rules carried from 0002/0003:
--   * idempotent, preflight-gated on the earlier migrations;
--   * least privilege: exactly the verbs the code issues, zero DELETE for the
--     runtime roles, audit trail stays append-only;
--   * RLS is unchanged: `pdf_references` already carries `tenant_isolation`
--     from 0002, and EVERY role here binds `app.tenant_id` transaction
--     locally. `marker_purge` is NOT a bypass role: a purge runs per tenant
--     with the binding set, never as a privileged cross-tenant delete.

-- 0. Preflight.
DO $$
BEGIN
    IF to_regclass('public.pdf_references') IS NULL THEN
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
    IF NOT EXISTS (SELECT 1 FROM schema_meta
                   WHERE key = 'jobs_version' AND value = '1') THEN
        RAISE EXCEPTION
            'job schema missing: apply schema/postgres_v3_jobs.sql first';
    END IF;
END $$;

-- 1. Runtime access to the document metadata rows. The adapter issues
--    SELECT (owner/tenant-scoped lookup) and INSERT (record the digest+key
--    after a successful upload). No UPDATE: the row is immutable metadata.
--    NO DELETE: retention is not a runtime verb.
REVOKE ALL ON TABLE pdf_references FROM marker_app, marker_worker;
GRANT SELECT, INSERT ON pdf_references TO marker_app, marker_worker;

-- 2. The SEPARATE elevated role for retention/deletion (0002 4.1).
--    NOLOGIN group role; the operator creates the login and keeps its
--    password in Secret Manager. It holds SELECT (find what to purge) and
--    UPDATE (the tombstone: clear the object key once the bytes are gone) on
--    pdf_references ONLY - it cannot read results, cannot write the audit
--    trail and cannot touch jobs. The object itself is deleted through the
--    storage service key, which this role never sees.
--
--    Note what it does NOT get: DELETE. The reviewed retention proposal keeps
--    the digest row as a tombstone for audit ("deletion removes the object,
--    keeps the digest/tombstone row"), so NO role in this schema holds any
--    DELETE at all - the audit trail cannot be trimmed by a purge.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'marker_purge') THEN
        CREATE ROLE marker_purge NOLOGIN;
    ELSE
        IF EXISTS (SELECT 1 FROM pg_roles
                   WHERE rolname = 'marker_purge'
                     AND (rolcanlogin OR rolsuper OR rolbypassrls
                          OR rolcreatedb OR rolcreaterole)) THEN
            RAISE EXCEPTION
                'marker_purge exists with escalation attributes; fix the role'
                ' by hand and record why';
        END IF;
    END IF;
END $$;

REVOKE ALL ON TABLE schema_meta, tenants, tenant_membership, results,
    audit_events, pdf_references, marking_jobs FROM marker_purge;
GRANT SELECT, UPDATE ON pdf_references TO marker_purge;

-- 3. Version marker.
INSERT INTO schema_meta (key, value) VALUES ('docs_version', '1')
    ON CONFLICT (key) DO NOTHING;
