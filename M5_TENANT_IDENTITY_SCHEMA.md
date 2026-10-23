# M5 — Tenant model, identity & Postgres migration design

**Status: SPEC / PROPOSAL — nothing implemented, nothing provisioned.**
Milestone M5 (C11 foundation + C10 contracts). Companion to `m5_api_contract.json`
and `M5_FASTAPI_EXTRACTION.md`. Baseline: main `3cfef2a`.

This document proposes the tenant model, the durable identity scheme for
manual uploads and re-marks, and the Postgres migration path from the C6-B3
SQLite store. **No table is created, no migration is run, no database is
provisioned.** The Postgres work is a design so the contract can be reviewed
before any spend.

---

## 1. Provisional design assumptions (explicitly provisional)

These were supplied as working assumptions and are **not** user-confirmed.
They change the schema materially, so they are listed up front and again in
§6 as decisions still required.

| # | Assumption | Consequence if wrong |
|---|---|---|
| A1 | Tenant is the **school/organization**, with scoped memberships. | A teacher-as-tenant model would drop the `tenants`/`memberships` tables and key ownership directly on the actor. |
| A2 | The teacher **account is the actor, never the tenant.** | If an actor could be a tenant, owner-scoping and membership checks collapse into one and RLS predicates change. |
| A3 | Manual uploads get an **opaque, server-issued submission id** bound to (document digest, owner). **No pupil name in any key.** | If a school later needs pupil-scoped history, the opaque id must be re-issued or a separate pupil-reference table added — the digest binding alone cannot recover it. |
| A4 | A **re-mark is an immutable new result generation** with a supersession link to the prior generation. | If re-marks were meant to mutate a stream in place, the `supersedes` chain and the `original_output_immutable` gate would need rework. |

---

## 2. Tenant & membership model

Two tables. Both are **proposed**; neither exists.

### `tenants`
| Column | Type | Notes |
|---|---|---|
| `tenant_id` | `TEXT` PK | Opaque, server-issued. Never derived from a name. |
| `kind` | `TEXT` | `'school'` \| `'organization'` (provisional). |
| `display_name` | `TEXT` | Human label; **not** an identity key. |
| `created_at` | `TIMESTAMPTZ` | |

### `memberships`
| Column | Type | Notes |
|---|---|---|
| `tenant_id` | `TEXT` FK → `tenants` | |
| `actor_id` | `TEXT` | Server-resolved authenticated subject. |
| `role` | `TEXT` | `'teacher'` (provisional; more roles later). |
| `status` | `TEXT` | `'active'` \| `'revoked'`. |
| `created_at` | `TIMESTAMPTZ` | |

- PK `(tenant_id, actor_id)`.
- **The actor is never the tenant** (A2). An actor reaches a resource only
  through an active membership.
- Revocation is a `status` flip, never a row delete, so audit attribution
  survives.

---

## 3. Durable identity — building on what exists

The current app derives storage identity in
`app5_1_automated.durable_result_identity` (line ~2866):

- **Classroom:** `classroom:{course}:{assignment}:{submission}:{revision}`
  — provider-stable; a resubmission is a **new** stream because the revision
  is part of the id. This is already durable and needs no change.
- **Manual:** `manual:{document_digest[:16]}:{review_key}`
  — `review_key` is random per run, so the stream is **session-scoped**. This
  is the documented open decision (AUDIT_STORAGE_CONTRACT §6.3).

### 3.1 Proposed manual identity (resolves §6.3 under A3)

```
manual:{tenant_id}:{submission_ref}
```
- `submission_ref` is an **opaque server-issued id** (random, unguessable),
  created once at upload and returned to the client.
- Bound server-side to `(tenant_id, document_digest, owner)` in a
  `manual_submissions` row. The digest proves the document; the opaque ref
  gives the durable stream; the tenant scopes it.
- **No pupil name, no pupil id, no free text** enters the key or the binding.

### 3.2 Proposed re-mark identity (resolves §6.4 under A4)

- A re-mark of the same underlying work issues a **new** `result_id` and
  records `supersedes = <prior result_id>`.
- The prior generation is **never mutated**. `original_output_immutable`
  already enforces fail-closed on a changed original; the supersession link
  makes the relationship explicit rather than implied by a revision bump.
