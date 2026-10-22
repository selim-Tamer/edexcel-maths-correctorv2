# Phase C5 Consolidation Report

**Mark-scheme reviewer — independent review, reconciliation and verification**

Status: draft for independent PR review. No merge, no deploy.
Baseline: `75f0a9981ee69b0fc6545a2f54e980affbf91c72` (merge of PR #41, `origin/main`).
C5 branch reviewed: `accuracy/c5-mark-scheme-reviewer`.
- Implementation A (DeepSeek): commit `9c6cc5e`, `investigation/scheme_review.py`.
- Implementation B (GLM): commit `a2af273`, `scheme_review.py` (root).

---

## 1. Baseline and final SHAs

| Item | SHA |
|---|---|
| Production baseline (`origin/main`) | `75f0a9981ee69b0fc6545a2f54e980affbf91c72` |
| Implementation A commit | `9c6cc5e` |
| Implementation B commit (= branch HEAD before consolidation) | `a2af273403a9f1e6ae660bbdb8563c8f76cb7798` |
| Consolidation branch | `accuracy/c5-consolidated-reviewer` |
| Consolidation commit | `75e0568` (recorded in the follow-up report-fix commit) |

Both original C5 commits are preserved in history; neither was rewritten or force-pushed.

---

## 2. Architecture comparison (summary)

| Dimension | Implementation A (`investigation/scheme_review.py`) | Implementation B (`scheme_review.py`, root) |
|---|---|---|
| Positioning | Offline investigation reviewer, strict closed request schema | Production deterministic reviewer, tolerant of real extraction shape |
| Entry point | `review_scheme(payload, provider, cache)` | `build_review`, `review_extracted_maxima`, `approve_scheme_review`, `resolve_findings` |
| Contract | `investigation.scheme_review.result/1`, three channels + `safeguards` | `app.scheme_review/1`, channels `extracted`/`verified`/`teacher`/`assisted` |
| M/A/B marks | Derived from C2 `code_is_supported`; dM folded to M with `dependent_method` flag | `code_is_supported` + explicit `dM` mark type; UNKNOWN is explicit |
| Criterion dependencies | Cross-question, forward, dependent-method-without-dep checks | C2 bridge (`C5-DEP-UNDEFINED`, `C5-DEP-CYCLE`, etc.) |
| Alternative methods | Group-aware **effective total** = standalone + Σ max(option totals); never sums branches; explains C2 total mismatch with traceability | Any `alternative_group` → generic `C5-ALT-SUM-REVIEW` + (if criteria sum ≠ total) a second `C5-TOTAL-MISMATCH`; **double-reporting, no branch-max reconciliation** |
| Follow-through | FT without earlier criterion / unspecified source / FT-code checks | C2 bridge + `C5-FT-UNSUPPORTED` (evidence-gated) |
| Question mapping | C3 `map_question_pages`, ambiguous/unresolved/heuristic findings | C3-shaped `bind_evidence`, `C5-MAPPING-CONFLICT`/`-UNCERTAIN` |
| Source evidence | C3 `validate_document_evidence`, verbatim `evidence_reference`, missing-doc blocking | `bind_evidence` verbatim containment, `C5-EVIDENCE-CONFLICT`, unavailable≠contradiction |
| Hallucination detection | Per-finding provider validation (unknown category/question/criterion/confidence), rejections preserved | `normalize_assisted_findings` fail-closed (any invalid → `invalid_response_shape`) |
| Teacher approval | Owner+assignment+revision+digest-bound; hard blockers never acknowledged away; `resolve_findings` | `resolve_findings` → `approve_scheme_review` → `approval_state`; blocking set explicit. Owner binding lives app-side (ack layer); engine identity/revision-argument validation is C6 pre-work (§10) |
| Review revision | `compute_revision` sha256 | `compute_revision` sha256 |
| Provider budgets | `MAX_PROVIDER_REQUESTS = 1`, owner-scoped `ReviewCache`, `ASSISTED_REVIEW_ACTIVATED`-equivalent via optional provider arg | `ASSISTED_REVIEW_ACTIVATED = False`, no live client, parse-only |
| Production integration | **None** (investigation-only, guarded by `tests/test_c5_mark_scheme_reviewer.py`) | `prepare_reference_metadata` caching, `render_scheme_review_panel`, warning channel, sensitive-state keys |
| Regression coverage | 121 investigation tests + 26-case benchmark (`all_matched`) | 51 unit + 8 integration + 5 UI + 15 eval + benchmark (`summary_ok`) |

Full 18-category matrix is retained in the working notes; the decision-relevant rows are above.

---

## 3. Chosen strategy: **Strategy B** (production reviewer authoritative, port stronger checks from A)

Root `scheme_review.py` remains the single authoritative engine. Rationale:

1. It is the only implementation wired into production (`prepare_reference_metadata`,
   `render_scheme_review_panel`, warning channel, sensitive-state keys, UI tests).
2. Its contract `app.scheme_review/1` is the one the app already consumes; promoting A
   would require a compatibility shim and rework of every integration test.
3. Its approval flow already supports the full lifecycle A lacks in a mutable form
   (`resolve_findings` → `approve_scheme_review` → `approval_state`) with an explicit
   blocking set; owner binding is enforced app-side (acknowledgement layer), and the
   engine-level identity/revision-argument gaps are recorded as C6 pre-work (§10).
4. A is investigation-only by design and is explicitly barred from production by
   `tests/test_c5_mark_scheme_reviewer.py` (asserts `app5_1_automated.py` never mentions
   `investigation.scheme_review`). Promoting A would break that safety boundary.

### Rejected alternatives

- **Strategy A (promote investigation reviewer):** rejected. Would require a production
  adapter, break the production-boundary guard, discard working Streamlit/UI integration,
  and A rejects malformed input by raising (losing signals) rather than representing them
  as findings.
- **Strategy C (shared core extraction):** rejected as over-engineering for this phase.
  The two engines have different contracts and vocabularies; a shared core would need a
  third schema and a migration, increasing regression risk for no verified accuracy gain
  on this pass. Revisit only if C6 needs both call shapes.

---

## 4. What is ported from A into B (the consolidation change)

The differential (Phase 10) proved three concrete weaknesses in B that A already solved.
These are ported:

1. **Alternative-method branch-max reconciliation.** `check_question_totals` now
   computes a group-aware effective total (standalone + Σ max(option totals)) when every
   group member carries an `alternative_option` label. A consistent labelled structure is
   `CHECKED`; an over-allocating branch yields a single precise `C5-TOTAL-MISMATCH`;
   an unlabelled/ambiguous group still yields `C5-ALT-SUM-REVIEW` (escalated, never summed).
   Branches are never double-counted and never silently summed.
2. **Explained C2 total-mismatch suppression (corrected description).** When a
   labelled alternative structure legitimately explains a criteria-sum ≠ total situation,
   the C2 engine still produces its raw `SCHEME-TOTAL-MISMATCH` finding during the
   review; `_mark_explained_total_mismatches` then **suppresses that single redundant
   finding** from the final C5 findings list, because the branch-max reconciliation
   (standalone + Σ max(option totals)) fully accounts for the raw-sum difference and
   the structure is not actually inconsistent. For such a question `check_question_totals`
   emits no finding either, so a consistent labelled structure reviews cleanly with zero
   findings (behaviour pinned by the `labelled-alternatives-match` differential oracle
   case). The audit trace is: (a) the group/branch labels the criteria keep in the
   record's `extracted` channel, and (b) this deterministic, documented suppression rule.
   **No per-review "explained" trace object is persisted** — earlier drafts of this
   report claimed a retained `evidence.explained_by_alternatives` finding; that was a
   design-stage idea that was not implemented, and the report has been corrected.
   A total mismatch the structure does *not* explain is retained untouched, and
   `check_question_totals` reports its own shortfall/overcount finding alongside it.
3. **Never abort on a single bad criterion.** (Already B behaviour; locked in by oracle.)
   A rejects negative marks with an exception; B represents them as `C5-MARKS-NEGATIVE`
   and keeps reviewing. B's represent-don't-abort model is kept and pinned.

New/updated surface in `scheme_review.py`:
- `alternative_option` field accepted in criterion normalisation (bounded string;
  malformed labels are coerced to `None` and escalate — never summed).
- `_reconcile_alternative_groups`, `_alternative_group_findings` and
  `_mark_explained_total_mismatches` helpers.
- `check_question_totals` now returns `(findings, explained)` and performs
  branch-max reconciliation.
- `_blocking_findings` is unchanged; suppression happens by *removing* the redundant
  finding before it reaches the record (see point 2 above).

`C5-ALT-SUM-REVIEW` remains acknowledgement-grade (non-blocking, escalated): the finding
forces `NEEDS_TEACHER_REVIEW` but is not in the blocking set. This is test-backed, not
just probed — the differential oracle pins `blocking: false` for the
`unlabelled-alternatives` case (`investigation/c5_differential_cases.json`, asserted by
`investigation/test_c5_consolidation_differential.py`).

---

## 5. Features preserved from each implementation

**From DeepSeek (A):** alternative-method branch-max reconciliation (with deterministic
suppression of the redundant C2 total mismatch it explains — see §4 point 2); the
"never abort on malformed single field" principle contrasted against A's
own exception path (we keep B's stronger represent-don't-abort); strict per-finding
provider hallucination validation as the reference model for the assisted channel.

**From GLM (B):** the authoritative `app.scheme_review/1` contract; production integration
(`prepare_reference_metadata` digest-keyed review caching, warning channel,
`render_scheme_review_panel`); the approval lifecycle functions
(`resolve_findings`, `approve_scheme_review`, `approval_state`) — engine-level identity
and revision-argument validation are **not yet enforced** and are recorded as C6 pre-work
(§10); owner binding today lives in the app's Streamlit acknowledgement layer;
sensitive-state ownership and logout clearing; the disabled-by-default
assisted channel with strict parse-only response handling; the full offline regression
suite, UI tests, eval harness and benchmark.

---

## 6. Features deliberately removed

None removed. Implementation A is retained in `investigation/` as the reference
investigation engine (its production-boundary guard stays green). No duplicate *active*
rule engine exists in production: B is authoritative, A is not imported by the app.

---

## 7. Remaining limitations / C6 candidates

- Assisted (Gemini) review stays fully disabled (`ASSISTED_REVIEW_ACTIVATED = False`);
  the root engine contains no provider client and no live request path exists — the
  guarantee is structural. Owner-scoped provider caching (`ReviewCache`) and per-request
  budgets (`MAX_PROVIDER_REQUESTS`) exist **only in the investigation reference engine A**,
  not in the production reviewer; they are not active production capabilities. Any future
  assisted activation requires a separately approved implementation with its own
  quota/security review — it is not a configuration flip.
- The consolidation ports structural reconciliation only. C4 mathematical verification is
  unchanged and still offline-diagnostic.
- Criterion-level approval granularity (approve one question, reject another) exists in A's
  vocabulary but is only partially surfaced in B's UI; deferred to C6.

---

## 8. Test and benchmark results

All runs executed offline on the consolidation branch (no live Gemini/Classroom
calls, no student data, no network). Commands per `AGENTS.md`.

### Full regression suite (post-change)

| Suite | Command | Result |
|---|---|---|
| Backend | `python -m unittest discover -s tests -p 'test_*.py'` | **500 OK** (baseline 493 + 7 new M3 tests) |
| UI | `python -m unittest discover -s tests/ui -p 'test_*.py'` | **39 OK** (baseline 39) |
| SDK | `python -m unittest discover -s tests/sdk -p 'test_*.py'` | **14 OK** (baseline 14) |
| Investigation | `python -m unittest discover -s investigation -p 'test_*.py'` | **640 OK** (baseline 629 + 11 consolidation-gate tests) |
| Compile | `python -m py_compile app5_1_automated.py scheme_review.py` | exit 0 |

No pre-existing test was modified; additions are the 11 consolidation-gate tests and
the 7 M3 stale-review-cache tests below.

### M3 fix: stale review cache (final-corrections commit)

Root cause: the C5 review was cached under `(scheme_key, "scheme_review")`
unconditionally, while the extraction it reviewed was cached only when clean
(`info[0] and not info[2]`). A failed/warning-producing extraction was therefore
retried (new model output, possibly different maxima/answers) while the **stale review
of the first attempt kept serving the warning channel** — it could mask new findings or
introduce warnings for superseded data.

Fix (`app5_1_automated.py`): new `scheme_review_inputs_digest(maxima, answers, numbers)`
— a deterministic sha256 over the canonical JSON of every material review input — now
keys the cached review as `(digest, review)`. Any change in maxima, answers or question
inventory recomputes the review; identical inputs still hit the cache. No model call is
involved in review computation, so Gemini request counts are unchanged; the extraction
cache itself is untouched.

Regression tests (`tests/test_scheme_review_integration.py::StaleReviewReuseTests`,
7 tests, synthetic/mocked only): same-extraction cache reuse (1 review call);
changed maxima / changed answers / failed-extraction-then-retry / question-inventory
change each recompute the review and the warning channel matches the current extraction;
per-session metadata stores never share review entries; three repeated calls add zero
provider requests and keep the extraction cached. Verified to **fail against the
original behaviour** (5 of 7 fail/error with the pre-fix code: the three stale-warning
differentiators plus two cache-shape assertions) and pass with the fix.

### Differential gate (new, permanent, in CI)

`python -m unittest investigation.test_c5_consolidation_differential` → **11 tests OK**.

Oracle case results on `investigation/c5_differential_cases.json` (10 synthetic cases):

| Engine | Pass | Failures |
|---|---|---|
| Baseline B (unmodified `a2af273`, same oracle) | **8/10** | `labelled-alternatives-match`, `labelled-alternatives-overcount` |
| Consolidated B (this branch) | **10/10** | — |
| Original A (`9c6cc5e`, unchanged) | status matches oracle **8/10**; semantic rule match **3/10** | status: `labelled-alternatives-match`, `negative-marks` (A aborts); rule vocabulary divergence on shortfall/unlabelled cases |

A's numbers are divergence evidence only: A's rule vocabulary differs from the
B-aligned oracle by design (see §4 oracle alignment), and A genuinely aborts on
negative marks and over-reports a consistent labelled structure.

### Benchmarks (all pre-existing harnesses, re-run post-change)

| Harness | Result |
|---|---|
| `python tests/benchmark_scheme_review.py` | exit 0; status `CHECKED`, 14 checks, 0 provider requests, `live_calls: none`; deterministic review median ≈ 2.4 ms (20 questions × 3 criteria, 15 repeats) |
| `python -m investigation.scheme_review_benchmark` | 26/26 cases, `all_matched: true`; status accuracy 1.0, finding recall 1.0, precision 1.0, false positives 0; C2 preservation 7/7; alternative group 3/3 |
| `python -m investigation.scheme_review_eval` | 13/13 cases, `summary_ok: true`; false-positive rate 0.0; 19/19 expected rules detected; escalation correct 13/13; baseline-beyond 10/12 defect cases |

### Adversarial / security scenarios (Phase 11)

Verified covered by existing tests — all green, none weakened. Test attribution
(corrected): the engine-B tests exercise the authoritative root reviewer; the
`test_c5_mark_scheme_reviewer.py` tests exercise the **investigation reviewer A**
(they import `investigation.scheme_review`) and pin the production-boundary guard,
not engine-B behaviour.

Engine-B tests (`tests/test_scheme_review.py`, import root `scheme_review`):
- Anonymous/empty identifiers cannot approve: `test_anonymous_identifiers_cannot_approve`.
- Stale revision invalidates approval (record mutated after approval):
  `test_stale_revision_invalidates_approval`.
- Blocking findings cannot be silently certified:
  `test_missing_evidence_cannot_be_silently_certified` and friends.
- Provider-disabled and bounded-record guarantees: source-scan tests
  (`generate_content`/`from google`/`client.`/`requests.` absent) and JSON-bound tests.

Engine-A / boundary tests (`tests/test_c5_mark_scheme_reviewer.py`, import
`investigation.scheme_review`):
- The marking app never imports engine A; A has no file IO or dynamic execution (AST).
- A's provider budget is at most one request; provider failure never leaks its message;
  A's result contract excludes student marks. (Reference-engine evidence — these
  properties are *not* implemented in, and not needed by, the root engine, which has
  no provider path at all.)

