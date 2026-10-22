# PHASE C6-B1 SECURITY REPORT — Secure Teacher Authorization, Session Lifecycle and C6-A Follow-Ups

Status: **IMPLEMENTED — DRAFT FOR INDEPENDENT SECURITY REVIEW. NOT MERGED, NOT DEPLOYED.**
No approval UI is activated. No marking, override, publication or provider behavior changed.

---

## 1. Git baseline and branch

| Item | Value |
|---|---|
| Repository | https://github.com/selim-Tamer/edexcel-maths-correctorv2 |
| Baseline | `bf227572677ef352002365c7685bf10187f114ec` — "Merge pull request #44" (verified merged into `origin/main`) |
| Branch | `security/c6b1-teacher-authorization-integration` (created from `origin/main` at baseline) |
| Local repository | `C:\Users\user\Documents\Default Project\edexcel-maths-correctorv2` |
| Prior C6-B1 work | Searched for `teacher_authorization.py`, `PHASE_C6B1_SECURITY_REPORT.md`, `tests/test_teacher_authorization.py`, commit prefix `5162a01`, and any `c6b1` branch locally/remotely — **none existed**; implemented fresh |

## 2. Current authentication architecture (as verified by inspection)

- Google OIDC login is Streamlit's built-in auth (`st.login("google")` / `st.logout()`), configured via `[auth]` secrets; the server-side identity is `st.user` (subject `st.user.sub`), consumed only through the app's guard `authenticated_google_identity()`.
- `google_logged_in()` reads `st.user.is_logged_in` inside try/except (fail closed).
- Sensitive session state is owner-bound: `sensitive_state_owner()` derives `"google:" + sub`; `reconcile_sensitive_state()` runs before any sensitive read on every rerun and destroys state on owner mismatch; `invalidate_sensitive_state()` runs on explicit logout (and mid-run identity transitions).
- Classroom flows reuse the OIDC access token (`st.user.tokens["access"]`) for `list_teacher_courses` (`teacherId=me`, ACTIVE), `list_assignments` (PUBLISHED, ASSIGNMENT only) and submission listing. Google's API itself enforces that these calls succeed only with genuine teacher membership.
- The only pre-existing "approval-like" UI is the C5 scheme-review **acknowledgement checkbox** (`save_scheme_review_acknowledgement`), which is deliberately not an approval and remains untouched.
- Streamlit provides **no reliable login-event callback**; silent OIDC cookie refresh is indistinguishable from an ongoing session (see §14).

## 3. Teacher identity trust boundary (implemented)

New module `teacher_authorization.py` is the adapter between the authenticated app state and C6-A's `ApprovalAuthority`:

- Every entry point takes `current_subject` from trusted application code (production: `authenticated_google_identity()`). No widget, URL parameter, header, or client payload is ever read. The verifier closure re-checks, on **every** `authorize`/`validate` call, that the stored subject and nonce still match the values captured at authority creation.
- A Google login is never treated as proof of teacher permission. Assignment authorization requires a grant recorded from a trusted server-fetched listing (§5); manual authorization requires a server-computed digest of actual uploaded bytes (§7).
- Anonymous, missing or malformed identities return `None`/raise (`fail closed`). `identifier()` (hardened, §8-F1) validates every identity string crossing the boundary.
- No token, email, name or provider object is stored in the authorization state — only the OIDC subject (already server-side), a random nonce, timestamps, and grant metadata (IDs, revision hashes).

## 4. Session nonce lifecycle (implemented)

- `current_teacher_session(state, subject)` owns exactly one `ApprovalAuthority` per authenticated (subject, login session) inside the server-side Streamlit session state (`teacher_authorization_state`, added to `SENSITIVE_STATE_KEYS`).
- Fresh nonce per authority: `secrets.token_urlsafe(32)`; a tamper-evident sentinel (`authority._c6b1_session_nonce`) binds the authority object to its state entry.
- Rotation/revocation occurs on every **observable** transition:
  - explicit logout → `invalidate_sensitive_state()` → `revoke_teacher_authorization()` (authority revoked — every issued context dies — then state dropped);
  - account switch (subject change) → previous authority revoked, fresh authority with fresh nonce and **empty** grants;
  - anonymous replacement / expired session / malformed state → same destruction path;
  - session hard age limit `SESSION_MAX_AGE_SECONDS` (12 h) → next access mints a fresh authority; old privileges are never restored.
