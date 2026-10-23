# C6-B3 M2 — audit storage contract (development-only, opt-in)

Status: proposed with implementation for independent review. This document is
the reviewed storage contract for milestone M2 of C6-B3. It does **not** pick a
production store, does **not** provision anything, and does **not** claim
school-ready durability.

## 1. Decision and rationale

| Option | Verdict | Why |
| --- | --- | --- |
| Naive append-only JSONL | **Rejected** | Cannot meet the milestone: no atomic multi-record write (a crash between two appends leaves a correction with no audit event), no storage-enforced uniqueness (stream sequence / idempotency key), no rollback of a half-written record, and two writers can interleave lines with no ordering guarantee. |
| **SQLite (stdlib `sqlite3`), development-only adapter** | **Chosen for M2** | Real ACID transactions (`BEGIN IMMEDIATE`), uniqueness enforced by `UNIQUE` constraints inside the storage engine, writer serialisation across processes/threads, integrity checking (`PRAGMA integrity_check`), snapshot/restore via the SQLite backup API — all with zero new dependencies and no network. |
| PostgreSQL (or another server database) | **Deferred — production path, not authorized here** | The right durability/tenancy/backups answer for a hosted service, but choosing and provisioning it is explicitly out of scope for this milestone. Migration path in §7. |

The adapter is wrapped behind a small, reviewable API (`audit_store.py`) so the
storage engine can be replaced later without touching the app's gate logic.

**This is development-only durability**: one local file, one host, no tenant
model, no retention policy, no replication, no backups except explicit
snapshots. The mode label is stored inside the file itself
(`store_meta.mode = "development-only"`, `AuditStore.mode_label` contains
"NOT school-ready") and asserted by tests.

## 2. Opt-in and reversibility

Persistence is **off by default**; with no environment variable set, behaviour
is exactly the C6-B3 M1 session-only behaviour and no file is ever opened.

| Variable | Meaning |
| --- | --- |
| `AI_MARKER_AUDIT_DEV_STORE` | Path of the local store file. Absent/empty → persistence disabled (session-only). |
| `AI_MARKER_AUDIT_DEV_ONLY` | Must be exactly `1` to acknowledge development-only persistence. Path without the acknowledgement → the store refuses to open and every decision fails closed (`audit_store_unavailable`). |

Removing both variables reverts to session-only behaviour with no code change
and no store access (tested). Opening the store itself also requires the same
explicit acknowledgement (`audit_store.open_store(..., dev_only_ack=True)`),
so persistence can never happen by accident from another call site.

Failure codes surfaced by the decision path (all refuse the mutation and keep
nothing): `audit_store_unavailable`, `audit_result_identity_unavailable`,
`audit_persist_failed`, plus the store's own codes (`stale_revision`,
`original_output_immutable`, `result_not_registered`,
`idempotency_key_payload_mismatch`, `schema_version_unsupported`, …).

## 3. Schema (SQLite, `schema_version = 1`)

```sql
store_meta(key PRIMARY KEY, value)            -- schema_version, mode
results(
  owner, result_id,                           -- composite PK: every read/write is owner-scoped
  result_revision INTEGER,                    -- optimistic concurrency counter
  original_digest,                            -- SHA-256 of the canonical ORIGINAL AI output
  original_scores,                            -- marks-only projection [[question, mark], ...]
  original_scores_digest,                     -- tamper check on that projection
  created_at)
audit_events(
  owner, result_id, sequence,                 -- UNIQUE (owner, result_id, sequence)      → ordered stream
  event_id PRIMARY KEY,
  idempotency_key, payload_digest,            -- UNIQUE (owner, result_id, idempotency_key) → scoped retry safety
  event_type, question_number, action, review_status,
  old_score, new_score, reason,
  expected_revision, resulting_revision,      -- revision chain
  supersedes_event_id,                        -- which earlier decision this one supersedes
  actor, binding, authorization_json, created_at)
```

Exactly three tables. **No** grade, approval or publication table exists in
this store: final approval and grade publication remain separate, separately
gated product concerns.

What is stored, deliberately:

- owner reference (an opaque subject string — never a token),
- the SHA-256 digest of the original AI output and a **marks-only** projection,
- per-decision fields: scores, reason, binding, actor, revision chain,
- audit-only authorization evidence, re-whitelisted at the store boundary to
  the same five short fields (`scope`, `actor`, `authorization_revision`,
  `row_revision`, `verified_at`).

What is **never** stored: raw student answers, working summaries, notes,
transcripts, access tokens, credentials, approval/publication state.
(Proved by test: sentinel answer text is absent from every byte of the store.)

## 4. Semantics