Streamlit integration tests (`tests/ui/test_streamlit_scheme_review.py`,
`tests/test_scheme_review_integration.py`, `tests/ui/test_student_data_isolation.py`):
- Anonymous session cannot acknowledge; acknowledgement is owner-bound and cleared on
  logout/account switch; a foreign teacher's click never checks the widget.

Shared security regression suites (`tests/test_authentication_guard.py`,
`tests/test_failure_diagnostics.py`): owner isolation / fail-closed identity.

Engine-level input handling: malformed `alternative_option` input fails closed —
`_normalize_criterion` coerces non-string/empty/overlong labels to `None`, so branch
labels are always `str | None` (hashable) and any malformed label escalates to teacher
review — never summed, never crashing the reviewer.

The consolidation touched only deterministic totals reconciliation; approval,
acknowledgement, owner-binding and provider-failure paths were not modified.

---

## 9. Merge recommendation

**Recommend merge after independent review; do not deploy without teacher/user approval** (per `AGENTS.md`).

- Exactly one authoritative C5 engine: root `scheme_review.py`. `investigation/scheme_review.py`
  stays untouched as reference/legacy and remains barred from production by
  `tests/test_c5_mark_scheme_reviewer.py`.
- Alternative methods are never double-counted: effective total = standalone +
  Σ max(branch totals); consistent labelled structures yield zero findings;
  unlabelled structures escalate (acknowledgement-grade, never blocking alone).
