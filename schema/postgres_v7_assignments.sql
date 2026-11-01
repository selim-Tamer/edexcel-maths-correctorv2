-- schema/postgres_v7_assignments.sql
--
-- 0007: server-backed teacher assignments (the assignment_store.py port).
--
-- DRAFT artifact: NOT applied anywhere. Applying it needs IR approval of the
-- exact SQL AND separate explicit user authorization (schema/MIGRATIONS.md).
--
-- Why this exists: the beta's assignment list is currently device-local, so a
-- reload or a second device loses it. The server now owns the row. Identity
-- stays a token decision in api/app.py: the adapter passes tenant_id and
-- owner_subject derived from the VERIFIED subject, never from the body.
--
-- Design rules carried from 0002/0003:
--   * idempotent: every statement can run twice;
--   * least privilege: exactly the verbs the adapter issues - SELECT and
--     INSERT only. There is no rename/delete route in contract v1.2, so no
--     UPDATE and no DELETE. Retention belongs to the separate elevated role;
--   * RLS ENABLE + FORCE with the same tenant predicate, fail closed;
--   * `app.tenant_id` / `app.subject` are policy INPUTS, not authenticity
--     proofs. A compromised database credential can set them; the identity
--     guarantee is the server-side token verification, and RLS is defence in
--     depth, not a second identity authority;
--   * version marker in schema_meta (assignments_version), never an
--     auto-migration;
--   * no secrets, no passwords, no superuser assumption;
--   * no dependency on 0005 (membership) beyond both being additive; this
--     file requires only the reviewed 0001-0004 base.

-- 0. Preflight.
DO $$
BEGIN
    IF to_regclass('public.schema_meta') IS NULL THEN
        RAISE EXCEPTION 'schema v1 missing: apply schema/postgres_v1.sql first';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM schema_meta
                   WHERE key = 'rls_version' AND value = '1') THEN
        RAISE EXCEPTION
            'hardening missing: apply schema/postgres_v2_tenant_rls.sql first';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM schema_meta
                   WHERE key = 'docs_version' AND value = '1') THEN
        RAISE EXCEPTION
            'documents access missing: apply schema/postgres_v4_documents.sql first';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles
                   WHERE rolname = 'marker_app' AND NOT rolcanlogin) THEN
        RAISE EXCEPTION 'marker_app missing or wrong shape: apply 0002 first';
    END IF;
END $$;

-- 1. The assignment row. `assignment_id` is minted by the server; the client
--    supplies only an idempotency key.
CREATE TABLE IF NOT EXISTS assignments (
    tenant_id       text NOT NULL,
    owner_subject   text NOT NULL,
    assignment_id   text NOT NULL,
    idempotency_key text NOT NULL,
    title           text NOT NULL,
    created_at      bigint NOT NULL,
    PRIMARY KEY (tenant_id, owner_subject, assignment_id),
    CONSTRAINT uq_assignments_idempotency
        UNIQUE (tenant_id, owner_subject, idempotency_key),
    CONSTRAINT ck_assignments_title
        CHECK (char_length(title) BETWEEN 1 AND 200),
    CONSTRAINT ck_assignments_id
        CHECK (assignment_id ~ '^asg_[0-9a-f]{24}$')
);

-- list_assignments: newest-first keyset page for one owner+tenant.
CREATE INDEX IF NOT EXISTS ix_assignments_owner_created
    ON assignments (tenant_id, owner_subject, created_at DESC, assignment_id DESC);

-- 2. RLS: same policy shape as the tenant tables in 0002. Fails CLOSED when
--    app.tenant_id is unset. Code still carries explicit tenant/owner
--    predicates, so a privileged connection cannot rely on RLS alone.
ALTER TABLE assignments ENABLE ROW LEVEL SECURITY;
ALTER TABLE assignments FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON assignments;
CREATE POLICY tenant_isolation ON assignments
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''));

-- 3. Least privilege: exactly the verbs the adapter issues.
--    No UPDATE (no rename route), no DELETE (rows are history).
REVOKE ALL ON TABLE assignments FROM marker_app, marker_worker;
GRANT SELECT, INSERT ON assignments TO marker_app;

-- 4. Version marker.
INSERT INTO schema_meta (key, value) VALUES ('assignments_version', '1')
    ON CONFLICT (key) DO NOTHING;
