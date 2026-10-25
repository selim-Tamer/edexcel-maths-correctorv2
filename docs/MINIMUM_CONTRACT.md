# Minimum shared API contract v1 (proposed)

Machine-readable source of truth: [`contract/minimum_v1.json`](../contract/minimum_v1.json).
Status **PROPOSED_AWAITING_FCM_CONFIRMATION** - it becomes `AGREED` only with a
`confirmed_by` message reference from FCM for this exact version.

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
| 1 | `POST /v1/assignments` | client-owned | - (local object, client id) |
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

## 8. What is deliberately NOT in v1

* A server-side assignment/queue/draft model. It needs its own tables,
  authorization and migration; it is not required to prove marking end-to-end.
* Signed upload URLs and direct-to-bucket writes.
* `cancel` / `pause` / `resume` for jobs.
* A submission resource separate from `result_id`.
* Streaming/websocket updates: the client polls `GET /v1/jobs/{id}`.

Adding any of these later is a new numbered contract version agreed by both
sides first, not a quiet route addition.