- Assisted/provider review remains disabled (`ASSISTED_REVIEW_ACTIVATED = False`);
  zero live calls by construction (benchmark-confirmed).
- S2–S6 security controls intact; no marking-logic, no student-data, no credential changes.
- Both original C5 commits preserved; no force-push; no unrelated files in the diff.
- Deferred findings requiring C6 pre-work are listed in §10; none is reachable from the
  current production surface.

---

## 10. C6 pre-work: deferred findings (recorded, not implemented here)

Distinguished explicitly: the production surface today (an owner- and content-hash-bound
acknowledgement checkbox in `render_scheme_review_panel` that **never mutates findings**,
plus a digest-keyed warning channel) is fail-closed and fully tested. The weaknesses below
are **latent engine-API properties** — they matter only when C6 wires the approval API
(`resolve_findings` / `approve_scheme_review`) into the app or extends the assisted
channel. Nothing here is implemented in this phase.

### M2 — engine-level approval validation (required before C6 activates the approval API)

Independent review found:

1. `resolve_findings()` does not validate teacher identity (any caller-asserted string,
   including blank/whitespace, is accepted).
2. `approve_scheme_review()` does not verify that its `scheme_revision` argument matches
   the record's own revision; a mismatched revision is stored unchallenged.
3. `determine_status()` / `review_summary()` display `TEACHER_APPROVED` without
   consulting `approval_state()`, so a stale approval can *look* valid on status surfaces
   even though `approval_state()` correctly reports `stale_revision`.
