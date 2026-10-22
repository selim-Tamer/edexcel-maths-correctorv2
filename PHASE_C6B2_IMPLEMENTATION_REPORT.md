# PHASE C6-B2 IMPLEMENTATION REPORT — Secure Question-Level Teacher Review and Mark Corrections

Status: **IMPLEMENTED — DRAFT FOR INDEPENDENT SECURITY REVIEW. NOT MERGED, NOT DEPLOYED.**
No force-push. No merge. No deployment. Draft pull request only, per instruction.

---

## A. Scope, baseline and branch

| Item | Value |
|---|---|
| Repository | https://github.com/selim-Tamer/edexcel-maths-correctorv2 |
| Baseline | `3ab1dc25d9ef262e3e9e637dfabedfbd5b2f72c5` — PR #45 (C6-B1) verified merged into `origin/main` as `3ab1dc2` |
| Branch | `feature/c6b2-question-level-teacher-review` (created from `origin/main` at baseline) |
| Local repository | `C:\Users\user\Documents\Default Project\edexcel-maths-correctorv2` |
| Python / Streamlit | 3.14.4 / 1.66.1 (matches the CI UI matrix) |

This phase first closed the two open C6-B1 security findings (F-1, F-2), then built the
question-level teacher review workflow on top of the existing override machinery.

**Explicitly out of scope, and verified untouched:** scheme-level / whole-paper approval
activation, Google Classroom grade publication, Gemini models/prompts/mark-scheme maxima,
and the React/Vite frontend.

## B. C6-B1 finding F-1 — Classroom permission freshness at privileged boundaries

**Finding.** A privileged decision could rely on a Classroom grant cached minutes earlier;
provider-side revocation or an assignment edit inside that window would not be detected.

**Fix.** A new pure, network-free contract
`teacher_authorization.reverify_assignment_access(state, subject, course_id, coursework_id,
trusted_courses, trusted_assignments)` re-verifies, at the moment of a privileged decision,
that:

- the course is present in a **fresh** `teacherId=me` listing;
- the coursework is present in a fresh per-course listing as a published `ASSIGNMENT`
  with a computable revision;
- an owned grant exists for **that exact course/coursework**;
- the freshly computed assignment revision equals the recorded revision
  (mismatch → `assignment_revision_changed`);
- the grant is inside its trust window (`assignment_grant_expired` otherwise).

It returns the decision binding `{owner, course_id, coursework_id, assignment_key,
revision, verified_at}` and fails closed with content-free error codes. The provider
boundary (listings, token) is supplied by the caller, so the module itself makes no
network call and the tests run fully offline on synthetic listings.

**App surface.** `reverify_classroom_review_access(row)` is called **only** inside a
question-review mutation (a teacher clicking submit). It fetches fresh listings with the
authenticated teacher's own server-side token, then calls the contract above. It is never
called during ordinary marking, rendering or read-only browsing — proven by test A30, which
asserts zero listing calls on the render path. Classroom rows now carry
`classroom_course_id` / `classroom_assignment_id` / `classroom_context`, recorded once at
marking time from the trusted listing that selected the coursework (never browser-supplied),
so a decision binds to the exact teacher, course and coursework. Missing metadata, missing
token, API failure, cross-course, cross-teacher and stale-grant cases all fail closed.

## C. C6-B1 finding F-2 — Trusted document provenance for privileged bindings

**Finding.** A direct privileged binding could be made with an arbitrary, well-formed
document digest that no session had actually uploaded.

**Fix.**

- `ServerIdentity` gains `document_digest`.
- `ApprovalAuthority._identity_for(assignment_id, document_digest=None)` passes the digest
  to the trusted verifier and requires the returned identity to carry exactly that digest
  (`document_provenance_unverified` otherwise). Verifier `AuthorizationError` codes now
  survive (specific refusal codes preserved); unexpected failures still collapse to
  `authorization_unavailable`.
- `teacher_authorization._verifier` accepts the optional digest: manual scope requires an
  **owned** grant for **that** digest (`manual_document_provenance_required`), not merely
  any grant.
- `scheme_review.bind_scheme_review` refuses manual-scope bindings that cannot prove
  provenance (A-1: forged/malformed digests refused).
