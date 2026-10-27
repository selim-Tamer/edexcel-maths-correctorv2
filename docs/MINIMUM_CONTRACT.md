# Minimum shared API contract v1.2 (PROPOSED_AWAITING_FCM_CONFIRMATION)

Machine-readable source of truth: [`contract/minimum_v1.json`](../contract/minimum_v1.json).

Status **PROPOSED_AWAITING_FCM_CONFIRMATION**: CM drafted v1.2 on the backend
side; it is **not agreed** until FCM confirms this exact version in writing
for the browser client and CM records both message ids. The previously agreed
version was **v1.1** (FCM confirmed in message
`2d206cc3-69d8-4b22-8859-cd08d6634612`; v1.0 in
`68a3f090-2d3e-47e2-acf9-9dd6d2c4b20d`). Any change to a row needs a new
numbered contract version confirmed by BOTH owners.

v1.2 adds only: `POST /v1/assignments` and `GET /v1/assignments` (server-owned
assignment rows, server-minted `assignment_id`, client idempotency key), the
four projection question fields as optional carry-through, and an explicit
boundary that richer scheme evidence is out of scope. No existing row's
behaviour changes.


This document exists because IFR's live-beta audit found the frontend calling
9-10 routes that the backend does not serve, and the two sides drifting apart
independently. The rule from here: **one route table, owned jointly.** CM owns
the runtime, CORS and the routes themselves; FCM owns the browser client. No
side adds an endpoint the other has not seen.

Nothing here is deployed, migrated, provisioned or billed.

---

## 1. What the frontend calls today, and where it goes