- Reruns are idempotent: same subject → same authority, same nonce, same context objects (test-verified over 150 reruns).
- Contexts cannot leak between Streamlit sessions: they exist only inside one session's `st.session_state`; authorities are never serialized, logged, or rendered.

**Documented limitation:** a silent OIDC refresh of an unchanged authenticated session is not observable, so the nonce does **not** rotate at cookie-refresh boundaries. The nonce lifetime equals the server-observed authenticated lifetime for that subject. No guaranteed rotation is claimed for events the application cannot see.

## 5. Course permission verification (implemented, synthetically tested)

- `record_course_grant()` records a grant only when the course ID is present in the **trusted** `list_teacher_courses(token)` response (teacherId=me, made with the authenticated teacher's own token). Absent/malformed listings grant nothing.
- Recording happens in the app right after the existing listing succeeds (`record_classroom_grants` best-effort wrapper; failures are swallowed and can only fail closed).
- A course grant alone **never** authorizes any assignment (tested).
- Grants carry `verified_at` and stop re-verifying after `COURSE_GRANT_MAX_AGE_SECONDS` (30 min) until a fresh trusted listing refreshes them; the app re-fetches listings on every Classroom rerun, so the trust window is refreshed by existing traffic, never by cache alone.

## 6. Assignment revision binding (implemented, synthetically tested)

- `record_assignment_grant()` requires: verified course (§5) + the coursework present, with `workType == ASSIGNMENT`, in the trusted `list_assignments(token, course_id)` response for **that** course. The composite identifier `course_id/coursework_id` is minted by trusted code; scheme reviews must use it.
- Google Classroom courseWork exposes **no provider revision ID**, so the revision is `sha256(canonical JSON)` over the documented field set: `course_id` (trusted, not the metadata echo) plus `id, title, description, maxPoints, workType, state, creationTime, updateTime, dueDate, dueTime, materials`.
  - **Covered:** any edit to those fields; because every courseWork update bumps `updateTime`, ordinary edits are detected even if Google adds fields later.
  - **Can escape detection:** hypothetical provider-side changes that alter neither the covered fields nor `updateTime` (none observed; documented, not claimed away). Submission-level changes are tracked separately by the existing submission-revision fingerprinting and are out of this scope.
- Revision changes invalidate previously issued contexts (`session_or_owner_changed`) and supersede bound reviews; reverting metadata re-opens a **new** context but the pruned one stays permanently dead (tested).
- Values are taken verbatim from the provider response; revisions are never fabricated. Missing `updateTime` etc. still yield a deterministic hash over what the provider actually returned.
- Cap: `MAX_ASSIGNMENT_GRANTS = 200`, `MAX_COURSE_GRANTS = 100` (fail-closed refusal when full; a fresh login starts empty).

## 7. Manual PDF ownership and digest provenance (implemented, synthetically tested)

- `record_manual_pdf_grant(state, subject, pdf_bytes)` accepts **bytes only** — a caller-supplied digest string is refused (`pdf_bytes_required`); the SHA-256 is computed over the actual uploaded bytes and bound to the authenticated owner. Size bounded by `document_limits.MAX_DOCUMENT_BYTES` (25 MiB default); the app's existing PDF security checks are unchanged and run before this point in the flow.
- The app registers the grant in the manual-marking store path (`remember_manual_upload(mark_scheme_pdf.getvalue())`) — the same bytes already digested for `last_manual_context`; one extra hash per completed marking run, never per rerun.
- `bind_manual_review()` is the sanctioned binding path: digest from actual bytes → grant owned by the current subject → manual-scope context from the session authority → C6-A `bind_scheme_review`. A review bound by teacher A can never be approved from teacher B's session (tested; B's authority has never registered it).
- Changed contents produce a different digest, which cannot ride on the old binding (snapshot/binding mismatch → refused); re-binding the changed document goes through provenance again (tested). Cap: `MAX_MANUAL_GRANTS = 50` with oldest-entry eviction (removal = permanently invalid, fail-closed direction).

