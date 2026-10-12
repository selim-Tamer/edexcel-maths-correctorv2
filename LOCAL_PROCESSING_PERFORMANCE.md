# Local processing and reference preparation performance

Baseline: main `78230becbdfb8dcd3a9141c974823a083e623286` (merged PR #8).
All measurements below are offline, using synthetic data. No live Gemini requests,
paid services, Classroom changes, student files or published grades were used.

## Findings and changes

Cold random-question preparation used two generations on the same references:
question-number discovery followed by maximum-mark/answer extraction. Manual and
Classroom preparation now request both in one structured response. The inventory
is separate from known maxima: a question with an unclear total must not disappear
from selection. Incomplete, malformed, duplicated or inconsistent inventories fall
back to the existing discovery path. Truncated/non-STOP extraction responses are
not accepted or cached. Original PDFs remain the evidence for marking and audits.
The SDK schema is checked with intercepted HTTP against google-genai 1.40.0.

Manual scheme metadata previously depended on the optional question-paper hash
and audit configuration. It now depends on the scheme hash, session/account scope,
app version and primary model configuration. Question inventories and student
results still depend on the question paper and full relevant configuration.
Classroom metadata remains assignment scoped. The existing bounded 10-minute,
non-sliding session caches and Gemini file expiry/status checks are preserved.
Changing a question paper therefore invalidates results without re-reading the
same scheme. No shared student cache was added.

The numeric validator repeatedly parsed intermediate values in equality chains.
A per-explanation memo now reuses those exact values. It is discarded on return;
no student text is retained globally. Differential tests compare correct and
incorrect chains to the frozen PR #8 implementation, including recurring decimals,
rounding, zero division and missing parentheses. Arithmetic warnings, method marks,
original scores and mandatory teacher review remain unchanged.

PyMuPDF adds an **opt-in local original-page inspector** under manual uploads.
Teachers explicitly choose 1–10 pages, see a visual preview and digital text, and
can download a PDF excerpt for review. The excerpt preserves page content and ink
annotations; synthetic digital and image-only fixtures render pixel-identically
before and after extraction. The original uploaded bytes are never replaced by
the excerpt. Text-only scans correctly return no digital text, not invented OCR.
Inspection is lazy, bounded to 50 MB/2,000 source pages, serialized across sessions
for PyMuPDF safety, and has no persistent text cache or Gemini calls. Parser errors
are replaced with a fixed safe message. This is a teacher review improvement,
not an automatic reduction in model input pages or marking latency.

## Measured results

Windows local run, Python 3.12.14 runtime, PyMuPDF 1.26.7, SymPy 1.14.0.
CPU medians: 20 iterations for PDF operations, 200 for arithmetic. Full output and
profile are in `tests/local_benchmark_results.json`; timings vary by machine/load.

| Measurement | Before | After |
|---|---:|---:|
| Cold random-selection metadata generations, either PDF fixture | 2 | 1 |
| Simulated metadata latency, **assuming 15 s per generation** | 30 s | 15 s |
| Cold original-PDF uploads, one student + scheme | 2 | 2 |
| Completed repeated preparation requests | 0 | 0 |
| Four equality-chain checks, local median | 0.7138 ms | 0.6425 ms |

The measured request reduction is 50% for **metadata only**, not overall marking.
At one unchanged marking call per student, a one-student run without audit uses
three versus two model generations; the relative saving falls with larger batches.
Uncertain inventories still need discovery and do not receive the one-call saving.
Combined response generation could take longer than either separate response;
actual end-to-end improvement requires a live, authorized test environment.

Six-page digital PDF with vector pen strokes/diagram/ink: 8,435 bytes; extracting
all digital text took 8.8056 ms. Two-page excerpt plus preview: 28.5644 ms, 3,173
bytes. The image-only counterpart was 9,023,753 bytes: digital text scan 0.8057 ms
(zero characters); excerpt plus preview 49.2131 ms, 3,012,662 bytes. These are
synthetic handwriting-like strokes, **not a real handwriting accuracy evaluation**.

The existing document transport benchmark still passes: cold polling uses 7 GETs
versus the historical PR #4 baseline's 19. Its virtual cold latency remains 13.85 s
versus 12.45 s (the documented quota/latency tradeoff from PR #5); this PR does not
change polling. Warm retained references remain 0 uploads/1 status GET. See
`PDF_PERFORMANCE.md` for transport details. No claim is made about browser-to-
Streamlit upload latency, provider processing time, or live model throughput.

## Rejected optimizations and limits

- SymPy's safe typed-AST arithmetic measured 0.1006 ms versus the existing numeric
  evaluator's 0.0985 ms for four expressions, plus roughly 498 ms import time.
  An earlier local run was also slower. It offers no demonstrated speed benefit
  here, so it is a **benchmark dependency only**, not loaded by the app. Never use
  `sympify`/`parse_expr` on untrusted student strings. Algebraic equivalence cannot
  establish handwriting, method credit or official evidence; no required audit is
  skipped on that basis. Audit request count is unchanged.
- Automatic page splitting/text-only marking was rejected: image-only scans yield
  no text, and a selected question can continue onto another page or contain a
  diagram. An excerpt being smaller is not evidence that it is safe for grading.
  No compression, downsampling or lossy re-encoding is applied to marking inputs.
- Upload concurrency stays at two; marking remains sequential. Existing request
  cooldowns and file cleanup remain in place. Reference extraction disables hidden
  SDK retries, limits output to 8,192 tokens and checks completion before reuse.
  Its existing model fallback list remains bounded; combined metadata introduces
  no additional generation retries. Large/unclear schemes may need fallback and
  teacher review; there is no live coverage of model output quality yet.
- PyMuPDF's [license](https://pymupdf.io/licensing) is AGPL/commercial dual licensing.
  This PR uses the free package and does not change the repository's license or
  purchase a commercial license. Review its deployment obligations for this app.
- The inspector is a convenience for manual uploads. Classroom's marking path uses
  the shared metadata optimization and original files; Classroom student downloads
  are not retained for inspection.

## Verification and next checks

Passed locally: **152 regression tests, 8 real Streamlit interaction tests, 4 real
SDK tests with intercepted HTTP**, syntax validation, both PDF benchmarks, and
visual inspection of the synthetic page preview. Existing reliability tests from
PRs #4–#8 run in the full suite. CI installs the pinned PDF dependency and runs the
new benchmark on Linux as well as the existing checks. No expected outcome is
substituted for application parsing, preparation, validation or PDF operations.

Reproduce with `python -m unittest discover -s tests -p 'test_*.py' -v`, the same
command for `tests/ui` and `tests/sdk`, and
`python tests/benchmark_document_pipeline.py` / `python tests/benchmark_local_processing.py`.
Install application requirements plus SymPy 1.14.0 for benchmarks; CI pins the
Streamlit/SDK test versions explicitly.

Before production adoption, authorize a separate test deployment and free-tier
request cap, then compare real combined/separate metadata outputs against a
teacher-approved scheme (including unclear totals, long schemes and continuation
pages). Check real free-tier latency, Cloud memory/import behavior and multi-user
isolation. No automatic merge, deployment or grade publication is part of this PR.
