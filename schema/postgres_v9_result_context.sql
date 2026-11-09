-- schema/postgres_v9_result_context.sql
--
-- 0009: immutable original-result context for BR-02 (PostgreSQL).
--
-- DRAFT artifact: NOT applied anywhere. Applying it needs review of the exact
-- SQL AND separate explicit user authorization (see schema/MIGRATIONS.md).
-- It must only ever be exercised against a verified disposable synthetic
-- database; never against production or a valuable development database.
--
-- Why this exists:
--   0001 keeps only the original DIGEST and a marks-only projection in
--   `results`; the complete original marking output lives in the development
--   ingest registry, which production may not use. BR-02 stores the complete
--   original plus its authoritative context in a separate immutable table,
--   atomically with the existing audit anchor row.
--
-- Design:
--   * `results.owner_subject` is the MEMBERSHIP subject (the existing FK
--     representation). `result_context` records that raw value together with
--     the canonical `google:<subject>` owner, so no reader has to guess one
--     from the other;
--   * the context row carries the complete original output, its digest, the
--     document digest and only genuinely available trusted links (job,
--     assignment, Classroom references). Missing links stay NULL - there is
--     no guessed backfill and no invented student/submission reference;
--   * the composite anchor FK makes a context row without its `results` row
--     impossible, and the composite membership FK keeps ownership in the
--     existing membership table;
--   * runtime roles get SELECT + INSERT only. There is NO UPDATE and NO
--     DELETE on `result_context`; a trigger refuses both at row level as
--     defence in depth, so the original result survives every review;
--   * RLS ENABLE + FORCE with the same `app.tenant_id` predicate as 0002;
--   * No membership-table grant for `marker_worker` (owner direction required
--     for any privilege expansion); worker ownership resolution is resolved
--     outside this schema;
--   * `schema_meta.schema_version` stays '1'; capability is recorded
--     separately as `result_context_version = '1'` (the adapter fails closed
--     without it);
--   * idempotent: every statement can run twice.
--
-- Rollback: schema/postgres_v9_result_context_rollback.sql. It refuses by
-- default when the table holds rows, so populated originals are never dropped
-- silently.

BEGIN;

-- 0. Preflight: the reviewed base, hardening and roles must exist.
DO $$
BEGIN
    IF to_regclass('public.schema_meta') IS NULL
            OR to_regclass('public.results') IS NULL
            OR to_regclass('public.audit_events') IS NULL
            OR to_regclass('public.tenant_membership') IS NULL THEN
        RAISE EXCEPTION 'schema v1 missing: apply schema/postgres_v1.sql first';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM schema_meta
                   WHERE key = 'schema_version' AND value = '1') THEN
        RAISE EXCEPTION 'schema_version is not 1: apply the reviewed base first';
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
    IF NOT EXISTS (SELECT 1 FROM pg_roles
                   WHERE rolname = 'marker_worker' AND NOT rolcanlogin) THEN
        RAISE EXCEPTION 'marker_worker missing or wrong shape: apply 0002 first';
    END IF;
END $$;

-- 1. The immutable original context.
CREATE TABLE IF NOT EXISTS result_context (
    tenant_id                 text NOT NULL,
    membership_subject        text NOT NULL,
    owner_subject             text NOT NULL,
    result_id                 text NOT NULL,
    result_scope              text NOT NULL,
    review_key                text NOT NULL,
    original_output           text NOT NULL,
    original_digest           text NOT NULL,
    document_digest           text NOT NULL DEFAULT '',
    job_id                    text,
    assignment_id             text,
    classroom_course_id       text,
    classroom_assignment_id   text,
    classroom_submission_id   text,
    classroom_revision        text NOT NULL DEFAULT '',
    created_at                timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, membership_subject, result_id),
    CONSTRAINT fk_result_context_anchor
        FOREIGN KEY (tenant_id, membership_subject, result_id)
        REFERENCES results (tenant_id, owner_subject, result_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT fk_result_context_membership
        FOREIGN KEY (tenant_id, membership_subject)
        REFERENCES tenant_membership (tenant_id, subject)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT fk_result_context_job
        FOREIGN KEY (tenant_id, owner_subject, job_id)
        REFERENCES marking_jobs (tenant_id, owner_subject, job_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT fk_result_context_assignment
        FOREIGN KEY (tenant_id, owner_subject, assignment_id)
        REFERENCES assignments (tenant_id, owner_subject, assignment_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CONSTRAINT uq_result_context_owner
        UNIQUE (tenant_id, owner_subject, result_id),
    CONSTRAINT ck_result_context_scope
        CHECK (result_scope IN ('manual', 'classroom')),
    CONSTRAINT ck_result_context_digest
        CHECK (original_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_result_context_document_digest
        CHECK (document_digest = '' OR document_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_result_context_review_key
        CHECK (char_length(review_key) BETWEEN 1 AND 512),
    CONSTRAINT ck_result_context_owner
        CHECK (owner_subject = 'google:' || membership_subject),
    CONSTRAINT ck_result_context_classroom
        CHECK (
            (result_scope = 'manual'
                AND classroom_course_id IS NULL
                AND classroom_assignment_id IS NULL
                AND classroom_submission_id IS NULL
                AND classroom_revision = '')
            OR
            (result_scope = 'classroom'
                AND classroom_course_id IS NOT NULL
                AND classroom_assignment_id IS NOT NULL
                AND classroom_submission_id IS NOT NULL)
        )
);

-- 2. Direct owner reads and trusted-link lookups (never a registry scan).
CREATE INDEX IF NOT EXISTS ix_result_context_owner_created
    ON result_context (tenant_id, membership_subject, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_result_context_trusted_job
    ON result_context (tenant_id, membership_subject, job_id)
    WHERE job_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_result_context_assignment
    ON result_context (tenant_id, membership_subject, assignment_id)
    WHERE assignment_id IS NOT NULL;

-- 3. Row-level security: fail closed without a transaction-local tenant.
ALTER TABLE result_context ENABLE ROW LEVEL SECURITY;
ALTER TABLE result_context FORCE  ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON result_context;
CREATE POLICY tenant_isolation ON result_context
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''));

-- 4. Immutability: no runtime trigger bypass, even if a grant regresses.
CREATE OR REPLACE FUNCTION result_context_immutable()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'result_context rows are immutable';
END $$;

DROP TRIGGER IF EXISTS result_context_immutable ON result_context;
CREATE TRIGGER result_context_immutable
    BEFORE UPDATE OR DELETE ON result_context
    FOR EACH ROW EXECUTE FUNCTION result_context_immutable();

-- 5. Least privilege: exactly the verbs the adapter issues.
--    SELECT (projection/visibility/snapshot), INSERT (atomic registration).
--    NO UPDATE, NO DELETE: originals are immutable and history is never
--    trimmed. Retention remains a separate elevated-role decision.
REVOKE ALL ON TABLE result_context FROM marker_app, marker_worker;
GRANT SELECT, INSERT ON result_context TO marker_app, marker_worker;

-- 6. Capability marker, distinct from the fixed schema_version = '1'.
INSERT INTO schema_meta (key, value)
VALUES ('result_context_version', '1')
ON CONFLICT (key) DO NOTHING;

COMMIT;