## 8. C6-A review findings (F1–F3) — implemented

### F1 — Unicode identifier validation (`scheme_authorization.py`)
`identifier()` now rejects Unicode categories `Cc` (control), `Cf` (format/zero-width/bidi), `Cs` (surrogate), `Co` (private-use), `Cn` (unassigned), in addition to the previous whitespace/ASCII/length/strip checks. Legitimate non-ASCII identities (accented Latin, CJK, Cyrillic, Greek) remain valid. Tests cover zero-width space/joiner, bidi override/marks, NBSP, line separator, BOM, control chars, private-use, surrogates and unassigned code points (`U+0378`), plus embedded-spoof variants and legitimate non-ASCII names. "Unassigned" tracks the runtime's Unicode database — a fail-closed direction across Python versions.

### F2 — Exception handling (`scheme_review.py`)
`SchemeReviewApprovalError` subclasses `ValueError`; inside `_snapshot()` and `bind_scheme_review()`'s revalidation block the broad `except (TypeError, ValueError, ...)` handlers swallowed intentional refusals and re-raised them under the wrong diagnostic code. Both handlers now re-raise `SchemeReviewApprovalError` unchanged; genuinely corrupted records (KeyError/TypeError/ValueError/RecursionError, plus AttributeError from corrupted nested structures) still fail closed under the generic codes. Verified codes: `exact_mark_allocations_required` (fractional/boolean/container/null marks), `full_review_required`, `review_snapshot_too_large`, `invalid_review_record` (non-finite values via `allow_nan=False`), `review_revalidation_failed` (corrupted records). Fail-closed behavior is preserved everywhere; only diagnostics improved.

### F3 — Authorization context exhaustion (`scheme_authorization.py`, one hook in `scheme_review.py`)
Confirmed: stale contexts/reviews were never removed short of `revoke()`, so revision churn or grant loss could permanently exhaust the 100-entry registries. `ApprovalAuthority.prune_superseded()` drops any context/review whose stored `ServerIdentity` no longer equals a fresh verification of the same scope. It runs on every new-context `authorize()`, on `validate()` failures, and in `bind_scheme_review()` when the review registry is at capacity. Safety: removal is permanent (membership checks mean a pruned entry can never validate again — no revival, no silent grant); only entries that **cannot** re-verify are evicted; currently valid entries are untouched; the hard 100-entry limits remain as the final fail-closed bound; duplicate contexts are never created on reruns (reuse fast-path precedes pruning). Tested with 300 revision churns, 105 assignment grants against the hard cap, and 105 review bindings under identity churn.

## 9. Files modified

| File | Change |
|---|---|
| `teacher_authorization.py` | **NEW** — trusted teacher authorization adapter (stdlib + existing repo modules only) |
| `scheme_authorization.py` | F1 identifier hardening; F3 `prune_superseded()`, `_current_identity()`, prune hooks in `authorize`/`validate` |
| `scheme_review.py` | F2 narrow exception-handling fix (2 sites); F3 review-registry prune hook before the capacity check |
| `app5_1_automated.py` | +`import teacher_authorization`; `teacher_authorization_state` added to `SENSITIVE_STATE_KEYS`; `invalidate_sensitive_state()` revokes the authority first; new best-effort wrappers `record_classroom_grants()` / `remember_manual_upload()` with owner binding; 3 call sites after existing trusted listings/store (zero new provider calls). No UI, marking, override, or publication change |
| `tests/test_teacher_authorization.py` | **NEW** — 50 adversarial tests (§10) |
| `tests/support.py` | Headless loader exposes the real `teacher_authorization` module (one line + import) |
| `PHASE_C6B1_SECURITY_REPORT.md` | **NEW** — this report |

