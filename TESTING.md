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

PR #1 remains unmerged. This branch includes its three commits so CI can run while
the maintainer reviews both changes. Review/merge ordering remains a maintainer choice.

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
