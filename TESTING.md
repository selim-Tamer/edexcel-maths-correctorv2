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

A separate CI job installs Streamlit 1.65.0 and runs real widget lifecycle tests:

    python -m pip install streamlit==1.65.0
    python -m unittest discover -s tests/ui -p 'test_*.py' -v

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