Deliberately unchanged: Gemini models/prompts, marking algorithms, C2–C5 verification logic (except the narrow F2 diagnostic fix), teacher overrides, Classroom publishing, PDF security limits, credentials, hosting, React frontend, dependency versions.

## 10. New security tests (`tests/test_teacher_authorization.py`, 50 tests)

Real adapter + real C6-A authority + real review engine; only the provider boundary, clock and session mapping are synthetic. Covers all 25 mandated adversarial cases:

1. anonymous session rejected · 2. missing teacher identity rejected · 3. invalid identity rejected · 4. teacher permission missing · 5. browser-supplied identity gains nothing (account-switch semantics, empty start) · 6. foreign teacher context rejected · 7. foreign assignment rejected · 8. assignment revision mismatch rejected · 9. course ownership enforced · 10. invalid/malformed course listings rejected · 11. manual PDF ownership enforced across teachers · 12. changed PDF digest cannot ride the old binding · 13. logout revokes capability · 14. account switch revokes capability · 15. re-login mints a fresh nonce · 16. old context permanently unusable · 17. reruns idempotent (150×, zero capacity use) · 18. malformed session state fails closed (tamper + replacement) · 19. Unicode spoofing rejected (13 hostile cases + 6 legitimate non-ASCII) · 20. fractional marks fail closed with `exact_mark_allocations_required` · 21. non-finite marks/records fail closed · 22. capacity stays bounded (300 churns) yet the hard cap still refuses · 23. stale contexts never regain authorization even after revision revert · 24. acknowledgement ≠ approval · 25. no new approval UI/endpoint (AST scan of app calls; import-surface scan of the adapter).

Plus: repeated manual uploads (bounded), assignment metadata changes, grant/session expiry (injected clock), partial/failed Classroom listings, missing provider metadata, multiple distinct assignments, review-record mutation after approval, superseded-review capacity recovery, app wiring (logout/switch/anonymous reconciliation purges, same-teacher rerun survival, owner-bound sensitive key).

## 11. Total offline regression results (all executed locally, this machine)

| Suite | Result |
|---|---|
| `python -m py_compile` (app + all touched modules) | PASS |
| `unittest discover -s tests -p 'test_*.py'` | **593/593 OK** (C6-A baseline 543 + 50 new) |
| `unittest discover -s tests/ui` (real Streamlit AppTest) | **40/40 OK** |
| `unittest discover -s tests/sdk` (intercepted SDK HTTP) | **14/14 OK** |
| `investigation.test_accuracy_regression` + `test_scheme_review_eval` + `test_c5_consolidation_differential` (CI set) | **54/54 OK** |
| Full `investigation` package (all 19 modules) | **640/640 OK** |
| **Total passing tests** | **1,287** (no skips added, no assertions weakened, no live calls) |

C2–C5 marking regressions, S2–S6 security regressions (`test_post_s6_cleanup`, `test_authentication_guard`, `test_student_data_isolation`, `test_supply_chain`, …), teacher-override and PDF-safety suites are all inside the 593/40 counts and pass unmodified.

## 12. Benchmark results

All 10 offline benchmarks PASS (exit 0): document pipeline, local processing, PDF inspection, PDF parser deadline, marking requests, service recovery, automatic recovery, deadline recovery, gemini rate-limit gate, scheme review.

Authorization performance (synthetic, in-memory, measured on this machine):
- repeat `authorize` (cached grant): **~0.1 µs/call** (1,000 calls ≈ 97 ms incl. loop overhead)
- repeat `approval_state`: **~0.2 µs/call**
- repeat `current_teacher_session` (rerun path): **~0.2 µs/call** — no PDF parsing, hashing or verification cost on reruns; expensive work (digest, revision hash) happens only at upload/listing events.

