# Post-PR #10 marking failure investigation

Baseline: main `3c75a007d17895e29f07a14785b6b7332936d498`, merging PR #10 head `dbcdd4ca456c5cf023dde1190e7ad23122716b00`.

## What the reported message establishes

The exact inner sentence originates at the final failure in `marking_requests.generate`. `grade_with_model` delegates to that controller; `mark_one_submission` never reaches normalization or totals when it raises. The manual future handler stores a Failed row with `result`, `totals` and `groups` set to None. The individual report adds the prefix `Marking failed for this student:`. A Classroom worker catches the same exception into its failure reason, but the quoted prefix is from the manual report.

This proves no new grade was returned by that generation operation. It does not identify the provider failure and does not prove previous session results were preserved. The deployed commit itself has not been independently read from Streamlit.

The supplied 2026-10-13 UTC logs show successful app startup from main, Python 3.14.8, Streamlit 1.65.0 and google-genai 2.30.1. They show no `marking_metrics` record or provider exception. The layout deprecation warnings do not identify a marking failure. Streamlit replaced pyarrow 25.0.1 with 24.0.0 during setup; there is no crash trace in the excerpt. Diagnostic enablement is unknown. Absence of logs is not evidence for a quota, timeout, model availability or SDK failure.

## Distinct paths to the same old message

| Evidence inside the controller | Behavior in PR #10 |
| --- | --- |
| HTTP 429, shared/unknown/mixed quota scope | Stop immediately, set client-wide cooldown (at least 90 seconds; longer for daily/zero-limit evidence). |
| HTTP 429, exclusively structured per-model quota | Cool down only the affected model and try another within budgets. A quick rejection does not consume a costly attempt. |
| TimeoutError, timeout-named exception, HTTP 408/504 | Stop immediately; no retry because generation may already have run. |
| HTTP 400/401/403 | Stop immediately as rejected request. |
| HTTP 404 explicitly naming requested model as unavailable | Cool down that model for 24 hours; quick rejection can reach later models. |
| Missing-file or unidentified HTTP 404 | Stop; do not poison the model cache. Previously labeled transport/client error; now labeled rejected request. |
| HTTP 500/502/503 | Try another model within budgets; 30-second model cooldown. |
| Empty text or invalid JSON/result shape | Try another model within budgets. A nonempty list of question dictionaries is required. |
| MAX_TOKENS finish | Stop as truncated response, even if some JSON is present. |
| Prompt block or other non-STOP finish | Stop as blocked/incomplete response. |
| Response arriving at/after deadline | Discard late result and stop. |
| Other SDK/config/transport/response-access exception | Stop, previously with only the generic public message. |
| All models exhausted, skipped on cooldown, or empty configuration | Fail without further requests; possibly zero requests. |
| Four request limit, two costly attempts, or 180-second scheduling budget exhausted | Stop; these limits are distinct from the underlying final attempt outcome. |

An already-active account cooldown has a separate rate-limited message in PR #10. Upload/reference preparation, normalization, selected-question filtering, post-processing and audit failures are separate stages. For example, a result containing no selected questions raises a different ValueError after generation; an audit failure keeps first-pass marks with a warning. They must not be inferred from the exact quoted controller message.

## Grade preservation findings

- Generation failure does not write a successful-result cache entry or synthesize zero marks. Cache writes in the manual collector require a Marked row with no audit/scheme warning; result cache lookup uses document content and marking context, not filename alone.
- There are no automatic Classroom grade writes in the marking path. Suggested grades and teacher review remain separate from publication.
- **Existing loss of displayed history:** the manual collector replaces `last_batch_results` even if a new batch fails. A previous uncached successful row or teacher-reviewed result can disappear from the active report. Successful AI cache entries can still exist, but they are not a durable grade archive and do not preserve session overrides by themselves.
- **Existing Classroom history replacement:** `merge_classroom_results` replaces a previous success for a submission ID with a Failed/Skipped re-mark. Prior approval is revoked and failed work remains retryable, but the old successful report was not retained there.
- The patch snapshots displaced successful rows, with current teacher overrides materialized, before either replacement. One most-recent successful displaced batch is retained per path in session memory and can be downloaded as historical JSON. Repeated all-failed batches do not erase it. It is not matched by filename, promoted to current, or eligible for Classroom approval. Current failure rows remain failures.
- This is bounded recovery, not permanent history: another replacement containing successful rows can replace the snapshot; a lost session loses it unless downloaded. It cannot recover a result already lost before this patch. No access to the user's production session or stored grades was available, so actual historical loss cannot be excluded.

## Diagnostics changes and privacy

`MarkingFailure` supplies an allowlisted plain-language reason plus random request reference. It says **No new marks were saved**, avoiding an assurance about older session history. It never includes exception strings, provider payloads, filenames, keys, prompts or answers. The reference may appear wherever the failure message is displayed/exported; detailed telemetry remains server-only.

The existing opt-in `MATH_MARKER_DIAGNOSTICS=1` log gains `outcome`, `failure_category` and `stop_reason`. The final attempt outcome identifies what happened; the stop reason distinguishes terminal failures, model exhaustion, request cap, costly-attempt cap, account cooldown and deadline. Existing per-attempt model/status/quota scope and timing fields remain. No request limit, model list, prompt, scoring rule or fallback permission is relaxed. Logging-handler failure can no longer discard a successful marking result or replace its classified failure.

Do not log raw exceptions or SDK error payloads. Keep the existing logs restricted to administrators with limited retention. To investigate a future failure, enable diagnostics in the server environment and correlate its random UI reference with `marking_metrics`; share only the reference, safe model identifiers, outcome/category/stop reason, status, quota scope, counts and timings. Enabling diagnostics or reproducing a live generation is an operator action and was not performed here. Counts and timings are operational metadata, not proof of billing or marking accuracy. Historical downloads deliberately contain grades and student data already in the session and must be handled as private reports, never diagnostic logs.

Other application paths still stringify exceptions (including some preparation and audit paths). This patch does not claim to sanitize the entire application's error handling; expanding that work should preserve stage-specific behavior and receive separate tests.

## Validation

New deterministic regressions cover categories, terminal vs exhausted fallback paths, zero-request cases, deadline, truncation/blocking, logging-handler failure, manual storage wiring, failed Classroom re-mark preservation, override snapshots and repeated-failure retention. A real Streamlit test checks historical download controls and rerenders.

The CI UI/SDK job now tests both Python 3.11/google-genai 1.40.0 and Python 3.14/google-genai 2.30.1, reflecting the minimum supported SDK and the supplied deployment version. SDK HTTP requests are intercepted locally with synthetic credentials and content. No live Gemini, paid API, production mutation, merge or deployment is part of this investigation.

Local results (Windows, bundled Python 3.12): syntax checks passed; 184 regression tests passed; 9 Streamlit tests passed; 7 intercepted-HTTP SDK tests passed on 1.40.0 and the same 7 passed independently on 2.30.1. The SDK difference did not reproduce the failure in these tests. This is not a full reproduction of the Python 3.14.8 Linux production environment; CI covers the Python 3.14 minor version separately. Initial UI/SDK discovery was blocked by sandbox access to freshly installed dependencies; rerunning with read access passed. No live success/accuracy claim is made.
