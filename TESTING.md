# Offline testing and accuracy evaluation

Run with Python 3.11+ (stdlib only; no pip install, accounts, student files or API keys):

    python -m py_compile app5_1_automated.py
    python -m unittest discover -s tests -p 'test_*.py' -v

## What ordinary CI proves

- 24 original synthetic questions, each with an explicit M1/A1 allocation and correct,
  partial, incorrect and blank response: 96 score-preservation cases.
- Deliberately incorrect full-credit awards must be flagged without changing marks.
- Q77, Q25, Q21, Q47, Q6 and Q49 regression examples; missing/duplicate questions,
  maxima disagreements, evidence checks, arithmetic, normalization and totals.
- Production manual and Classroom orchestration, mocked Gemini generation/audit,
  quota cooldown/fallback, bounded request counts and malformed responses.
- Generated one/multiple-page PDF bytes survive temporary-file upload; remote
  PROCESSING/ACTIVE/FAILED states, timeout, transport failure and local cleanup.
- Classroom/Drive HTTP contracts, pagination, Docs export, permissions/errors,
  multiple attachments, cleanup, skipped/failed work and read-only marking.
- 100 runs of 24 synthetic questions must finish within 15 seconds locally/CI.
  Elapsed time and mock request counts are printed. This generous CPU smoke budget
  is not a live API latency or concurrency capacity measurement.

The app delegates PDF interpretation to Gemini; offline PDF tests exercise byte
transport and the remote processing lifecycle, not rendering, OCR, handwriting or
semantic grading. The synthetic response fixtures are authored expected outcomes;
a fake returning them is not evidence that a real model would assign those scores.

The headless loader compiles actual app function definitions and literal constants,
skipping Streamlit UI/OAuth execution. HTTP and Gemini boundaries fail unless explicitly
faked. This deliberately does not validate SDK installation, OAuth login or browser UI.
Never weaken an assertion merely to accept a failure; reproduce, inspect and fix the
underlying defect, then rerun the entire offline suite.

## CI and cost

The inherited workflow from PR #1 runs on pull requests, main pushes, daily and manual
dispatch. It uses a standard Ubuntu runner and Python, no paid APIs and no dependency
installation. CI has read-only repository permissions except the existing separate
main/scheduled failure issue-reporting job. No test publishes Classroom grades,
merges PRs or deploys. GitHub Actions billing depends on repository/account policy;
there is no GPU, hosted database or model/API cost in this suite.

The offline workflow is installed on main. Feature/fix pull requests run the same suite
before a maintainer chooses whether to merge them.

## Opt-in live acceptance plan (manual only)

Live testing is NOT run by CI and is NOT enabled by this PR.

1. Obtain explicit maintainer authorization, a hard request/spend cap, a test-only
   Google Classroom account/course and a currently available Gemini model. Prefer
   free quota and stop on quota exhaustion; do not enable billing as a fallback.
2. Use original synthetic PDFs first: typed, handwriting, rotations, multiple pages,
   image attachments, empty/corrupt files, and a Google Doc exported to PDF.
   Do not use real student submissions without explicit consent.
3. Establish a separate human-reviewed evaluation set: source mark schemes, fixed
   per-question maxima, visible response evidence and teacher-adjudicated marks.
   Keep any consented scans, credentials and transcripts outside this repository.
4. Run manual upload and Classroom import on the same materials. Inspect selected
   and missing questions, all mark decisions, discrepancies and cleanup. Restrict
   the live harness to read/download operations; never PATCH grades or return work.
5. Record only aggregate exact question-score agreement, mean absolute mark error,
   false-full-credit rate, missed-question rate, review rate, p50/p95 elapsed time,
   generation/upload/poll counts and quota errors. Break down by scan quality/topic.
   Predeclare acceptance thresholds with the teacher; do not derive them from results.
6. Review disagreements against scans and original schemes. Add a synthetic regression
   reproducing each software defect, fix in a branch, rerun offline tests and open a PR.
   Human approval is required before deployment. Do not change teacher scores to fit AI.
7. Remove remote test files, revoke test credentials when done, and keep names,
   submission IDs, access tokens, raw provider errors and scans out of GitHub logs.

## Automatic investigation