- `scheme_review._bound_review` re-proves the bound digest on every approval use
  (approve/reject/resolve/**display**), so grant eviction or a foreign-session document
  invalidates effective approval — approval display never relies on stored metadata.
- `ApprovalAuthority.prune_superseded` permanently evicts bound manual reviews whose digest
  no longer re-verifies (A-5).
- New **read-only** `teacher_authorization.manual_document_owned(state, subject, digest)`
  for display-time gates; it never mints authorization state.

## D. Question-level review workflow (new)

New pure module `question_review.py` (no Streamlit, no network, no filesystem):

- statuses `unreviewed / approved / corrected / flagged`;
- integer-rubric mark validation: rejects bool, NaN, ±inf, non-numbers, negative,
  `> max_marks` and **fractional** marks — no silent rounding anywhere;
- reason validation: non-empty, bounded (2000 chars), plain text; required to correct
  marks or to flag;
- `question_review_binding(...)` — a SHA-256 fingerprint over the pristine AI question
  marks, the marking-evidence digest, the row revision and the selected-question set;
- `evaluate_question_review(...)` — decision rules: `unreviewed` is not submittable,
  `corrected` requires marks, `approved` must not carry marks, a **live** flag cannot be
  silently approved without a reason while a flag on superseded evidence is simply
  superseded (so changed evidence never traps a row);
- `bounded_events(...)` — audit trail capped at 500 events, oldest dropped.

**Single override system preserved.** A mark correction is routed through the existing
`save_teacher_override`, so `teacher_overrides` / `teacher_override_reasons` stay the one
override store, the original AI marks stay recorded on `teacher_override.original_marks`,
and totals, exports and the Classroom grade preview recalculate through the existing
`calculate_totals` / `percentage_band_grade` rules. Nothing here publishes a grade.

**State.** `question_review_decisions` and `question_review_events` were added to
`SENSITIVE_STATE_KEYS`, and the `qreview_*` widget prefixes to
`SENSITIVE_WIDGET_PREFIXES`, so logout and account-switch reconciliation destroy them
exactly like every other owner-bound store.

## E. Authorization gates and identity

Every question-review mutation runs these gates server-side, in order, failing closed:

1. **Authenticated identity** — `authenticated_google_identity()` must return a subject;
   the recorded `actor` is the server-derived owner token (`google:<sub>`), never a widget
   value.
2. **Ownership** — the row must be found in an owner-verified store
   (`owned_review_row`) and its `report_owner` must match the current owner.
3. **Provenance, manual rows** — `manual_document_provenance_verified(row)` must prove the
   row's document digest is an owned manual grant of the current teacher.
4. **Fresh Classroom re-verification, classroom rows only, mutation only** —
   `reverify_classroom_review_access(row)` (F-1). Read-only rendering never calls it.
5. **Validation** — status, reason and (for corrections) the mark are validated by
   `question_review`; an invalid mark or missing reason rejects the whole decision and
   writes no override.

Rendering is additionally gated: review controls render only when a `review_row` context
is passed **and** `row_owner_allowed(row)` passes, so historical rows, foreign rows and
legacy call sites render no controls at all.

## F. Invalidation when evidence or revision changes

A stored decision is displayed only while its binding matches the binding recomputed from
current evidence (`stored_question_review`). Any of the following invalidates it
immediately and visibly (status column shows "Invalidated (evidence changed)" and the
per-question block shows a warning):

- the pristine AI marks for that question change (regrade);
- the marking-evidence digest changes (new mark scheme / document);
- the Classroom row revision changes (student resubmission);
- the selected-question set changes.

An invalidated decision is never silently reused; the question returns to `Unreviewed`
for display purposes and must be reviewed again.

## G. Tests — all offline, synthetic provider responses only

61 new tests, all passing, all offline. The headless loader's shared `requests` fake
**raises** on any unexpected HTTP call, so a green run proves no network access occurred.

| File | Tests | Focus |
|---|---|---|
| `tests/test_c6b2_security.py` | 28 | F-1 freshness + F-2 provenance (A-1 … A-11 family) |
| `tests/test_question_review.py` | 33 | The 30 enumerated adversarial cases A01–A30 plus callback/UI-surface cases |

The 30 enumerated adversarial cases in `tests/test_question_review.py`:

| ID | Adversarial case | Result |
|---|---|---|
| A01 | Boolean mark (`True`/`False`) | rejected `mark_not_a_number` |
| A02 | NaN / ±inf mark | rejected `mark_not_finite` |
| A03 | Negative mark | rejected `mark_negative` |
| A04 | Mark above scheme maximum | rejected `mark_exceeds_scheme_maximum` |
| A05 | Fractional mark | rejected, never rounded |
| A06 | Non-numeric mark / unknown-maximum | rejected, fails closed |
| A07 | Empty / non-text / over-long reason | rejected |
| A08 | AI mark change | binding invalidated |
| A09 | Scheme digest change | binding invalidated |
| A10 | Row revision change | binding invalidated |
| A11 | Selection change | binding invalidated |
| A12 | Question / student / assignment mismatch | binding invalidated |
| A13 | Malformed binding digests | never match |
| A14 | `unreviewed` submitted | not submittable |
| A15 | `corrected` without valid marks/reason | rejected |
| A16 | `approved` carrying corrected marks | rejected |
| A17 | Live flag silently approved | requires a reason |
| A18 | Flag on stale evidence | superseded, not sticky-trapped |
| A19 | Audit event flood | bounded to 500, oldest dropped |
| A20 | Anonymous session | rejected, nothing stored |
| A21 | Foreign / unowned / mismatched row | rejected |
| A22 | Manual row without owned document | rejected |
| A23 | Owned document approval | recorded with server-derived actor |
| A24 | Correction | routed through `save_teacher_override`, AI marks preserved |
| A25 | Invalid corrections (bool/NaN/inf/neg/>max/fractional/None) | no override written |
| A26 | Correction/flag without reason | rejected, no override written |
| A27 | Classroom decision with revoked access | denied; fresh listing permits and binds |
| A28 | Classroom API failure at the boundary | fails closed, second listing not called |
| A29 | Evidence change after a stored decision | decision invalidated (all three vectors) |
| A30 | Read-only rendering | zero Classroom requests; no controls without a review row |

No existing test was weakened. Two existing test fixtures in
`tests/test_scheme_review.py` and `tests/test_scheme_authorization.py` were updated for the
extended `verify(assignment, document_digest=None)` contract (vouching only for the
synthetic `"a"*64` digest); their assertions are unchanged.

## H. Test results (recorded counts)

| Suite | Command | Result |
|---|---|---|
| Offline regression | `python -m unittest discover -s tests -p 'test_*.py'` | **654 OK** (baseline 593 + 61 new) |
| Real Streamlit UI | `python -m unittest discover -s tests/ui -p 'test_*.py'` | **40 OK** |
| SDK (intercepted HTTP) | `python -m unittest discover -s tests/sdk -p 'test_*.py'` | **14 OK** |
| Accuracy evaluator | `python -m unittest investigation.test_accuracy_regression` | **28 OK** |
| Scheme-review eval | `python -m unittest investigation.test_scheme_review_eval` | **15 OK** |
| C5 consolidation differential | `python -m unittest investigation.test_c5_consolidation_differential` | **11 OK** |
| Syntax check | `python -m py_compile` (app, pipeline, review, auth, `question_review`) | **OK** |
| Benchmarks (10 scripts) | `python tests/benchmark_*.py` | **10/10 exit 0** |

Total: **762 tests green**, offline. `benchmark_parser_deadline.py` reported one
non-zero exit on a first run under heavy parallel load; it re-ran green three consecutive
times afterwards. That benchmark measures PDF-parser kill timing and touches no code this
phase modified (`document_limits`, PDF parsing unchanged); recorded here for honesty, not
treated as a regression.

## I. Supply-chain / dependency

No dependency was added, removed or pinned. `question_review.py` uses only the standard
library (`hashlib`, `json`, `math`, `typing`). The `pip-audit` dependency-audit job
requires no change; the workflow's syntax-check list was extended to compile
`question_review.py`, `teacher_authorization.py` and `scheme_authorization.py`.

## J. Residual risks and limitations

1. **Revocation latency.** F-1 re-verifies at the moment of each privileged decision, so a
   revocation is detected from the next decision onward. A decision already recorded stays
   in session state until the session is reconciled or ends; it is never exported or
   published by this phase.
2. **Session-scoped review state.** Decisions and audit events live in owner-bound
   Streamlit session state and are destroyed on logout/account switch. They are not a
   durable audit log; a deployment needing long-term auditability must persist them
   server-side in a later phase.
3. **Silent OIDC cookie refresh** (inherited C6-B1 limitation, unchanged) is not
   observable, so the authority nonce does not rotate at cookie-refresh boundaries.
4. **Fractional rubrics.** Mark validation assumes an integer rubric, matching the current
   EdExcel mark scheme structure and the existing `clamp_mark` behavior. If a future paper
   legitimately awards fractional marks, `question_review.validate_mark_value` must be
   extended deliberately, not silently.
5. **Manual rows depend on a live document grant.** If the bounded manual-grant store
   evicts a document (50-entry cap), review controls for rows pinned to that document
   disable until the document is re-uploaded. This is the intended fail-closed direction.

## K. Recommendation and deliverables

**Recommendation: GO WITH CONDITIONS.**

The implementation is complete, fails closed on every examined boundary, keeps a single
override system, activates none of the out-of-scope surfaces, and is covered by 61 new
offline adversarial tests with 762 tests green in total. Conditions for merge/deploy:

1. Independent security review of F-1/F-2 changes in `scheme_authorization.py`,
   `teacher_authorization.py` and `scheme_review.py` (this report is a draft for that
   review, not a substitute for it).
2. Explicit maintainer approval before any merge (this phase merged nothing).
3. A live, maintainer-run, test-account smoke test of the manual and Classroom review
   flows before production deployment — all tests here are offline by design, so live
   provider behavior is deliberately not claimed.
4. A decision on J-2 (durable audit persistence) before the feature is relied on for
   formal grading workflows.

**Deliverables**

| Deliverable | Location |
|---|---|
| F-1 freshness contract | `teacher_authorization.py` (`reverify_assignment_access`) |
| F-2 provenance | `scheme_authorization.py`, `teacher_authorization.py` (`manual_document_owned`), `scheme_review.py` |
| Question review model | `question_review.py` |
| App gates, handlers, UI | `app5_1_automated.py` |
| Adversarial tests | `tests/test_c6b2_security.py`, `tests/test_question_review.py` |
| Test-loader seam | `tests/support.py` |
| CI syntax list | `.github/workflows/marking-tests.yml` |
| Branch / PR | `feature/c6b2-question-level-teacher-review`, draft PR against `main` |
