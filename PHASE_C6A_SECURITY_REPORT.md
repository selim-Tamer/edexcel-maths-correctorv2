# Phase C6-A — Teacher Approval Authorization and Revision Validation

## 1. Baseline and scope

Reviewed and implemented on `main` commit
`c8b1884c49865fe12c3aa142c4fd508b24288d17` (merged PR #43, following C5
consolidation PR #42). Rechecked the remote main SHA before preparing this
report. Work is isolated in branch `security/c6a-teacher-approval-hardening`;
pre-existing local dependency/C5 work was not changed. Root `AGENTS.md` was
read. No production app, marking controller, dependency manifest, provider
configuration or Classroom code was edited.

This hardens internal review APIs. It does **not** activate an approval UI,
add a mandatory marking gate, or authenticate teachers by itself.

## 2. Reproduced M2 findings

Synthetic calls to the original engine demonstrated:

| Finding | Baseline behavior | Correction |
|---|---|---|
| M2-A | `resolve_findings(record, ids, "")` marks findings resolved | Every privileged operation requires a currently valid, server-issued context |
| M2-B | Approval with `wrong-revision` writes `TEACHER_APPROVED` | Exact record/revision equality required before decision mutation |
| M2-C | `approval_state` returns `stale_revision`, while summary still says `TEACHER_APPROVED` | Status and summary use the same authoritative evaluation; no context means no effective approval |
| M2-D | Blank-identity resolution clears a synthetic verified `TOTAL_MISMATCH`; approval then succeeds | Structural/evidence/unsupported findings cannot be waived; correction and a fresh revalidated review are required |

Baseline backend execution: **500 tests passed**. Reproduction altered only
synthetic in-memory records, not stored results.

## 3. Root causes

The legacy write APIs trusted caller-supplied strings. Revision checking lived
only in a separate read helper, and summary/status trusted stored flags. The
resolution API treated every requested finding ID as waivable without evidence,
reason, ownership or individual policy. The original revision hash covered the
interpretation, not the complete evidence/mapping/resolution snapshot.

## 4. Security design and identity contract

`scheme_authorization.ApprovalAuthority` is an explicit **trusted server-code
boundary**, not a browser endpoint or an authentication mechanism. Its verifier
is invoked on issuance and every privileged operation/approval read. It must
verify authentication, teacher role/permission and access to the requested
assignment, returning `ServerIdentity(owner, session_nonce, assignment_revision)`
or denying access. Assignment scopes require a current server-verified revision;
manual scopes require `assignment_revision=None`.
Exceptions become a fixed, content-free failure category.

Only contexts actually issued by that authority are accepted. Raw strings,
dictionaries and separately constructed context objects are rejected. Owner
and login-session identifiers must be bounded nonblank strings without
whitespace/control characters. A fresh login requires a fresh server nonce.
The nonce remains in server memory, not the serializable review.

An authority is session-local, bounded to 100 contexts and 100 review entries.
`revoke()` clears both registries and permanently disables that authority.
The verifier also detects logout, account changes and changed authorization
without relying solely on cleanup.

The engine cannot make a dishonest verifier honest. Trusted Python code can
construct an authority or mutate private state; this is not a sandbox against
arbitrary code execution. No future endpoint may construct a verifier from
browser-supplied identities. An authenticated Google subject alone does not
prove teacher authorization.

## 5. Review/approval binding contract

The new `bind_scheme_review` registers a fresh review only after reconstructing
its deterministic findings and extracted content from retained input data.
Cleared/edited findings cannot be registered as if revalidation passed.

Required bindings:

- Server-verified owner and login session.
- Exact assignment ID and server-verified assignment revision, when scoped to
  an assignment. A changed revision invalidates even an unchanged local record
  through the live verifier. An explicitly authorized
  `None` scope supports manual review; it is not a fabricated assignment ID.
- Explicit fresh review-record ID, actual revision, and lowercase SHA-256
  document digest supplied by trusted server document storage.
- A canonical, bounded snapshot of **every record field except decision
  metadata**, including complete interpretation inputs, document evidence,
  question mapping/inventory, extracted criteria, verified findings, assisted
  outcome, binding metadata and individual resolutions.
- Separately held server decision metadata, checked against the record.

No required IDs/digests are invented by the engine. Missing source context,
maxima-only records, malformed/non-finite snapshots, duplicate registration IDs
and unexpected record mutations fail closed. The server must derive the PDF
digest itself: a well-formed hexadecimal string does not prove PDF provenance.

Full-input retention is bounded to 400,000 JSON characters. Inputs outside that
contract retain read-only review behavior but cannot acquire approval. Complete
snapshots are bounded to 1,200,000 characters. The registry stores digests and
small authorization/decision metadata, not an additional copy of PDF text.

Any change to content, evidence, findings, resolutions, owner, assignment or
review identity invalidates effective approval. Rebuilding requires a fresh
review identity. Exact-content copies within the same authorized session are
equivalent snapshots; cross-session/assignment copies are not authorization.
Repeated approval of the unchanged valid snapshot is idempotent. Rejection
requires regeneration before approval.

## 6. Authoritative status and separate operations

`approval_state` validates the live context, server registry, exact snapshot and
decision. `determine_status` and `review_summary` reuse it. Stored approval
metadata without valid authorization never displays as effective approval.
Summary exposes a historical-metadata indicator, separately from its status.

Warning acknowledgement, finding resolution, scheme approval, student-result
approval and grade publication remain distinct. Scheme approval changes only
the review's teacher decision metadata and session registry. It has no
publication or model-call path.

## 7. Hard-blocker and resolution policy

All unresolved findings prevent privileged scheme approval. The existing C5
classification of structural hard blockers is preserved, including its
alternative-method reconciliation behavior.

The sole resolution allowlist entry is deterministic `C5-MAPPING-UNCERTAIN`
with category `QUESTION_MAPPING_UNCERTAIN` and state `evidence_insufficient`.
It permits a teacher to confirm a heuristic page mapping without rewriting
criteria. Resolution requires exactly one known finding ID, a bounded nonblank
attributable reason, the bound document digest, an integer page number, and an
excerpt that occurs in the extracted source page and exactly matches a cited
criterion on that question's mapped page.

All structural contradictions, missing/conflicting evidence, invalid or
unsupported criteria, alternative-method disputes and unknown rules require
correction and revalidation. Provider suggestions/failures cannot be certified.
There is no blanket waiver or checkbox-to-resolution path. Resolution alone
does not approve a scheme; changing a stored resolution invalidates approval.
Text containment establishes reference consistency, not independent proof of
the mathematical correctness of a teacher's interpretation.

## 8. Files changed

- `scheme_authorization.py`: server-issued context and revocable session registry.
- `scheme_review.py`: bound registration, privileged operation checks, exact
  snapshot verification, resolution policy and shared approval-status evaluation.
- `tests/test_scheme_authorization.py`: 43 targeted adversarial test methods,
  with additional mutation/identity/evidence subcases.
- `tests/test_scheme_review.py`: migrate existing approval tests to the secure
  context contract; replace the unsafe missing-evidence waiver expectation with
  a rejection assertion while retaining blocker/dependency checks.
- `tests/ui/test_streamlit_scheme_review.py`: exercise the actual checkbox and
  confirm warnings/blockers remain, without creating result approvals/overrides.
- This report.

## 9. Adversarial verification

Real engine tests cover anonymous/blank/malformed identities; forged contexts;
foreign teachers; missing or mismatched assignment scope; revision mismatch;
stale summaries; changes to raw/normalized scheme, evidence, mappings, inventory,
findings, resolutions, owner, digest, review ID and decision; missing metadata;
cross-session replay; fresh review identity enforcement; hard blockers;
blanket/unknown finding IDs; missing/wrong resolution evidence or reason;
individual legitimate resolution; metadata-only fake approval; logout/revocation;
account switching and same-account fresh login; idempotence; rejection;
corrupted/non-finite records; bounded registry; and read-only maxima warnings.

Existing Streamlit/auth/isolation/override/Classroom tests remain in the full
suites. The new UI case verifies that checking the real acknowledgement box
does not clear a hard blocker, approve the scheme, create a Classroom grade
approval or alter overrides. Publication restrictions are additionally exercised
by the unchanged backend/UI Classroom tests; no real publishing call was made.

## 10. Full offline regression results

Local Windows Python **3.14.4**, Streamlit **1.66.1**, google-genai **2.30.1**,
pypdf **6.21.2**, Pillow **13.0.0**:

| Suite | Passed |
|---|---:|
| Backend (`unittest discover -s tests`) | 543 |
| Streamlit (`unittest discover -s tests/ui`) | 40 |
| SDK intercepted HTTP (`unittest discover -s tests/sdk`) | 14 |
| Investigation (`unittest discover -s investigation`) | 640 |
| **Distinct test methods across suites** | **1,237** |

This includes C2/C3/C4/C5, security, PDF parser isolation, authentication,
student isolation, overrides and Classroom restrictions. The focused approval
and C5 suite passed **94** tests (included above, not added twice). Application,
changed modules/tests and UI test compile successfully; diff whitespace check
passes. No new dependencies or provider requests were introduced.

During validation the unchanged investigation pilot configuration test failed
under the separately installed SDK 1.40.0 because `thinking_level` is unsupported
there. The full investigation suite passes under SDK 2.30.1. This is a pre-existing
offline pilot SDK requirement, not a production configuration change. Initial
C5 differential failures exposed an overly broad change to the hard-blocker
helper; its original classification was restored, and all 640 tests then passed.

CI results are recorded in the PR/final review once the draft is created; this
report does not pre-claim unexecuted remote checks. The existing CI matrix also
checks Python 3.11 and SDK 1.40.0 for the production UI/SDK compatibility paths.

## 11. Benchmarks

All **10 existing offline benchmarks** exited successfully: document pipeline,
local processing, PDF inspection, parser deadline, marking requests, service
recovery, automatic recovery, provider deadline, Gemini limits and scheme review.
They use synthetic files, local CPU measurements and mocked provider timing.
No live latency or marking-accuracy claim follows from these results.

Additional same-process baseline/current comparison, median of 101 synthetic
runs on this workstation:

| Operation | Main baseline | C6-A |
|---|---:|---:|
| Full 20-question/60-criterion review with evidence | 2.2634 ms | 2.6003 ms |
| Current production maxima-only review | 0.1540 ms | 0.1606 ms |

New single-question approval repeat: **0.1260 ms**; authoritative status read:
**0.1033 ms**. Full review has a small local overhead from preserving complete
inputs; the production maxima-only path is unchanged and its small timing
difference is not evidence of a meaningful regression or speedup. No additional
Gemini calls occur. Existing parser benchmark still terminates an over-budget
worker and recovers; PDF inspection retains rendered pixels in synthetic cases.

## 12. Deferred C5 findings

| Finding | C6-A disposition |
|---|---|
| L1 float/NaN/infinite maxima | Privileged registration rejects non-exact allocation representations and non-finite snapshots. Legacy read-only normalization remains deferred; e.g. 1.9 can still display as 1, but cannot be approved. |
| L2 blocker resolution | Fixed with a narrow individual resolution allowlist and correction/revalidation for all other findings. |
| L3 truncated finding/message counts | Deferred; existing bounded UI presentation unchanged. |
| L4 revision input coverage | Full privileged snapshot includes raw inputs, document evidence, mapping, inventory, bindings and resolutions. Legacy warning revision semantics remain unchanged. |
| L5 AST import-boundary enforcement | Deferred broader enforcement; inspected new imports contain only local/standard-library authorization support, no provider dependency. |
| L6 legacy investigation reviewer | Unchanged and isolated; production still uses root `scheme_review.py`. |

## 13. Self-review, remaining risks and compatibility

- Every privileged entrypoint (including rejection) and effective approval read
  checks the live server context. There are no production callers of these APIs;
  existing warning/acknowledgement UI uses `review_summary` without authorization.
- Registry state, not editable record flags, is the authority. Snapshot and
  decision checks detect mutable-record reuse and stale approval replay.
- Existing passing tests were adapted to the explicit new contract, not made
  to bypass authorization. The formerly unsafe waiver expectation is reversed.
- Caller-string compatibility is intentionally broken for privileged APIs.
  Read-only review/summary calls remain supported.
- This is session-local groundwork, not a durable approval/audit database.
  Restarts lose authorization and effective approvals, which fail closed.
- Operations assume serialized server-side session access, matching the current
  Streamlit workflow. A future shared/multithreaded or multiworker approval
  service must add transactional storage/locking rather than share this mutable
  authority across concurrent writers.
- SHA-256 bindings detect change, not source authenticity. C6-B must establish
  genuine document provenance and assignment ownership before registration.
- No security guarantee against arbitrary trusted-server-code execution is
  claimed. Synthetic passing tests do not establish real Edexcel accuracy.

## 14. C6-B integration requirements

1. Supply a server verifier that checks authenticated identity **and teacher
   authorization**, current assignment access and revision, and a fresh
   per-login nonce.
   Do not use a browser teacher ID or OAuth subject alone as authorization.
2. Own the authority in sensitive session state; revoke/drop it on logout or
   account switching. Never serialize contexts/registries to a client or log them.
3. Derive the actual PDF digest and fresh review ID from trusted storage; bind
   only revalidated source records. Do not turn current maxima-only warnings
   into full approval records by inventing metadata.
4. Keep warning acknowledgement, evidenced resolution, scheme approval,
   student-result approval and publication distinct in labels and handlers.
5. Pass the live context to every approval display; missing context must remain
   read-only. Do not render `record['teacher']['status']` as effective approval.
6. Regenerate/revalidate after criteria/evidence/assignment changes; preserve
   historical decisions separately if durable audit history is later required.
7. Add transactional authorization/audit design before multiworker persistence,
   and test the actual role-verifier integration before enabling approval UI.

## 15. Merge recommendation

Prepare a **draft PR for security review**, subject to passing GitHub Actions
and human approval. The reproduced M2 engine defects are addressed offline;
current production warnings, overrides, marking and publishing behavior remain
unchanged. C6-B authentication/role integration and durable approval UX are
explicit follow-up work, not features claimed complete here. Do not merge,
deploy or enable a new approval interface automatically.
