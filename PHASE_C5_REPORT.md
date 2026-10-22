# Phase C5 report — reviewed mark-scheme interpretations

## 1. Summary

Phase C5 adds an offline-first, bounded review layer for AI-extracted Edexcel
4MA1 mark-scheme interpretations. It composes the existing C2 structural engine
and C3 page-evidence/mapping modules, adds deterministic interpretation checks,
reconciles alternative-method totals without summing branches, records
owner-bound teacher decisions, and supports an optional, mockable, single-request
provider review whose output is always a proposal.

The work is **investigation-only**. The production marking path was not
modified, no student data or credentials were used, no live Gemini/Classroom
call was made, and no grade is published. Nothing was merged or deployed.

Branch: `accuracy/c5-mark-scheme-reviewer`
Baseline: `origin/main` at `75f0a9981ee69b0fc6545a2f54e980affbf91c72` (PR #41)

## 2. Scope and non-goals

In scope:

- deterministic review of a proposed mark-scheme interpretation;
- separation of extracted, verified and teacher-approved information;
- validation of citation pages/excerpts against C3 document records;
- question-to-page mapping checks;
- alternative-method and follow-through reconciliation;
- explicit approval blocking and acknowledgement rules;
- optional provider review with strict validation, caching and fail-safe statuses;
- synthetic benchmark coverage and production-boundary regression tests.

Out of scope (deliberately unchanged):

- production extraction, marking, retries, quotas or Classroom upload paths;
- live provider activation or credentials;
- any change to student marks, maxima or grade publication;
- S2/S3/S4 safeguards;
- C6 work;
- claims of accuracy on real Pearson papers.

## 3. Architecture

`investigation/scheme_review.py` reuses rather than replaces earlier phases:

| Layer | Reused capability |
| --- | --- |
| C2 | `scheme_model.parse_scheme` strict model; `verification_engine.verify_scheme_document` structural findings |
| C3 | `pdf_evidence.evidence_reference` / document validation; `pdf_question_map.map_question_pages` |
| C4 | Not imported or modified; C2's `SCHEME-MATH-UNSUPPORTED` passes through unchanged |

The public entry points are `review_scheme(payload, provider=None, cache=None)`,
`build_provider_packet(...)` and `ReviewCache`.

The request contract is `investigation.scheme_review/1`. Unknown top-level keys,
unknown assignment fields, malformed schemes, out-of-range claims and unsupported
schemas fail closed with `SchemeReviewError`; error messages never echo scheme
prose. Both request and result are serialized-size bounded.

### Information separation

The result keeps three channels strictly separate:

- `extracted`: the proposed scheme and per-criterion claims, uncertified;
- `verified`: C2 passthrough, C5 deterministic findings, effective totals,
  reconciliation, evidence references, mapping summary, coverage, hard blockers
  and required acknowledgements;
- `teacher`: reviewer identity, review status, approval validity, binding
  (reviewer, assignment, revision, scheme digest), applied decisions and
  per-criterion/per-question status.

Provider findings appear only under `provider_review`; they are never inserted
into `verified.c5_findings` and never change extracted or verified fields.

## 4. Deterministic checks

C5 adds checks that C2 cannot express without inventing information:

- question identity, empty schemes and questions without criteria;
- unknown claims, missing extraction claims, invalid/noncanonical part ids;
- mark-type conflicts/uncertainty/undeclared types and mark-allocation conflicts;
- follow-through conflicts and orphan alternative-option declarations;
- extraction-confidence unknown/low warnings;
- missing source documents/pages, invalid roles, out-of-range pages;
- unusable text layers (reported as evidence unavailable, never as corruption);
- missing, mismatched or non-verbatim excerpts;
- missing/ambiguous/unresolved mapping and evidence outside the mapped pages;
- heuristic continuation-page reliance;
- total overcount and missing criteria against the reconciled effective total;
- unresolved alternative structures;
- cross-question, forward, cyclic and alternative-branch dependency conflicts;
- dependent-method criteria with no prerequisite;
- follow-through marks with no earlier source or no explicit source;
- assignment/scheme digest mismatch;
- approval blocking, incomplete acknowledgement, unknown acknowledgement,
  duplicate/ambiguous decisions and unknown decision targets.

C2 findings are preserved exactly. When a C2 `SCHEME-TOTAL-MISMATCH` is fully
explained by a resolved alternative-method structure, it is retained in
`c2_findings` but marked `explained_by_alternatives` for approval accounting;
it is not deleted or hidden.

### Alternative methods

`_alternative_structure` groups criteria by `alternative_group` and
`alternative_option`. Each group contributes **the maximum branch total once**;
branches are never summed. If any branch label is missing or a member weight is
unknown, the question's `effective_total` is `None` and
`C5-ALTERNATIVE-STRUCTURE-UNKNOWN` blocks approval. A partial sum is never
reported because it would look more authoritative than the evidence allows.

## 5. Optional provider review

- Disabled by default (`provider=None` → `not_requested`, zero requests).
- Exactly one request per invocation (`MAX_PROVIDER_REQUESTS = 1`).
- Anonymous reviewers, missing mark-scheme evidence, non-callables and packet
  construction failures refuse without calling anything.
- Packet excludes teacher decisions, reviewer identity, owner ids, approvals,
  student data and marks; it contains only proposed criteria, cited excerpts, a
  bounded sample of cited source pages and deterministic findings.
- Packet is bounded by `MAX_PACKET_CHARS = 120000` and page excerpts by
  `MAX_PACKET_PAGE_CHARS = 4000`.
- Responses may be a dict, a JSON string or UTF-8 bytes. Strict validation
  rejects unexpected fields, unknown categories, hallucinated questions or
  criteria, duplicate criterion references, non-verbatim source excerpts,
  overlong text and excessive finding counts.
- Findings are re-identified deterministically from canonical ordering, so the
  same proposal set produces the same ids regardless of provider ordering.
- Every accepted finding is `origin="gemini_proposal"`, `state="suspected_issue"`,
  `blocks_approval=False`, `acknowledgement_required=True`, and `review_required`.
- Statuses cover `completed`, `completed_with_rejections`, `invalid_json`,
  `schema_invalid`, `timeout`, `quota_limited`, `blocked`, `empty_response`,
  `unavailable`, `anonymous_refused`, `unavailable_evidence` and
  `request_limit`.
- Any non-completed status adds `C5-PROVIDER-REVIEW-UNAVAILABLE`; the review
  never assumes the provider finding set is empty.
- `ReviewCache` keys on reviewer, assignment, revision, effective scheme digest
  and packet digest. Cached entries are deep-copied; teacher decisions and
  approvals are never cached.
- Provider exceptions are classified without reading the exception message, so
  secrets or student prose cannot leak into results.

## 6. Teacher decisions and approval

Approval is valid only when:

- the decision owner matches the authenticated reviewer;
- assignment id and revision match;
- the effective scheme digest matches (or both are absent);
- no hard blocker exists;
- every non-hard acknowledgement-required finding is acknowledged;
- every acknowledged id exists in this review.

Hard blockers (missing evidence, unresolved structure, conflicts, unknown
targets, etc.) cannot be acknowledged away. Unknown and duplicate decisions are
reported. Approval binding is returned explicitly so a stale revision or a
different teacher cannot inherit it.

## 7. Benchmark evidence

`investigation/scheme_review_cases.json` contains **26 synthetic cases**:
clean control, missing criterion, total overcount, wrong mark type, omitted
claim, low confidence, noncanonical part, unknown claim, duplicate criteria,
missing source evidence, excerpt mismatch, evidence outside mapping, heuristic
continuation mapping, resolved/unresolved alternatives, branch-max mismatch,
forward dependency, dependency cycle, follow-through without source, blocked
approval, accepted/rejected/timed-out/refused provider cases, revision mismatch
and missing source document.

`python -m investigation.scheme_review_benchmark` results:

```json
{
  "cases": 26,
  "all_matched": true,
  "status_accuracy": 1.0,
  "finding_recall": 1.0,
  "finding_precision": 1.0,
  "false_positives": 0,
  "mapping_correct": 1.0,
  "total_consistency": 1.0,
  "c2_preservation": 1.0,
  "provider": {
    "status_accuracy": 1.0,
    "category_recall": 1.0,
    "category_precision": 1.0,
    "bounded": true
  }
}
```

Group coverage includes clean control, missing criterion, dependency,
alternative, mapping, evidence, approval, provider, escalation,
C2 preservation, totals, claim, part, mark type, duplicate, confidence and
revision. The benchmark is synthetic engineering evidence only; it is not an
accuracy claim about real 4MA1 papers.

All nine pre-existing backend benchmark scripts also exit 0, and
`python -m investigation.math_benchmark` (C4) exits 0.

## 8. Test coverage

The Phase 10 review cases are exercised by 121 investigation tests plus 9
production-boundary tests:

| Suite | Tests | Result |
| --- | ---: | --- |
| `investigation.test_scheme_review` | 78 | OK |
| `investigation.test_scheme_review_gemini` | 35 | OK |
| `investigation.test_scheme_review_benchmark` | 8 | OK |
| `tests/test_c5_mark_scheme_reviewer.py` | 9 | OK |
| **C5 total** | **130** | **OK** |

C5 tests are deterministic and offline. Provider tests use local callables only;
there are no SDK imports, network calls, credentials or student data.

Full regression in an isolated worktree at the PR commit:

| Suite | Tests | Result |
| --- | ---: | --- |
| Backend `discover -s tests` | 434 | OK |
| UI `discover -s tests/ui` | 34 | OK |
| SDK `discover -s tests/sdk` | 14 | OK |
| Investigation `discover -s investigation` | 614 | OK |
| **Total** | **1096** | **OK** |

Baseline totals were 425 / 34 / 14 / 493. The increase is the 9 boundary tests
and 121 investigation tests. The previously observed Windows `.lock`
`PermissionError` in `test_gemini_pilot` did not recur in the isolated run.

`python -m py_compile app5_1_automated.py` and compilation of every new module
pass.

## 9. Performance and cost

Measured on the development machine (CPython 3.14.4, Windows), median of
repeated runs:

| Measurement | Result |
| --- | --- |
| Single clean review | 0.53 ms min / 0.59 ms median / 1.08 ms max (50 runs) |
| 26-case benchmark | 21.2 ms min / 22.0 ms median / 22.7 ms max (10 runs) |
| 25 questions × 10 criteria (250 criteria) | 25.0 ms median |
| Clean provider packet | 2,144 characters |
| Packet cap | 120,000 characters |
| Provider requests per invocation | at most 1 |

Cost:

- The deterministic review makes **zero** external calls; cost is local CPU only.
- The optional provider path makes at most one request per invocation and is
  cached per reviewer/assignment/revision/scheme/packet. A cache hit performs no
  provider call.
- No live calls were made, so no provider billing was incurred in this phase.
- A full request is bounded at 120k characters; result serialization is bounded
  at 2 MB. The practical ceiling is the request cap (roughly 400 criteria),
  comfortably above a normal 4MA1 paper; larger synthetic inputs fail closed
  with a bounded error rather than returning truncated output.

## 10. Security and privacy regression

`tests/test_c5_mark_scheme_reviewer.py` asserts:

- the production app never imports `investigation.scheme_review`;
- `scheme_review.py` imports only `copy`, `hashlib`, `json`, `re` and
  investigation modules;
- no file IO, `eval`/`exec`/`compile`/`__import__`, `getpass`, `input` or
  network modules appear in the reviewer;
- the result contract is fixed and contains no student names, ids, awarded
  marks, final grades or classroom grade fields;
- safeguards are exactly: official scheme authoritative, provider findings are
  proposals, does not publish grades, does not affect student marks, requires
  teacher confirmation, no live provider calls;
- the provider is optional, at most one request, and cannot add criteria,
  change totals or certify a finding;
- provider failures never leak exception text;
- anonymous reviews neither call the provider nor approve anything;
- results round-trip as JSON within the bound.

No S3/S4/S2 weakening occurred: no production files, quota logic, retry logic,
auth/session logic or Classroom paths were changed in this branch.

## 11. Preserved behaviour and compatibility

- C2 findings are passed through as untouched deep copies.
- C2's `SCHEME-TOTAL-MISMATCH` is preserved; only C5 approval accounting can
  treat a resolved alternative-method mismatch as explained.
- C3 mapping/document validation remains authoritative; C5 never repairs pages
  or invents excerpts.
- C4 is untouched and unimported.
- The production app remains byte-identical to the baseline on this branch.
- No new runtime dependency is added.

## 12. Limitations and follow-ups

- C5 is not wired into the production UI or marking pipeline; activation needs a
  separate reviewed change.
- The benchmark is synthetic. Real-paper accuracy requires teacher-approved
  schemes and verified evidence, neither of which is claimed here.
- Provider prompt quality and live latency/cost were not evaluated; only the
  contract, bounds, validation and mocked behaviour are covered.
- Very large synthetic schemes beyond the request cap fail closed. That is
  intentional, but a future UI must surface the bounded error clearly.
- A resolved alternative structure leaves the original C2 total finding in
  `c2_findings`; consumers must use the approval-accounting flags rather than
  treating every C2 line as a blocker.

## 13. Files changed

- `investigation/scheme_review.py` — C5 review core.
- `investigation/test_scheme_review.py` — deterministic/contract/approval tests.
- `investigation/test_scheme_review_gemini.py` — mocked provider tests.
- `investigation/scheme_review_cases.json` — 26 synthetic benchmark cases.
- `investigation/scheme_review_benchmark.py` — benchmark runner and metrics.
- `investigation/test_scheme_review_benchmark.py` — benchmark integrity tests.
- `investigation/README.md` — Stage C5 documentation.
- `tests/test_c5_mark_scheme_reviewer.py` — production-boundary/security tests.
- `PHASE_C5_REPORT.md` — this report.

No production source file is modified.

## 14. Verification commands

```sh
python -m py_compile app5_1_automated.py
python -m unittest investigation.test_scheme_review investigation.test_scheme_review_gemini investigation.test_scheme_review_benchmark -v
python -m unittest discover -s tests -p 'test_*.py' -q
python -m unittest discover -s tests/ui -p 'test_*.py' -q
python -m unittest discover -s tests/sdk -p 'test_*.py' -q
python -m unittest discover -s investigation -p 'test_*.py' -q
python -m investigation.scheme_review_benchmark
python -m investigation.math_benchmark
```

## 15. PR instructions

`gh` is not available in this environment. Create the PR from:

```text
https://github.com/selim-Tamer/edexcel-maths-correctorv2/compare/main...accuracy/c5-mark-scheme-reviewer?expand=1
```

Suggested title:

```text
Phase C5: bounded, offline-first mark-scheme reviewer
```

Suggested description:

```markdown
## Summary

Phase C5 adds an investigation-only reviewer for AI-extracted Edexcel 4MA1
mark-scheme interpretations. It composes C2 structural verification and C3
page evidence/mapping, adds deterministic interpretation checks, reconciles
alternative-method totals without summing branches, and supports an optional,
mockable provider review bounded to one request.

The official mark scheme remains authoritative. Extracted, verified and
teacher-approved information stay separate. Provider findings are proposals
only; they cannot add/remove criteria, change totals, certify a finding or
approve anything.

## Safety

- No production marking, Classroom, quota, auth or grade path changed.
- No student data, credentials or live provider calls.
- Deterministic checks run before and independently of any provider review.
- Missing evidence and unresolved structures block approval; they are never
  filled by inference.
- Approval is bound to reviewer, assignment, revision and scheme digest.

## Evidence

- 26-case synthetic benchmark: all matched, 1.0 status accuracy,
  1.0 finding recall/precision, 0 false positives, 1.0 C2 preservation.
- C5 tests: 130 passing (78 + 35 + 8 + 9).
- Full regression: 434 backend, 34 UI, 14 SDK, 614 investigation — all passing.
- All nine pre-existing benchmarks plus C4 math benchmark exit 0.
- Single clean review median 0.59 ms; 250-criterion synthetic review ~25 ms;
  provider packet 2,144 chars against a 120,000-char cap; at most one request.

## Not included

No merge, deployment, live provider call, C6 work or real-paper accuracy claim.
See `PHASE_C5_REPORT.md` for the full review, performance/cost analysis,
security regression and limitations.
```

Do not merge or deploy. Teacher/user approval is required before any
production activation.

## 16. Checklist

- [x] Baseline PR #41 commit recorded.
- [x] Branch created from baseline.
- [x] C2/C3 reuse, not replacement.
- [x] Extracted/verified/teacher states separated.
- [x] Official scheme authoritative.
- [x] Deterministic validation before provider review.
- [x] Provider optional, single-request, cached, fail-safe.
- [x] No student data, credentials, live calls, grade publication or S2/S3/S4 weakening.
- [x] Synthetic/offline tests for Phase 10 cases.
- [x] Benchmark evidence and performance/cost review.
- [x] Security regression and full test counts.
- [x] Report, commit, push and compare URL.
- [ ] Merge/deploy — intentionally not done.