| # | Frontend call (live) | Disposition | Backend route |
|---|---|---|---|
| 1 | `POST /v1/assignments` | mapped | `POST /v1/assignments` (server-owned; `GET /v1/assignments` lists this owner's rows) |
| 2 | `POST /v1/assignments/{id}/uploads` | replaced | `POST /v1/documents?filename=` (raw PDF) |
| 3 | `POST /v1/uploads/{id}/confirm` | removed | - (upload is atomic) |
| 4 | *(none today)* | required addition | `POST /v1/jobs` |
| 5 | `POST /v1/jobs/{id}/control` (`retry`) | mapped | `POST /v1/jobs/{job_id}/run` |
| 6 | `GET /v1/assignments/{id}/jobs` | client-owned | - (poll `GET /v1/jobs/{id}`) |
| 7 | `POST /v1/drafts` | client-owned | - (device storage) |
| 8 | `POST /v1/drafts/{id}/finalize` | client-owned | - (decisions + local score) |
| 9 | `GET /v1/submissions/{id}` | mapped | `GET /v1/results/{result_id}/projection` |
| 10 | `POST /v1/results/{id}/decisions` | mapped | `POST /v1/results/{result_id}/decisions` |
| 11 | `GET /v1/results/{id}/events` | mapped | `GET /v1/results/{result_id}/events` |

`retry` is the only job control the backend has. `cancel`, `pause` and `resume`
must not be rendered as live actions in the beta, and the client must not call
`/control` at all.

**Operational note on `POST /v1/jobs/{job_id}/run`** (factual behaviour of the
endpoint as agreed, not a row change): the executor is synchronous and
lease-guarded. A second call while the lease is live is refused, so the retry
control cannot double-execute a job. A client-side timeout (15 s) abandons the
client's *wait*, not the server-side work; the authoritative state is
`GET /v1/jobs/{job_id}`, so the client recovers by polling that instead of
assuming the run failed.

**Durable worker trigger (contract v1.1, confirmed by both owners).** Row 5 is
unchanged: browser retry still drives the synchronous, lease-guarded
`POST /v1/jobs/{job_id}/run`. What v1.1 settles is who *executes* a queued
job: the durable worker (`worker.py`, one job per Cloud Run Jobs invocation)
when triggered by the operator or a scheduler - concretely
`Cloud Scheduler -> gcloud run jobs execute marker-worker`. The trigger is
operator infrastructure, not a client responsibility and not a server route.
Self-trigger from the API and "no trigger" were explicitly rejected. The
client's recovery path is always polling `GET /v1/jobs/{job_id}` with backoff
**1 s, 2 s, 4 s, then 5 s, stopping at ~2 minutes** and rendering the
last-known state (FCM enforces this schedule in `src/polling.ts`).

**The client currently has no job-creation call.** `confirmUpload` was expected
to start marking, but the backend starts marking only through `POST /v1/jobs`
with a `document_digest`. That is the one call FCM must add: upload returns the
digest, then the client creates the job.

## 2. The upload change (the one real client redesign)

Today: `authorizeUpload` expects a signed URL and `confirmUpload` expects a
second round trip. Beta: one authenticated request.

```ts
// POST /v1/documents?filename=<encodeURIComponent(file.name)>
// Authorization: Bearer <google-id-token>
// Content-Type: application/pdf
// body: the File/Blob bytes
// -> 201 { document_digest, owner, bytes, pages, kind, stored }
// -> 200 same shape, stored:false when the identical bytes were already stored
```

`fileId` is `document_digest`. The server validates before writing (PDF
signature, the **25 MiB application cap**, page count, encryption), computes
the sha256 itself, and stores owner+tenant scoped. There is no bucket URL, no
presigned URL and no public object. Re-uploading identical bytes is
idempotent, which is what makes a retry after a network failure safe.

## 3. Identity and question numbers

* Bearer token = Google **ID token**, verified against `GOOGLE_OIDC_AUDIENCE`.
  GIS supplies it in the browser; no Supabase Auth substitution.
* Tenant and owner are derived server-side from the verified subject and an
  active membership. The client never sends either.
* `question_number` is a **string** on the wire (`"1"`, `"2a"`). The backend
  now also accepts an integer and normalises it to the stripped string, so the
  current `questionNumber: 1` stops being a 400/422 - but the contract value is
  still the string, because question ids are not always numeric.
* `submissionId` is the `result_id` the job returns. There is no separate
  submission resource.

## 4. Idempotency

* Job key: `job-` + first 32 hex chars of
  `sha256(assignmentId + "|" + studentRef + "|" + document_digest)`.
  Deterministic per (assignment, student, uploaded bytes). A retry reuses it;
  only a **new document digest** produces a new key and a new job.
* Decision key: generated once per intended decision and reused verbatim on a
  network retry. Replaying a key with a different payload is refused
  (`idempotency_key_payload_mismatch`), so never derive it from `Date.now()` or
  an attempt counter.

## 5. Error handling the client must implement

| Status + code | Meaning | Client action |
|---|---|---|
| 401 `authentication_required` | missing/invalid/expired token | sign-in flow |
| 403 `tenant_membership_required` | authenticated, **no active membership** | show membership problem; **do not sign out**, do not retry as if expired |
| 403 `manual_document_provenance_required` | result not backed by a succeeded job | fix the flow, not the token |
| 409 `stale_revision` | projection moved on | refetch `GET /v1/results/{id}/projection`, use its `result_revision`, re-apply the unchanged decision |
| 409 `review_question_ambiguous` / 404 `review_question_unavailable` | question id does not resolve to one row | refetch projection; do not retry the same body |
| 409 `idempotency_key_payload_mismatch` | same key, different payload | new key **with a new intent**, or show the conflict |
| 413 `document_too_large` | over the 25 MiB app cap | pre-check client-side; do not retry |
| 503 `store_unusable` / `dev_opt_in_required` / `model_unavailable` | server composition problem | show "service not ready"; no automatic retry loop |
| 401 `authentication_required` (detail `oidc_not_configured`) | server has no OIDC audience | configuration, not a user action |

Startup-log codes (`service_unavailable` for a bad origin allowlist, for
example) mean the process refused to start, so the browser sees a dead
revision rather than a JSON envelope.

## 6. Device state (drafts) and logout

Drafts are **not** a server resource in beta. Key them by
`sub` + `assignmentId` + `studentRef` in IndexedDB (or localStorage with a
size guard). On logout, stop rendering the previous sub's drafts immediately;
the bytes stay on the device under that sub's key and are not readable through
the UI until the same sub signs in again. Offer an explicit "discard local
drafts". Nothing is uploaded by saving a draft, so a different user can never
read another teacher's draft through the API.

Retention: local drafts persist until discarded, finalised or the browser
clears site data. That is a device decision, not a server promise; the backend
retention rules for uploaded PDFs are separate (packet §4).

## 7. CORS

`AI_MARKER_ALLOWED_ORIGINS` is a comma-separated list of **exact** origins.
Production accepts `https` only, no wildcard, no credentials, methods
`GET, POST, OPTIONS`, headers `Authorization, Content-Type, Accept`. A missing
or invalid list fails the process at startup rather than silently allowing
nothing. FCM must supply the final Pages origin (and any preview-origin
decision - previews are not allowed by default).

## 8. Assignments (new in v1.2, PROPOSED)

The assignment row is **server-owned** so it survives a reload and a second
device. Identity is unchanged: `tenant_id` and `owner_subject` come only from
the verified token and active membership.

```jsonc
// POST /v1/assignments  (Authorization: Bearer <google-id-token>)
// request
{ "idempotency_key": "<8..128 chars>", "title": "Year 10 Paper 1" }
// -> 201 { "assignment_id": "asg_<24 hex>", "title": "Year 10 Paper 1",
//          "created_at": 1761600000 }
// -> 200 identical body on same key + same title (replay)
// -> 409 idempotency_key_payload_mismatch on same key + different title
// -> 400 payload_invalid on blank/whitespace title or >200 chars after trim
```

* `assignment_id` is **minted by the server**; the client never sends one.
  Replaying the key returns the same assignment, keyed by
  `(tenant_id, owner_subject, idempotency_key)`.
* `title` is trimmed; 1..200 characters after trimming is the whole accepted
  body. Extra client fields (id, tenant, owner) are not part of the body and
  cannot override the token.

```jsonc
// GET /v1/assignments?limit=1..100&cursor=<next_cursor>
// -> 200 { "assignments": [ { "assignment_id": "...", "title": "...",
//                             "created_at": 1761600000 } ],
//          "next_cursor": "<opaque>" | null }
// -> 400 payload_invalid on a malformed cursor
```

* Scope is the caller's own `owner_subject` within their tenant, mirroring
  documents ownership. A tenant-wide teacher view is an additive later
  version, not a silent change here.
* Order is `created_at DESC, assignment_id DESC` (keyset, no OFFSET); each row
  appears once across pages. `next_cursor` is null when the page is not full.
* No rename and no delete route: the runtime role has `SELECT, INSERT` only on
  the assignments table. Retention belongs to the separate elevated role.

## 9. Projection question fields (new in v1.2, PROPOSED)

`review_status`, `corrected`, `last_action` and `last_reason` are **optional
carry-through** fields on each projection question. Present when the backend
has stored a value, absent otherwise; the client renders nothing when a field
is absent and never substitutes a default that could read as review evidence.

**Out of scope and explicitly not done:** richer scheme evidence - per-question
mark-scheme lines, A/M/B scheme annotations, or any engine-output field beyond
those four. That needs its own owner-approved scope and a new numbered
contract version. It is not invented here and not silently deferred as if
delivered.

## 10. What is deliberately NOT in v1.2

* Server-side **drafts** and **queue** state (still device-local), and a
  tenant-wide assignment view.
* Validating an `assignment_id` on `POST /v1/documents` or `POST /v1/jobs`;
  document/job-to-assignment linkage is a separate agreed addition.
* Signed upload URLs and direct-to-bucket writes.
* `cancel` / `pause` / `resume` for jobs.
* A submission resource separate from `result_id`.
* Streaming/websocket updates: the client polls `GET /v1/jobs/{id}`.
* Richer scheme evidence (section 9 boundary).

Adding any of these later is a new numbered contract version agreed by both
sides first, not a quiet route addition.
