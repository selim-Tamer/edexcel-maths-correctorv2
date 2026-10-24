-- M5 persistence slice: PostgreSQL schema v1 (IMPL artifact; NOT provisioned).
--
-- Applied EXPLICITLY by operators/CI (tests/schema apply step); nothing in
-- the application runs DDL automatically and there is NO auto-migration:
-- postgres_store.open_store fails closed on a missing/unsupported
-- schema_meta version. Postgres integration is tested ONLY via the CI
-- service container; no instance is provisioned, deployed or paid for.
-- Local-dev fallback stays the development-only SQLite audit store
-- (audit_store.py) and is labelled as such everywhere it appears.
--
-- Design conditions (IR security design review):
--   * owner + tenant are DATA, never client claims: every owner-scoped row
--     carries tenant_id and both FK back to an existing membership row;
--   * originals are immutable (enforced in code + digest columns here);
--   * optimistic revision and stream-scoped idempotency are UNIQUE-enforced
--     in storage, same contract as the SQLite store;
--   * PDFs are REFERENCES only (server-derived digest + private object
--     key): raw document bytes and student answer text are never stored.

BEGIN;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   text PRIMARY KEY,
    value text NOT NULL
);

INSERT INTO schema_meta (key, value) VALUES ('schema_version', '1')
    ON CONFLICT (key) DO NOTHING;

CREATE TABLE IF NOT EXISTS tenants (
    tenant_id  text PRIMARY KEY,
    status     text NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Column order (tenant_id, subject) matches every FK that points here.
CREATE TABLE IF NOT EXISTS tenant_membership (
    tenant_id  text NOT NULL REFERENCES tenants(tenant_id)
               ON UPDATE CASCADE ON DELETE RESTRICT,
    subject    text NOT NULL,
    role       text NOT NULL DEFAULT 'teacher',
    active     boolean NOT NULL DEFAULT TRUE,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, subject)
);
CREATE INDEX IF NOT EXISTS tenant_membership_by_subject
    ON tenant_membership (subject);

CREATE TABLE IF NOT EXISTS results (
    tenant_id              text NOT NULL,
    owner_subject          text NOT NULL,
    result_id              text NOT NULL,
    result_revision        integer NOT NULL DEFAULT 0,
    original_digest        text NOT NULL,
    original_scores        text NOT NULL,   -- marks-only projection (JSON)
    original_scores_digest text NOT NULL,
    created_at             timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, owner_subject, result_id),
    CONSTRAINT fk_results_membership
        FOREIGN KEY (tenant_id, owner_subject)
        REFERENCES tenant_membership (tenant_id, subject)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CHECK (result_revision >= 0)
);

CREATE TABLE IF NOT EXISTS audit_events (
    tenant_id            text NOT NULL,
    owner_subject        text NOT NULL,
    result_id            text NOT NULL,
    sequence             integer NOT NULL,
    event_id             text NOT NULL,
    idempotency_key      text NOT NULL,
    payload_digest       text NOT NULL,
    event_type           text NOT NULL,
    question_number      text NOT NULL,
    action               text NOT NULL,
    review_status        text NOT NULL,
    old_score            text NOT NULL DEFAULT '',
    new_score            text NOT NULL DEFAULT '',
    reason               text NOT NULL DEFAULT '',
    expected_revision    integer NOT NULL,
    resulting_revision   integer NOT NULL,
    supersedes_event_id  text,
    actor                text NOT NULL,
    binding              text NOT NULL DEFAULT '',
    authorization_json   text NOT NULL DEFAULT '{}',
    created_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, event_id),
    CONSTRAINT uq_audit_events_stream_sequence
        UNIQUE (tenant_id, owner_subject, result_id, sequence),
    CONSTRAINT uq_audit_events_stream_idempotency
        UNIQUE (tenant_id, owner_subject, result_id, idempotency_key),
    CONSTRAINT uq_audit_events_stream_event
        UNIQUE (tenant_id, owner_subject, result_id, event_id),
    FOREIGN KEY (tenant_id, owner_subject, result_id)
        REFERENCES results (tenant_id, owner_subject, result_id)
        ON UPDATE CASCADE ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS audit_events_stream_question
    ON audit_events (tenant_id, owner_subject, result_id,
                     question_number, sequence);

-- Private document REFERENCES only: server-derived sha256 + object key.
-- No bytes, no filenames, no pupil names, no public URLs.
CREATE TABLE IF NOT EXISTS pdf_references (
    tenant_id        text NOT NULL,
    owner_subject    text NOT NULL,
    document_digest  text NOT NULL,
    storage_key      text NOT NULL,
    size_bytes       bigint,
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, owner_subject, document_digest),
    CONSTRAINT fk_pdf_membership
        FOREIGN KEY (tenant_id, owner_subject)
        REFERENCES tenant_membership (tenant_id, subject)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    CHECK (document_digest ~ '^[0-9a-f]{64}$')
);

COMMIT;
