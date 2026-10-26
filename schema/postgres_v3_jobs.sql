-- schema/postgres_v3_jobs.sql
--
-- 0003: durable marking_jobs in PostgreSQL (the port of job_store.py).
--
-- The development job store is SQLite; the production composition needs the
-- same lease/attempt semantics on the managed database so jobs survive
-- process restarts and Cloud Run scale-down. This file is a DRAFT artifact:
-- it is NOT applied anywhere. Applying it needs IR approval of the exact SQL
-- AND separate explicit user authorization (see schema/MIGRATIONS.md).
--
-- Design rules carried from 0002:
--   * idempotent: every statement can run twice;
--   * least privilege: exactly the verbs the adapter issues, no DELETE;
--   * RLS ENABLE + FORCE with the same tenant predicate, fail closed;
--   * version marker in schema_meta (jobs_version), never an auto-migration;
--   * no secrets, no passwords, no superuser assumption.

-- 0. Preflight: 0001 and 0002 must already be applied.
DO $$
BEGIN
    IF to_regclass('public.schema_meta') IS NULL THEN
        RAISE EXCEPTION 'schema v1 missing: apply schema/postgres_v1.sql first';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM schema_meta
                   WHERE key = 'schema_version' AND value = '1') THEN
        RAISE EXCEPTION
            'schema_version is not 1: this migration is for the reviewed base';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM schema_meta
                   WHERE key = 'rls_version' AND value = '1') THEN
        RAISE EXCEPTION
            'hardening missing: apply schema/postgres_v2_tenant_rls.sql first';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles
                   WHERE rolname = 'marker_app' AND NOT rolcanlogin) THEN
        RAISE EXCEPTION
            'marker_app missing or wrong shape: apply 0002 first';
    END IF;
END $$;

-- 1. The job table: column-for-column the shape job_store.py already uses,
--    with the epoch seconds it already stores (BIGINT), so the Python port is
--    a translation, not a redesign.
CREATE TABLE IF NOT EXISTS marking_jobs (
    tenant_id       text NOT NULL,
    owner_subject   text NOT NULL,
    job_id          text NOT NULL,
    idempotency_key text NOT NULL,
    document_digest text NOT NULL,
    status          text NOT NULL,
    attempts        integer NOT NULL DEFAULT 0,
    max_attempts    integer NOT NULL,
    lease_until     bigint NOT NULL DEFAULT 0,
    result_id       text,
    error_code      text,
    created_at      bigint NOT NULL,
    updated_at      bigint NOT NULL,
    PRIMARY KEY (tenant_id, owner_subject, job_id),
    CONSTRAINT uq_marking_jobs_idempotency
        UNIQUE (tenant_id, owner_subject, idempotency_key),
    CONSTRAINT ck_marking_jobs_status
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    CONSTRAINT ck_marking_jobs_attempts
        CHECK (attempts >= 0 AND attempts <= max_attempts)
);

-- find_authorizable_job: newest SUCCEEDED job for this owner+tenant+document.
CREATE INDEX IF NOT EXISTS ix_marking_jobs_authorizable
    ON marking_jobs (tenant_id, owner_subject, document_digest, status,
                     created_at DESC);

-- 2. RLS: same policy shape as the five tenant tables in 0002. The predicate
--    binds the transaction-local app.tenant_id and fails CLOSED when it is
--    unset. FORCE binds the table OWNER too; superuser/BYPASSRLS are NOT
--    constrained and are authorised by explicit tenant predicates in code.
ALTER TABLE marking_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE marking_jobs FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON marking_jobs;
CREATE POLICY tenant_isolation ON marking_jobs
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''));

-- 3. Least privilege: exactly the verbs the PostgreSQL adapter issues.
--    SELECT (read job state), INSERT (create), UPDATE (claim/lease/settle).
--    NO DELETE for either runtime role - job rows are history, and the
--    retention/purge path belongs to the separate elevated role (0002 4.1).
REVOKE ALL ON TABLE marking_jobs FROM marker_app, marker_worker;
GRANT SELECT, INSERT, UPDATE ON marking_jobs TO marker_app, marker_worker;

-- 4. Version marker: this migration is additive, the adapter contract for
--    results/audit_events is untouched, and a second apply is a no-op.
INSERT INTO schema_meta (key, value) VALUES ('jobs_version', '1')
    ON CONFLICT (key) DO NOTHING;