## 13. Provider-call impact

**Zero.** C6-B1 adds no network call anywhere: the adapter imports no HTTP library (test-enforced), grant recording consumes the listings the app already fetches, and the manual digest reuses bytes already read. SDK interception tests confirm no extra requests. Dependency audit (`pip-audit --strict`): production `requirements.txt` and `tests/pdf-requirements.txt` both **clean** (exit 0); no dependency changed.

`git diff --check` note: on this Windows git configuration (`core.autocrlf=true`, no attributes), every added line of the historically-CRLF `app5_1_automated.py` is reported as "trailing whitespace" — identical to all historical commits to that file (verified on merged commits `3c1576d`, `a2af273`, `972d0fa`). With `core.whitespace=cr-at-eol` (the file's actual convention) the diff is clean; the content contains no real trailing whitespace. Line endings were deliberately not converted (that would rewrite the whole file).

## 14. Remaining security limitations (honest inventory)

1. **Nonce rotation on silent OIDC refresh is not observable** (§4). Mitigations: Streamlit cookie expiry, explicit logout revocation, 12 h server-side session cap, account-switch revocation.
2. **Offline trust window for Classroom grants** (30 min): provider-side revocation (teacher removed from a course) is not detectable between trusted listing refreshes. **Privileged scheme approval remains DISABLED (no UI, no endpoint)** — nothing can act on a stale grant today. C6-B2 must decide whether per-approval re-verification is required.
3. **Assignment revision coverage** is limited to the documented field set (§6); changes outside it that also do not bump `updateTime` would escape.
4. **Teacher status is "teacher of the listed course", not institutional verification**: evidence is Google's own `teacherId=me` enforcement. No roster/role directory is consulted.
5. **State-replacement residue:** if trusted server code overwrote the auth-state entry with a non-dict value, the unreachable old authority object cannot be revoked through the mapping (Python-level reachability); the state is reset to a fresh, empty authority and no privilege survives in the state. In-place tampering fully revokes. Not reachable by any browser input.
6. **Synthetic-only claims:** all authorization behavior is proven with synthetic data offline; no live Classroom permission flow has been exercised (prohibited in this phase). Real-provider behavior of listing shapes is assumed from Google's documented API and the app's existing production usage.
7. One runtime caveat: `Cn` (unassigned) rejection depends on the running Python's Unicode version; CI runs 3.11 and 3.14, which can disagree on newly assigned code points (fail-closed direction only).

## 15. C6-B2 integration requirements

1. Build the durable approval UX on `current_teacher_session` + `ta.bind_manual_review` / verified assignment contexts; never call `bind_scheme_review` directly with hand-made digests.
2. Decide the grant re-verification policy for privileged actions (recommend re-listing the single assignment server-side at approval time) before enabling any approval control; keep the 30-min window or shrink it.
3. Preserve the five-way separation: warning acknowledgement / evidenced resolution / scheme approval / student-result approval / grade publication.
4. Pass the live context to every approval display; missing context stays read-only; never render stored `teacher.status` as effective approval.
5. Add transactional/audit design before any multi-worker persistence (authorities are per-process memory by design today).
6. Test the actual role-verifier integration against real (test-account) Classroom data under maintainer supervision before enabling approval UI.

## 16. Independent-review readiness assessment

**READY for independent security review — GO WITH CONDITIONS.**
Implemented and test-proven offline: identity trust boundary, nonce lifecycle, revocation on logout/switch/expiry/malformed state, course/assignment/manual grants with fail-closed verification, revision and document-provenance binding, F1–F3. Capabilities deliberately kept disabled: any approval UI or endpoint, live permission re-verification, provider-revocation detection. Conditions for merge: GitHub Actions green on both Python 3.11 and 3.14 jobs (pending at report time), and human security review of §14's limitations — in particular the OIDC-refresh nonce caveat and the 30-minute grant trust window, both inert while approval UI remains disabled.