- `GET /v1/results/{id}` can therefore return a stable chain without any
  in-place rewrite.

---

## 4. Audit tables — migration from SQLite

The C6-B3 store (`audit_store.py`, `SCHEMA_VERSION = 1`) maps to Postgres
with the same composite keys. Per `AUDIT_STORAGE_CONTRACT.md` §7, **only the
adapter changes**; the app layer, gate order and failure codes do not.

### `results`
| SQLite today | Postgres proposal |
|---|---|
| `owner`, `result_id` (composite PK) | `tenant_id`, `owner`, `result_id` (composite PK) |
| `result_revision` INTEGER | `result_revision` BIGINT |
| `original_digest`, `original_scores`, `original_scores_digest` | same, `TEXT` |
| `created_at` | `TIMESTAMPTZ` |
| — (absent) | `supersedes` `TEXT` NULL — the re-mark link (§3.2) |

### `audit_events`
| SQLite today | Postgres proposal |
|---|---|
| `(owner, result_id, sequence)` PK | `(tenant_id, owner, result_id, sequence)` PK |
| partial UNIQUE `(owner, result_id, idempotency_key)` | partial UNIQUE `(tenant_id, owner, result_id, idempotency_key)` |
| `sequence` INTEGER | `sequence` BIGINT |
| event columns (`event_type`, scores, `reason`, `actor`, `binding`, `authorization_evidence`, …) | unchanged, `TEXT` |

### Concurrency & error mapping
| SQLite mechanism | Postgres equivalent | Code preserved |
|---|---|---|
| `BEGIN IMMEDIATE` | `SELECT … FOR UPDATE` or serializable txn | — |
| UNIQUE violation on sequence | `23505` | `stream_sequence_conflict` |
| UNIQUE violation on idempotency (same payload) | `23505` | `idempotency_key_conflict` |
| Same key, different payload | app-checked before insert | `idempotency_key_payload_mismatch` |
| Optimistic revision mismatch | `WHERE result_revision = expected` rowcount 0 | `stale_revision` |

`schema_version` becomes a `schema_migrations` table. The adapter **fails
closed** on an unknown version; it does not auto-migrate.

### Row-Level Security (defense in depth, not a substitute)
RLS can enforce tenant/owner scoping at the database. **The adapter keeps its
explicit owner/tenant predicates regardless**, so behaviour is identical with
or without RLS. RLS is a second lock, never the only one.

---

## 5. Lifecycle requirements — stated, NOT decided

**This proposal deliberately sets no retention or legal policy.** The
requirements and the open questions are recorded so they are not silently
invented at migration time:

| Requirement | Status |
|---|---|
| Retention window per record class | **UNDECIDED** |
| Erasure vs audit accountability (tombstone / hard delete / pseudonymise) | **UNDECIDED** |
| School / GDPR data-subject request path | **UNDECIDED** |
| Backup retention & restore objectives | **UNDECIDED** |
| Event signing / hash chain (a privileged writer can currently rewrite a local file undetectably) | **UNDECIDED — production concern** |

Any of these can change the schema (a `tombstoned_at` column, a separate
erasure ledger). They must be settled **before** the migration, because a
production store has to enforce them from day one.

---

## 6. Decisions still required from the user

These are **separate from** the safe offline progress in this PR. They block
the real migration, not the contract review.

1. **§6.1 Tenant scope** — is the tenant the school, or something coarser/finer?
   (A1)
2. **§6.2 Retention & erasure** — the window, and how erasure reconciles with
   audit accountability under GDPR/school policy. (§5)
3. **§6.3 Manual identity** — confirm the opaque server-issued ref bound to
   (document digest, owner) is acceptable, or specify what must identify a
   manual result durably. (A3)
4. **§6.4 Re-mark identity** — confirm a re-mark is a new immutable generation
   with a supersession link. (A4)

---

## 7. What is NOT in this design

- No live Supabase/Postgres adapter — `audit_store.py` is untouched by this PR.
- No provisioning, no region, no cost.
- No real student data.
- No React/frontend wiring (C10 is a later slice).
- No grade-publication path — the app makes zero mutating Classroom/Drive
  calls, and this contract adds none.
