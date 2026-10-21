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
| Consolidation commit | *(filled at commit time)* |

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
| Teacher approval | Owner+assignment+revision+digest-bound; hard blockers never acknowledged away; `resolve_findings` | Owner+revision-bound; `resolve_findings` then `approve_scheme_review`; blocking set explicit |
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
   (`resolve_findings` → `approve_scheme_review` → `approval_state`), owner-bound.
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
2. **C2 total-mismatch traceability.** When a labelled alternative structure legitimately
   explains a criteria-sum ≠ total situation, the mapped C2 `SCHEME-TOTAL-MISMATCH` /
   `C5-TOTAL-MISMATCH` finding is retained with `evidence.explained_by_alternatives=true`
   and excluded from the blocking set — preserving the original finding for traceability
   instead of erasing it for a cleaner UI.
3. **Never abort on a single bad criterion.** (Already B behaviour; locked in by oracle.)
   A rejects negative marks with an exception; B represents them as `C5-MARKS-NEGATIVE`
   and keeps reviewing. B's represent-don't-abort model is kept and pinned.

New/updated surface in `scheme_review.py`:
- `alternative_option` field accepted in criterion normalisation (bounded string).
- `_normalize_alternative_groups` / `_alternative_effective_total` helpers.
- `check_question_totals(..., flat=...)` extended with branch-max reconciliation and
  `explained_by_alternatives` accounting.
- `_blocking_findings` no longer counts an explained-by-alternatives C2 total mismatch.

`C5-ALT-SUM-REVIEW` remains acknowledgement-grade (non-blocking, escalated). Confirmed by
direct probe during differential authoring: the finding forces
`NEEDS_TEACHER_REVIEW` but is not in the blocking set.

---

## 5. Features preserved from each implementation

**From DeepSeek (A):** alternative-method branch-max reconciliation and its C2
traceability; the "never abort on malformed single field" principle contrasted against A's
own exception path (we keep B's stronger represent-don't-abort); strict per-finding
provider hallucination validation as the reference model for the assisted channel.

**From GLM (B):** the authoritative `app.scheme_review/1` contract; production integration
(`prepare_reference_metadata` caching, warning channel, `render_scheme_review_panel`);
owner- and revision-bound approval lifecycle (`resolve_findings`, `approve_scheme_review`,
`approval_state`); sensitive-state ownership and logout clearing; the disabled-by-default
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
  no live request path exists. Owner-scoped caching and per-request budgets are present
  but unexercised until C6.
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
| Backend | `python -m unittest discover -s tests -p 'test_*.py'` | **493 OK** (baseline 493) |
| UI | `python -m unittest discover -s tests/ui -p 'test_*.py'` | **39 OK** (baseline 39) |
| SDK | `python -m unittest discover -s tests/sdk -p 'test_*.py'` | **14 OK** (baseline 14) |
| Investigation | `python -m unittest discover -s investigation -p 'test_*.py'` | **640 OK** (baseline 629 + 11 new) |
| Compile | `python -m py_compile app5_1_automated.py scheme_review.py` | exit 0 |

No pre-existing test changed; the only additions are the 11 consolidation-gate tests.

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

Verified covered by existing tests — all green, none weakened by this change:

- Anonymous review never approves nor calls a provider:
  `tests/test_c5_mark_scheme_reviewer.py::test_anonymous_review_neither_calls_provider_nor_approves`,
  `tests/test_scheme_review.py::test_anonymous_identifiers_cannot_approve`,
  `tests/ui/test_streamlit_scheme_review.py::test_findings_render_and_anonymous_cannot_acknowledge`.
- Foreign teacher cannot acknowledge: `tests/ui/test_streamlit_scheme_review.py::test_foreign_teacher_click_never_checks_the_widget`.
- Stale revision invalidates approval: `tests/test_scheme_review.py::test_stale_revision_invalidates_approval`.
- Owner isolation / fail-closed identity: `tests/ui/test_student_data_isolation.py`,
  `tests/test_authentication_guard.py`, `tests/test_failure_diagnostics.py`.
- Provider failure content-free: `tests/test_c5_mark_scheme_reviewer.py::test_provider_failure_never_leaks_its_message`.
- Malformed `alternative_option` input fails closed: `_normalize_criterion` coerces
  non-string/empty/overlong labels to `None`, so branch labels are always
  `str | None` (hashable) and any malformed label escalates to teacher review —
  never summed, never crashing the reviewer.

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