1. **Atomic correction + audit commit.** `commit_correction` writes the audit
   event and advances `results.result_revision` inside one `BEGIN IMMEDIATE`
   transaction. A fault injected at `begin`, `after_event_insert` or
   `before_commit` rolls back both rows. A correction never exists without its
   audit event, and no decision is recorded in session state if the commit
   failed (the session override is rolled back too).
2. **Immutable original AI output.** `register_result` stores the digest once.
   Re-registering identical output is idempotent; different output for the
   same owner+result is refused (`original_output_immutable`) instead of
   silently relabelling what the decisions were reviewed against.
3. **Owner isolation.** Every query is filtered by `owner` as part of the
   primary key. Another owner gets `result_not_registered` for reads, writes,
   verify and export — no data, no existence oracle beyond "not mine".
4. **Optimistic concurrency.** Each commit names the revision it was based on.
   A mismatch fails with `stale_revision` and writes nothing. Concurrent
   writers therefore cannot lose updates: tested with two writers and with a
   3-thread / 18-commit stress run producing a dense, gap-free stream.
5. **Scoped idempotency.** Key = SHA-256(owner, result_id, semantic payload).
   Same key + same payload → returns the prior result (`status: duplicate`,
   same sequence/event id, no second event). Same key + different payload →
   `idempotency_key_payload_mismatch`. Uniqueness is enforced by SQLite, not by
   application code (raw SQL duplicates are rejected by the engine and tested).
6. **Ordered events.** Sequence is assigned inside the transaction and
   `UNIQUE (owner, result_id, sequence)` forbids collisions. `replay()` re-reads
   the stream and fails closed on gaps, revision-chain breaks, duplicate
   event ids/keys, dangling `supersedes` references, widened evidence fields
   or a revision row that no committed event explains.
7. **Restart / recovery / restore.** Close + reopen preserves history,
   originals and revisions (tested). `snapshot_to()` takes a consistent online
   snapshot; `restore_from()` verifies integrity and schema version *before*
   atomically replacing the target and clears stale WAL sidecars first.
   Corrupted, foreign or wrong-version snapshots are refused and the live store
   is left intact.
8. **No silent erasure.** There is **no** delete/trim/purge/clear API on the
   store (the complete public surface is asserted by test). `export_stream()`
   exists for authorized data-subject export; erasure policy is an open
   decision (§6), not something this milestone invents.
9. **Fail-closed verification.** `verify()` re-runs SQLite's integrity check,
   recomputes the marks-projection digest and revalidates the event chain; any
   deviation yields `ok: False` plus a problem list.

## 5. App integration (gate order preserved)

`apply_question_review_decision` runs, in this order:
authentication → row ownership → fresh scope authorization (live Classroom
re-verification / manual document provenance) → action validation → question
identity → binding → decision evaluation → **durable plan** (store resolved,
stream revision read) → override write → **durable commit** → session record +
session audit event.

So: no store interaction at all happens before the fresh gates pass (a revoked
or ambiguous decision never even registers the result), and the durable commit
happens *before* the decision is recorded, so an accepted decision always has
its audit event. `record["audit_reference"] = {result_id, sequence, event_id}`
ties the session record to the durable stream entry when persistence is on.

The manual PDF byte/owner binding (`authorization_revision` = SHA-256 of the
owned upload) and the classroom binding (revision freshly computed from live
listings) are stored verbatim in the event's evidence; nothing reads them back
as authorization.

## 6. Open decisions (identified, not invented)

These cannot be answered safely from the current source, so this milestone
does **not** invent policy; each needs one explicit decision:

1. **Tenant boundary.** Storage is isolated by the existing per-teacher
   `owner` (server-derived subject). The plan's `tenant` concept (school /
   class boundary, `tenant_id` column, cross-tenant denial) depends on C11's
   tenant model. *Decision needed:* is the storage scope per-teacher owner, per
   school/tenant, or owner nested inside tenant? The schema carries `owner` in
   both primary keys today; adding `tenant_id` later is a contained migration
   (§7) but must not be guessed here.
2. **Retention and deletion.** No retention window, expiry or erasure
   behaviour is defined (no such API exists at all). *Decision needed:* the
   retention period, and how data-subject erasure is reconciled with audit
   accountability (tombstone vs. hard delete vs. pseudonymisation), including
   the school/GDPR requirement that decides it.
3. **Durable identity for manual uploads.** Manual rows are keyed
   `manual:{document_digest[:16]}:{review_key}`; `review_key` is random per run,
   so a manual row's durable stream is session-scoped. *Decision needed:* what
   identifies a result durably for a manual upload (uploaded document + pupil
   reference, a submission id, …) without putting a pupil name in the key.
4. **Identity across re-marks.** A classroom identity is
   `classroom:{course}:{assignment}:{submission}:{revision}` — a resubmission
   (new provider revision) starts a new stream, but re-marking the *same*
   revision with different AI output is refused (`original_output_immutable`,
   fail closed rather than silently replace the original). *Decision needed:*
   whether a re-mark is a new result generation, and how that is signalled.

