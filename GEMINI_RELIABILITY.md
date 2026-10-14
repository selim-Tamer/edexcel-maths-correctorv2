# Gemini service reliability investigation — 14 October 2026

Baseline: main `1d3b0613db78c4153919d31b1e38b7a0dc8b4905`, including PR12.
No live Gemini requests, paid APIs, deployment or grade publication were performed.

## Evidence and uncertainty

The supplied production record shows two HTTP 503 responses from 3.8 Flash and
3.6 Flash after 8.413s and 3.183s. Neither timed out. Queue/sleep time was zero;
the controller stopped after its two generation attempts. Startup diagnostics
were enabled and the matching private record was emitted successfully.
Google describes 503 as temporary service overload/unavailability. The record
does not identify the provider's internal reason, establish a widespread outage,
or exclude an input-specific backend failure.

Official documentation checked on 14 October 2026:

- [3.8 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash)
  and [3.6 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.6-flash)
  are documented model identifiers with PDF input, structured output and thinking.
- [3.5 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash)
  is deprecated and routes to 3.6 Flash. It is not an independent fallback.
- [3.5 Flash-Lite](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite)
  remains the existing last fallback. No new model is introduced or promoted.
- [Pricing](https://ai.google.dev/gemini-api/docs/pricing) lists free-tier input
  and output for these three retained models. This does not establish a particular
  account's access, remaining quota or billing status. No billing features change.
- [Troubleshooting](https://ai.google.dev/gemini-api/docs/troubleshooting)
  recommends bounded delayed retries for transient failures.
- [Structured output](https://ai.google.dev/gemini-api/docs/generate-content/structured-output)
  and [document processing](https://ai.google.dev/gemini-api/docs/generate-content/document-processing)
  document the existing PDF/JSON path. Larger documents and required reasoning
  can increase work; the supplied record has no PDF page/token measurements or
  successful usage metadata to attribute this failure to size.

No evidence justifies changing SDK version, removing schema fields, stripping
PDFs, reducing thinking, or promoting a less capable model. The main call has no
custom thinking, sampling or output-token settings. SDK 1.40.0 and 2.30.1 are
tested with actual request serialization and intercepted HTTP, including 503
headers. That establishes client behavior, not server compatibility or accuracy.

## Changes and bounds

- Remove the redirected 3.5 Flash alias from marking and audit lists, preserving
  the order of independent models. Reference/result cache keys include these lists.
- Before an eligible fallback following 503, wait five seconds, or a longer valid
  Retry-After (seconds or HTTP date). Malformed/nonfinite values use five seconds.
- At most one automatic wait, capped at 30 seconds, within the existing 180-second
  scheduling budget. Long Retry-After values stop instead of being shortened;
  no fallback is sent before the requested interval on that client.
- Keep two non-fast-rejection generation attempts and four absolute HTTP calls.
  The default list now contains three models, so it can make at most three calls;
  two consecutive 503s still stop after two. Confirmed quick model-specific quota
  or unavailable-model responses retain the existing bounded skip behavior.
- Preserve 120-second per-request timeouts, shortened to remaining budget.
  SDK automatic retries remain disabled. Ambiguous timeouts/504, shared or unknown
  quota, authentication failures and 500/502 retain their prior policy.
- No sleep when no eligible fallback remains. Recheck deadline after sleep to
  guard scheduler oversleep. No new concurrency or model-discovery requests.
- Private metadata adds stage, numeric retry interval and an allowlisted decision.
  No header text, raw errors, responses, PDF contents or credentials are logged.

The 180 seconds is a scheduling/request-timeout budget, not a hard OS cancellation
deadline: lock queueing is separately measured, and a stuck transport or scheduler
can exceed it. No new request is started after the deadline. Automatic added
sleep is 0–30 seconds, normally five, and consumes that budget.

## Offline comparison

`python tests/benchmark_service_recovery.py` executes the frozen PR12 controller
and current controller against identical scripted provider behavior. Success
costs 60 virtual seconds; a failed response costs one. These are synthetic
recovery windows, not measured production success probabilities.

| Scenario | PR12 | Proposed | Requests before / after |
| --- | --- | --- | --- |
| Immediate success | Success, 60s | Success, 60s | 1 / 1 |
| First model fails, healthy fallback | Success, 61s | Success, 66s | 2 / 2 |
| Persistent 503 | Failure, 2s | Failure, 7s | 2 / 2 |
| Service recovers after 5s | Failure, 2s | Success, 66s | 2 / 2 |
| Service recovers, Retry-After 12s | Failure, 2s | Success, 73s | 2 / 2 |
| Retry-After 120s | Failure, 2s | Deferred, 1s | 2 / 1 |

Across these six deliberately selected cases completion changes from 2/6 to 4/6.
This is a fixture score only. The cost is five seconds for an already healthy
fallback or persistent outage. No live speedup or recovery-rate claim is made.
All four existing benchmarks also run: document transport, local processing,
PDF inspection and the earlier marking-request comparison. Original PDF bytes,
rendered pixels and warm-cache request reductions remain covered.

## Preservation, manual recovery and limits

Prompts, original PDFs, official scheme evidence, validation, partial credit,
teacher overrides/approval and Classroom revision/publication code are unchanged.
Tests exercise actual application marking/validation after fallback and compare
correct, incorrect and partial-credit fixtures with direct success. Existing
snapshot, approval-revocation, owner isolation and Streamlit rerun tests remain.

Teachers can retry with the existing marking action. Completed eligible results
are reused; failed results are not cached as success. Historical teacher overrides
remain downloadable, never automatically approved current grades. Within an
automatic fallback, the exact same attachment objects are reused without uploads.
Across manual attempts, references are reused only when the existing optional
retention setting is enabled and cache validity/expiration checks pass. Student
uploads are cleaned up at run end; retaining them longer was rejected for privacy.

Cooldowns are client-local. The app creates a fresh client for a new manual run,
so a later button click does not inherit the previous client's cooldown. A teacher
must wait before retrying; this change does not add an account-wide rate limiter.
Session history/caches are lost on restart and snapshots retain at most 200 rows
per path. Optional second-pass audits/reference extraction have separate existing
request policies; the marking limits above are not a whole-homework API cap.

Repeated 503s can still fail. Free-tier capacity/access, real handwritten accuracy,
input-dependent provider behavior, and the best delay require an approved live
test environment with synthetic documents. A final pre-merge review should check
these limits, privacy, model alias removal and the retry latency trade-off.

## Local validation

209 regression tests, 11 Streamlit interaction tests and 8 SDK compatibility tests
pass (228 distinct tests). The 8 SDK tests pass on both 1.40.0 and 2.30.1, for 236
test executions when counting both SDK runs. All five offline benchmarks and
Python syntax checks pass. CI repeats UI/SDK validation on Python 3.11 and 3.14.
The new tests cover delay recovery, persistent 503s, seconds/date/malformed
Retry-After, manual retry after cooldown, client isolation, deadline oversleep,
unchanged other HTTP policies, request bounds and marking credit preservation.
