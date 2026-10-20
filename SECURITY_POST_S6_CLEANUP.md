# AI Maths Marker — Post-S6 Security Cleanup Report (S6-R1, S6-R2, S6-R4, S6-R5)

Base commit: `b5f4f45737a47d0cdfdc838a7a44d0616ae60d20` (merge of PR #30).
Branch: `security/post-s6-cleanup`. All four Stage S6 cleanup findings were
re-verified against the current source (not the historical line numbers)
before any change; verification evidence and tests are listed per finding.

Scope guard: no S3 document-size/page-count/batch limit changed, no S4 quota
budget changed, no marking-engine or Phase-B/C verification behaviour
changed, and no live Gemini/Classroom/Drive traffic occurs in any test.

---

## S6-R1 — Classroom error privacy (originally Low)

**Confirmed root cause.** Two inconsistent patterns existed:

1. `classroom_request()` raised `RuntimeError(f"Google API error {status}:
   {response.text[:2000]}")` — up to 2000 characters of provider response
   body could travel into any caller's exception text.
2. The Classroom worker stored `"reason": str(mark_error)` (and the
   worker-failure fallback stored `str(worker_error)`), so raw provider
   text reached stored rows, `st.write` failure lines, the classroom
   summary table (`Reliability` column) and any JSON export of previous
   results. Audit failures stored `str(audit_error)` in
   `audit_warning` on both the manual and classroom paths (shown via
   `st.warning`). The manual failure path already used the S2.1-safe
   `marking_failure_reason`, which was part of the flagged inconsistency.

**Fix (app5_1_automated.py).**

- `classroom_request` now raises `RuntimeError("Google API error <status>.")`
  — the body is never included; only the status code is (it is
  non-sensitive).
- New `classroom_failure_reason(exc)` reuses the existing
  `provider_error_code()` allowlist (the same helper `safe_error_detail`
  uses) plus the `DocumentValidationError` content-free contract, and emits
  exactly one line: exception class + allowlisted category + (when safely
  matched against a fixed list of legitimate codes 400..504) the HTTP
  status + the S2.1 privacy suffix. No filenames, Drive IDs, tokens,
  response bodies or student data can pass.
- Applied at all three failure-reason sites: submission-level catch,
  worker-failure fallback, and both `audit_warning` sites now use
  `safe_error_detail()` instead of `str(...)`.
- Categories preserved: quota/rate limit, credential rejection, permission
  refusal, timeouts/cut-off connections, temporary unavailability, invalid
  request, unparseable response, document rejections (with the stable S3
  codes such as `UNSUPPORTED_TYPE`, `TOO_MANY_PAGES`, `DOWNLOAD_TIMEOUT`).
  Teachers still see which service-level class failed and whether retrying
  makes sense; failed submissions remain retryable and never convert to
  successful marks.

**Tests** (`tests/test_post_s6_cleanup.py::ClassroomErrorPrivacy`):
response bodies with synthetic student names / Drive IDs / fake
credentials never appear in raised API errors, in reason fields, in full
worker-result rows (status stays `Failed`, generation never runs), or in
`audit_warning`; HTTP status matters (403 preserved as a safe category);
quota and document categories remain useful.

**Status: Fixed.**

---

## S6-R2 — PDF parser resource safety (originally Medium at public launch)

**Confirmed root cause.** `document_limits.pdf_page_count()` constructed
`PdfReader(...)` and read `len(reader.pages)` synchronously inside the
marking process (manual preflight in the main thread; classroom scheme and
attachment paths in the worker). A malformed/hostile page tree can keep
pypdf working for an unbounded wall-clock duration. Production call sites:
`app5_1_automated.py` lines ~2581 (Classroom attachment), ~2770 (Classroom
scheme), ~4216 (manual preflight), plus the `deep=False` transfer boundary
(document_pipeline.py, not affected).

**Fix (document_limits.py + call sites unchanged).**

- `pdf_page_count()` now runs the pypdf metadata walk in a **disposable
  child process** under a hard wall-clock deadline
  (`PDF_PARSE_TIMEOUT_SECONDS`, env `MARKER_PDF_PARSE_SECONDS`, default 20 s,
  clamped 2-300 s). The deadline is enforced by the parent with
  `subprocess.run(..., timeout=...)`, which **terminates and reaps** the
  stalled child on expiry — unlike a `future.result(timeout=...)` thread
  timeout, which leaves a stuck thread running. A new content-free category
  `PDF_PARSE_TIMEOUT` makes the failure retryable and safe to display.
- The child streams the PDF over stdin (no disk, no temp file), prints one
  JSON line `{"ok", "encrypted", "pages"}` and exits; only a boolean and an
  integer cross the boundary. Any parser escape — including `MemoryError` —
  is reported as an ordinary parser failure (`INVALID_PDF`).
- Spawn failure (locked-down hosting) fails closed (`INVALID_PDF`): a
  document that cannot be verified is never accepted implicitly.
- Concurrency is bounded: a module-level `BoundedSemaphore(2)`
  (`MARKER_MAX_PARSER_PROCESSES`, clamped 1-8) caps simultaneous parser
  children across all threads.
- Encryption, page-count limit, size limit and error-message contract are
  unchanged (`ENCRYPTED_PDF`, `TOO_MANY_PAGES`, `INVALID_PDF` keep their
  messages; checked). Windows (spawn via `sys.executable -c`, no shell) and
  Linux are both supported; the child imports `document_limits` via cwd +
  `PYTHONPATH`, never importing or launching Streamlit. No waiting-on-stuck
  thread-style pseudo-timeout is used anywhere.
- Escape hatch `MARKER_PDF_PARSER_ISOLATED=0` restores the historic
  same-process parse (documented for debugging); this path has **no**
  deadline and is explicitly a residual risk when chosen.

**Performance impact** (Windows 3.14.4, `tests/benchmark_parser_deadline.py`,
now in CI):

| Document | In-process parse | Isolated parse | Overhead |
|---|---|---|---|
| 6-page digital-ink synthetic | median 1.2 ms | median 358.5 ms | +357 ms |
| 6-page scanned-like synthetic | median 1.2 ms | median 358.1 ms | +357 ms |

The isolated cost is dominated by interpreter startup (constant per
document, size-independent on these inputs). Marking latency is dominated
by Gemini transfer/generation (seconds-to-minutes per submission, one
student at a time), so the added per-document cost is small in context; the
manual-batch preflight of 30 documents adds roughly 11 s worst-case. A
CI-verifiable measurement runs on every PR so drift is visible.

**Tests** (`tests/test_post_s6_cleanup.py::ParserIsolation`): parity of
isolated vs same-process paths (valid/corrupt/encrypted); page-count limit
still enforced; `validate_document` still reports page counts; a **real
hanging child process** is killed at the budget (`PDF_PARSE_TIMEOUT`),
the calling path recovers with normal parsing right afterwards, and the
permit pool is intact; `subprocess.TimeoutExpired`/`OSError` map to
content-free categories; concurrent parses are serialized by the permit
bound.

**Status: Fixed** (isolated path default; the explicit env opt-out keeps
the historic risk visible and opt-in only).

---

## S6-R4 — Classroom download deadline (originally Low)

**Confirmed root cause.** `_download_bounded()` streamed with
`timeout=120`, a per-socket-operation timeout. It bounded a stalled
connection op but not the whole transfer: a compliant trickle could feed
one byte per 119 s community and stay under the byte limit while
occupying the marking worker indefinitely.

**Fix (app5_1_automated.py `_download_bounded`).**

- A monotonic whole-transfer budget
  `document_limits.MAX_ATTACHMENT_DOWNLOAD_SECONDS`
  (env `MARKER_MAX_DOWNLOAD_SECONDS`, default 300 s, clamped 30-1800 s) is
  started before the HTTP request and checked (a) after the request
  returns and (b) before every buffered chunk, using `time.monotonic`
  (injectable `clock`/`duration` for deterministic tests).
- The per-operation socket timeout is now `min(120, budget)`, so a single
  stalled read cannot wait longer than the shorter of 120 s or the whole
  budget.
- Expiry raises the content-free retryable `DOWNLOAD_TIMEOUT`
  (`DocumentValidationError`), partial content is discarded, partial
  variables never escape, the response is always closed in `finally`, and
  the row stays `Failed` without leaking the S4 permit (the run-level
  permit release path is unchanged and outside the worker).

**Guarantee wording (important).** This is a **best-effort aggregate
bound**, not a strict per-byte deadline: expiry is checked between reads,
and in the worst case one already-started blocking read can continue up to
the socket read timeout (≤120 s) before the deadline check sees it.
Requests/urllib3 offer no portable way to abort a mid-read without
replacing the transport (async I/O); that architecture change is out of
scope. The worst total occupancy is therefore budget + one read timeout.
Everything else is bounded: byte limit unchanged, streaming unchanged,
MIME/metadata validation unchanged.

**Tests** (`tests/test_post_s6_cleanup.py::DownloadDeadline`, deterministic
fake clocks and fake streams): normal transfer (complete bytes, response
closed); missing declared size (bounded download still applied at the
Drive metadata flow); oversized transfer (byte-limit category preserved);
deadline expired before the first chunk; deadline expiring between chunks
(partial discarded); few-bytes/slow-clock trickle trip; mid-stream failure
propagates and the response is closed with the upstream sanitization
shown; HTTP-failure message stays content-free; socket read timeout capped
by the budget.

**Status: Partially Mitigated → effectively Fixed for all practical
failure classes; the strict whole-transfer guarantee is documented as
best-effort due to the requests/urllib3 in-process transport.**

---

## S6-R5 — GitHub Actions version comments (originally Informational)

**Verification.** All three immutable SHA pins were re-verified against
the GitHub refs API (2026-10-20) with `refs/tags/<tag> -> commit object`:

| Action | Pin | Verified tag | Verdict |
|---|---|---|---|
| `actions/checkout` | `34e114876b0b11c390a56381ad16ebd13914f8d5` | `v4.3.1` | comment accurate |
| `actions/setup-python` | `a26af69be951a213d495a4c3e4e4022e16d87065` | `v5.6.0` | comment accurate |
| `actions/github-script` | `f28e40c7f34bde8b3046d885e986cb6290c5673b` | `v7.1.0` | comment accurate |

**Finding not confirmed** — the comments accurately describe their pinned
commits, so no pin or label was changed and no version label was invented.
Only one workflow line was added (the new parser-deadline benchmark step);
permissions, `persist-credentials: false`, audit job, matrix and all
existing steps are untouched, and `tests/test_supply_chain.py` still passes.
`tests/test_post_s6_cleanup.py::WorkflowVersionPins` now asserts the pins
and comments against the verified table so any future bump forces
re-verification.

**Status: Fixed (verification guard added); no code change required.**

---

## Offline verification results (2026-10-20, Windows 3.14.4)

- Backend `tests` suite: **425 passed** (400 historical + 25 new
  post-S6 cleanup tests).
- Streamlit UI suite: **34 passed**. SDK interception suite: **14 passed**.
- Investigation suite: **493 passed**.
- Offline benchmarks: **9/9 pass** — the 8 historical benchmarks plus the
  new `tests/benchmark_parser_deadline.py` (also added to CI).
- `py_compile` on `app5_1_automated.py`, `document_limits.py`: OK.
- No live provider traffic; all Classroom/Drive/Gemini behaviour is
  synthetic and mocked, and identity/HTTP boundaries fail closed.

Regression guarantees re-checked inside the suite: S2 anonymous guards
(`test_authentication_guard`), S2.1 isolation (`test_upload_validation`,
`test_review_safety`), S3 limits (`test_document_limits`), S4 quota gates
(`test_gemini_limits`, permits release in `finally`), S5 supply chain
(`test_supply_chain`). Marking totals, override/approval rules and
retryability assertions are untouched and still pass.

## Remaining limitations

- Download deadline is between-read best-effort (worst case budget + one
  socket read timeout of ≤120 s); a strict guarantee would need a
  non-blocking transport.
- Parser isolation adds ~0.36 s per document of process startup on Windows
  (expected lower on Linux/CI); the flag `MARKER_PDF_PARSER_ISOLATED=0`
  removes both the guarantee and the cost for hosts that forbid spawning.
- `_safe_http_status` may preserve a benign 3-digit run as "HTTP status"
  when user-controlled text contains such a run; the values are from a
  fixed allowlist and carry no private content.
- Public SaaS deployment remains NOT APPROVED (S6 verdict unchanged).