Actions runs tests and reports failures; it does not itself launch Codex. An assigned
Codex session can inspect failing runs, make fixes and open PRs. Continuous unattended
repair requires separately configured agent scheduling/authorization; no such service
or paid agent job is introduced here.


## Review-state and lifecycle regressions

The suite now also executes the actual manual summary/export block and Classroom
results block with a recording UI. It checks that overrides affect metrics,
question rows, totals, Markdown and CSV in the same rerun. These are headless UI
contract tests, not a real browser/OAuth end-to-end run.

- Every new run (including a manual cache hit) creates a fresh review identity.
  Cached AI results are kept separate from reviewed results; original marks and
  teacher reasons are available in exports.
- Classroom overrides invalidate approval using a Streamlit callback before
  widgets render. Re-marking creates a new approval identity.
- Classroom history is scoped to account, course, assignment, scheme and settings.
  Each check reads Drive version metadata to detect edits; this adds Google
  metadata requests but no extra model generation for unchanged submissions.
  Failed/skipped work stays retryable. An explicit re-mark option bypasses the cache.
- Validators run before the optional audit and again after it. Flagged questions
  may now trigger an audit that was previously skipped. Audit remains opt-in via
  the existing checkbox and may consume additional model quota.
- Failed/timed-out remote uploads are deleted on a best-effort basis. Batch
  failures collect late successful uploads for cleanup before reporting the error.
  Successful uploads remain owned by the existing caller cleanup path.
- The unused grade-boundary upload has been removed. All displayed bands remain
  provisional; this change does not implement official session-specific grades.

For a manual acceptance check, use synthetic work, apply an override, download both
report formats, re-mark the same filename, switch Classroom assignments, edit a test
attachment and check again. Confirm that each new run needs its own review and that
no grades are written to Classroom. Do not use live student data for CI.


## PR review follow-up regressions

The stdlib suite covers submitted widget values versus render-time callback
arguments; success → failed/skipped forced re-mark → normal retry; withdrawal and
resubmission; unchanged current approvals; and keeping environment/Secrets keys
out of browser inputs. Result reconciliation uses the complete successful
turned-in listing, including when there are no new submissions to mark. A failed
listing does not remove or relabel history.

A separate CI job installs Streamlit 1.66.1 and runs real widget lifecycle tests:

    python -m pip install streamlit==1.66.1
    python -m unittest discover -s tests/ui -p 'test_*.py' -v

(1.66.1 is the first release in the requirements window that admits the
CVE-fixed Pillow 13.0.0 pin: streamlit <=1.66.0 caps `pillow<13`.)

These tests use AppTest, the production report block and synthetic session data.
They submit edited marks/reasons together with the Apply button, check approval
reset before rendering, and verify historical results cannot be approved or edited.
They make no OAuth, Classroom or Gemini requests. AppTest is not a browser/OAuth
end-to-end test; the live acceptance plan above still applies. The original
stdlib job remains dependency-free; the UI job requires package downloads.

### Credential migration

Automatic key persistence and reading keys from a local .env file have been
removed. Existing .env files are not deleted or modified. If you used that feature,
configure GEMINI_API_KEY in Streamlit Secrets or the server environment, or enter
a key for the current session. Server credentials are never used as widget values.
Keys entered through the UI are session-only and are never saved to a shared file.

### Historical Classroom results

After a successful check, submissions no longer in the turned-in listing remain
visible as historical results. Their previous score is retained for reference,
but their approval is cleared, approval/override controls are disabled or absent,
and no current Classroom grade is suggested. A subsequent resubmission receives
a fresh review even if its revision fingerprint is unchanged. Unchanged work
that is still turned in retains its review and approval.


## Document preparation performance

Run `python tests/benchmark_document_pipeline.py` for a zero-network comparison against PR #4. The main stdlib suite enforces its request budgets. `python -m unittest discover -s tests/ui -p "test_*.py" -v` includes the actual manual Mark button and ordinary rerun checks in Streamlit AppTest. See [PDF_PERFORMANCE.md](PDF_PERFORMANCE.md) for measurements, cache privacy, lifecycle, request limits and unmeasured live stages. Never add supplied student PDFs to test fixtures.