## 7. Migration path to a production store (Postgres)

Not implemented, not authorized; documented so the contract survives the swap:

- `results` and `audit_events` map 1:1 to tables with a composite primary key
  `(owner, result_id)` and `(owner, result_id, sequence)` plus a partial unique
  index for `(owner, result_id, idempotency_key)`; `TEXT` → `TEXT`, `INTEGER` →
  `BIGINT`, timestamps stay ISO/unix-second text or move to `TIMESTAMPTZ`.
- `BEGIN IMMEDIATE` → `SELECT ... FOR UPDATE` (or a serializable transaction);
  the SQLite unique-constraint failure codes map one-to-one to `23505`
  violations and are already surfaced as `stream_sequence_conflict` /
  `idempotency_key_payload_mismatch`.
- WAL/synchronous pragmas are dropped; durable commit semantics and backups
  become the database's responsibility.
- `schema_version` becomes a migration table (e.g. `schema_migrations`);
  this adapter fails closed on an unknown version rather than migrating.
- The API surface (`register_result`, `commit_correction`, `events`, `replay`,
  `verify`, `snapshot_to`/`restore_from`, `export_stream`) stays as-is; only
  `audit_store.py` changes. The app layer, gate order and failure codes are
  untouched.
- Open decisions §6.1–§6.4 must be settled *before* that migration, because a
  tenant column and retention behaviour are exactly what a production store
  would have to enforce from day one.

### 7.1 Adapter boundary toward Supabase + private object storage

Selected direction (documented only; **nothing provisioned, no region chosen,
no real data migrated, no domain purchased** — each needs its own
authorization): client data in **Supabase Postgres**, domain/DNS at
**Cloudflare**, API integration via **FastAPI**. The boundary the M2 store
already exposes is what makes that swap contained:

- **Tables → Supabase Postgres**: `results` and `audit_events` become Postgres
  tables with the same composite keys; `supabase` client/`psycopg` replaces the
  stdlib `sqlite3` connection inside `audit_store.py` only. Row-Level Security
  can enforce the `owner` scoping the adapter enforces in code today, but the
  adapter keeps its explicit owner predicates so behaviour is identical with or
  without RLS.
- **Marks-only projection and snapshots → Cloudflare private storage (or
  Supabase Storage)**: `snapshot_to`/`restore_from` and `export_stream` are the
  only byte-blob touch points. Pointing them at private object storage (signed,
  non-public URLs, owner-scoped) leaves the transactional path in Postgres;
  the immutable original reference stays a digest in the `results` row either
  way.
- **FastAPI integration**: the app layer's gate order, fresh-authorization
  checks and failure codes do not change — FastAPI would sit in front of the
  same `commit_audit_decision`/`audit_persistence_plan` calls, so the dev-only
  opt-in and fail-closed posture survive the integration untouched.
- **Not a UI replay engine**: even after the swap, the session decision records
  remain the UI source of truth; the store stays an audit trail, per §9.

## 8. Acceptance mapping (plan row for M2)

| Acceptance item | Where it is proved |
| --- | --- |
| Restart preserves history | `RestartRecoveryTests`, `OptInCommitTests.test_restart_recovers_history_and_continues_the_stream` |
| Originals remain unchanged | `ImmutableOriginalTests`, `OptInCommitTests.test_a_changed_original_output_refuses_later_decisions` |
| Replay is idempotent | `IdempotencyTests` (same key/payload → prior result; app-level repeat decision appends nothing) |
| Stale writes fail | `ConcurrencyTests` (store) + `FailClosedTests.test_a_stale_revision_is_refused_without_partial_state` (app) |
| Cross-tenant access fails | `OwnerIsolationTests` (owner-scoped stand-in until §6.1 is decided) |
| Restore/replay tested | `SnapshotRestoreTests`, `OrderingAndReplayTests` |
| Atomic event/correction writes | `AtomicCommitTests` (fault injection at every stage) |
| Failure injection | `AtomicCommitTests`, `FailClosedTests.test_a_failed_audit_write_keeps_nothing` |

## 9. Limitations (stated, not claimed away)

- Single host, single file, no replication, no automated backup: development
  durability only.
- No tenant model, no retention/deletion policy (§6) — deliberately absent.
- Idempotency covers retries of an identical decision; it does not provide
  exactly-once delivery across processes that crash mid-response beyond
  re-running the same operation.
- The revision chain and digests detect tampering and drift; events are not
  hash-chained or signed, so a writer with direct file access could rewrite
  history undetectably (an external chain/signature is a production concern).
- Manual-upload streams are session-scoped (§6.3), so "history survives
  restart" holds fully for Classroom rows and only within a session for manual
  rows until that decision is made.