4. Hard blockers can be cleared by one blanket `resolve_findings` call at the engine
   layer (the production UI cannot do this; a test pins the current engine behaviour).

Required before C6 activates the approval API:

1. Validate non-blank authenticated teacher identity in both `resolve_findings` and
   `approve_scheme_review`.
2. Bind approval to the authenticated owner (mirror engine A's
   `_decision_applicability` owner check).
3. Validate assignment and scheme digest where available.
4. Reject revision mismatches at approval time (`approval_revision_mismatch`).
5. Ensure stale approvals cannot appear valid on any status surface.
6. Define which findings are resolvable versus non-waivable (`BLOCKING_RULES` /
   evidence-blocking findings should refuse resolution; require re-extraction instead).
7. Add independent regression tests for every case above.

### Lower-severity follow-ups

- **L1** — `_normalize_mark_value` silently truncates float maxima (`3.99 → 3`) and
  `NaN`/`inf` raise raw `ValueError`/`OverflowError` (not `SchemeReviewError`), which the
  production call site does not catch. Production reachability is low (upstream coerces
  to int), but the published entry point should convert these into `C5-MARKS-INVALID`
  findings and/or degrade to "review unavailable" instead of aborting the run.
- **L2** — non-waivable findings are not protected from engine-level resolution (see M2.6).
- **L3** — truncation honesty: `teacher_messages` stops at `MAX_TEACHER_MESSAGES = 12`
  with no "… N more findings" indicator; the production path pre-caps question payloads
  below the `C5-QUESTIONS-CAPPED` threshold so that finding can never fire there.
- **L4** — the scheme revision hashes `{maxima, answers}` only; `question_numbers`
  changes findings without changing the revision, so panel and cached record can share a
  revision while showing different message sets (attestation precision only).
- **L5** — the strict AST import allow-list covers engine A only; the root engine is
  guarded by a substring scan that `import socket` would pass. Reuse the AST pattern.
- **L6** — legacy investigation reviewer (A) limitations, kept as reference only:
  unbounded `ReviewCache` (no eviction cap), cached provider results not re-validated on
  hit, `C5-ALTERNATIVE-MAX-MISMATCH` reports only the first divergent group, abort-on-
  negative-marks exception path, dead constants (`REVIEW_STATUSES`,
  `PROVIDER_RESPONSE_SCHEMA`, `_canonical_question_id`).