### Parser and download resource budgets (post-S6)

Two wall-clock bounds are tunable by environment variables (see
`document_limits.py`) and enforced by default:

- Deep PDF metadata inspection runs in a disposable child process with a
  hard deadline (`MARKER_PDF_PARSE_SECONDS`, default 20 s, clamped 2-300 s).
  A stalled parser is terminated and the document is rejected with the
  content-free `PDF_PARSE_TIMEOUT` category; simultaneous parser children
  are capped by `MARKER_MAX_PARSER_PROCESSES` (default 2).
  `python tests/benchmark_parser_deadline.py` measures the isolation
  overhead on synthetic documents and proves the deadline terminates a
  stalled parser. `MARKER_PDF_PARSER_ISOLATED=0` opts out of isolation for
  debugging only, and then the deadline guarantee does not apply.
- Classroom/Drive attachment downloads carry an aggregate transfer budget
  (`MARKER_MAX_DOWNLOAD_SECONDS`, default 300 s, clamped 30-1800 s) on top
  of the ≤120 s per-socket-operation timeout; a transfer expiring it is
  rejected with `DOWNLOAD_TIMEOUT` and stays retryable. The per-read socket
  timeout still bounds a single stalled read, so the aggregate bound is
  checked between reads (best-effort, worst case budget + one read
  timeout). Classroom failure reasons remain content-free categories
  (`classroom_failure_reason`), never raw provider exception text.


## Installation

Production deployment (Streamlit Community Cloud) consumes exactly one manifest:
`requirements.txt` in the repository root. Local developers mirror it:

    python -m pip install -r requirements.txt
    streamlit run app5_1_automated.py

Offline test/benchmark dependencies (PDF stack + ReportLab fixture generation +
SymPy verification research) live in `tests/pdf-requirements.txt` and are never
part of production installation.

Python support: CI validates Python 3.11 and 3.14 (Streamlit UI matrix);
3.11+ is the documented floor.

## Dependency policy and supply chain (Stage S5)

Constraints are intentional, not accidental (findings F-10, F-11, F-12):

- Every straight dependency in `requirements.txt` carries a bounded range
  (`>=x,<y`) validated by the CI matrix, or an exact pin for the
  untrusted-input PDF stack (`pypdf`, `pypdfium2`, `Pillow`). No open-ended
  floors. Numerical code and model-param behaviour must be re-verified when a
  range is widened.
- `Authlib>=1.3.2,<2` stays despite no direct imports: the app uses Streamlit's
  native `st.login("google")` OIDC sign-in, and Streamlit imports `authlib.jose`
  from `streamlit/auth_util.py` under its `auth` extra. Removing it breaks
  Google sign-in on Streamlit Cloud. `tests/test_supply_chain.py` guards this.
- `sympy` is development-only (offline verification research/benchmarks in
  `investigation/`); the deployed app performs plain Python arithmetic.
- There is intentionally no lockfile: Streamlit Community Cloud does not
  consume one, and duplicating the manifest would create two drifting sources
  of truth. Reproducibility instead comes from pinned PDF stack versions,
  CI-validated windows, and auditing the fully resolved set.
- Updates arrive as reviewable Dependabot PRs (`.github/dependabot.yml`
  pipelines: root manifest, tests manifest, GitHub Actions).
- All workflow actions are pinned to full-length commit SHAs; workflows run
  least-privilege (`contents: read`) and reference no repository secrets.
- `tests/test_supply_chain.py` enforces these guarantees on every PR.

### Automated vulnerability scanning and response

A `dependency-audit` CI job runs pip-audit (PyPA/GitHub advisory data) against
the resolved production and test dependency sets. The job fails on any
published advisory or resolution error (`--strict`), so a skipped audit can
never be reported as clean. Nobody should add a suppression to force it green;
maintainers respond to findings by:

1. Reading the advisory ID in the job log; confirming whether the vulnerable
   component is actually used (same method as S5: import/reachability review).
2. Patching with the smallest compatible upgrade, or documenting a C-category
   (unreachable/monitor) decision in the stage log when no fix exists.
3. Reviewing Dependabot alerts as a second opinion; rely on scanner IDs, not
   on local logs when in doubt.
