# Main marking latency: offline investigation

Baseline: main 0fd4aedeeec0dec9b4039e8a478f0df2aa9a9573 (merged PR #9). No live Gemini calls or student data were used.

## Findings and changes

The main request already batches all selected questions for one student, with the complete student PDFs, official scheme and optional question PDF. Successful marking takes one generation, not one per question. Existing preparation caches and upload expiration checks remain unchanged. Metadata extraction and optional audits are separate stages.

The client already had a 120-second timeout. The tested minimum SDK (google-genai 1.40.0) defaults to one attempt, so hidden SDK retries are not an established cause. The application, however, could try four models after failures, including ambiguous timeouts and quota errors. We cannot attribute the reported three-minute run to this without live diagnostics.

The new controller preserves the 120-second request timeout, sets SDK attempts explicitly to one, and allows at most four HTTP generation requests (three fallbacks), at most two non-fast-rejection attempts, and a 180-second scheduling budget. A confirmed model-unavailable 404 or explicitly model-specific quota rejection taking at most ten seconds does not consume the two-attempt budget, but always consumes the four-request budget. Slower rejections consume both. It stops on timeouts, shared/unknown quota, auth/request rejection and truncated/blocked responses. One costly fallback remains for unavailable services or unusable JSON. Only 404 responses explicitly naming the requested model as unavailable are cached; missing-file/unidentified 404s stop conservatively. Known unavailable models are skipped on subsequent students using the same client. Quota classification reads the SDK structured QuotaFailure violations. Every violation must name the requested model, a PerModel quota ID and a Gemini generation quota metric; mixed, incomplete or unfamiliar scopes fail closed. Shared/unknown quota blocks the client; confirmed model quota blocks only that model. Error text alone never authorizes quota fallback. Client-local cooldowns respect Retry-After without sleeping; clients never share account state. Cooldowns expire with the client and do not persist across fresh runs. The deadline is checked between calls/responses; the SDK HTTP timeout is not a hard process cancellation guarantee.

This deliberately trades some fallback availability for bounded latency and request consumption. A teacher may need to retry manually after ambiguous timeouts or shared/unknown quota errors. All four configured models remain reachable after quick, confirmed unavailable/model-quota responses; fresh clients obey the same finite limits. Faster failure is not faster completed marking.

Duplicate user instructions were removed. The full system instructions, response schema, official scheme evidence, student working, mark breakdown, correct method and full solution remain. No thinking reduction or output token cap is imposed. No automatic marks change is introduced. Arithmetic/transcription review and Classroom pathways retain their existing behavior.

Requests for one account/client remain serial, including concurrent callers. More concurrency or splitting students/questions would increase quota pressure, duplicate PDF context and risk losing cross-question evidence. Server-side Batch is not introduced: the current pricing table does not offer its model pricing on the free tier. No paid feature or new network probe is added.

## Reproducible offline comparison

Run python tests/benchmark_marking_requests.py. It executes the frozen original grade_with_model and current function with identical synthetic ten-question inputs and an injected clock/provider. Captured results: tests/marking_benchmark_results.json.

| Measurement | Before | After |
| --- | ---: | ---: |
| User prompt characters | 2,370 | 1,295 |
| System characters | 3,335 | 3,335 |
| Schema characters | 1,544 | 1,544 |
| Total instruction/schema characters, excluding PDFs | 7,249 | 6,174 |
| Successful marking calls | 1 | 1 |
| Simulated successful response | 60 s | 60 s |
| All-timeout failure calls / simulated time | 4 / 480 s | 1 / 120 s |
| All-503 failure calls / simulated time | 4 / 4 s | 2 / 2 s |
| Unknown-quota 429 failure calls / simulated time | 4 / 4 s | 1 / 1 s |
| Model-quota then successful fallback | 2 / 61 s | 2 / 61 s |
| Two unavailable models then success | 3 / 62 s | 3 / 62 s |

The user prompt shrinks 45.4%; total text above shrinks 14.8%. These are exact character counts, not tokenizer measurements, and exclude potentially dominant PDF tokens. Synthetic response length is unchanged. Failure timings are controlled simulations, not observed service latency. No successful-marking speedup is claimed.

Recovery regressions exercise model-specific/shared/unknown/mixed/malformed quotas, two unavailable models followed by success, repeated fresh clients, all four configured models, duplicate inputs, a 20-model list capped at four calls, slow rejections, 503 recovery, 504 stopping and content-free quota diagnostics. Intercepted real-SDK tests verify the actual provider error shape and disable inherited retries.

Existing digital and raster handwriting-style PDF benchmarks also completed. Original uploads and inspection behavior are unchanged; the existing metadata optimization remains two generations to one, and completed repeat runs make no requests. This is a preserved PR #9 benefit, not a new improvement here. Local PDF timing varies with machine load; it does not explain cloud generation latency.

## Private diagnostics

Set MATH_MARKER_DIAGNOSTICS=1 in the server environment to enable the math_marker.marking logger. Keep these logs restricted to deployment administrators and apply the deployment's retention policy. It is off by default. One content-free record is also retained in client memory for the most recent request, then discarded with the client.

Records contain requested and returned model version, response/queue/total elapsed seconds, attempt count, configured timeout, outcome/status, cooldown durations/skips, input/schema/response character counts, attachment count and available prompt/output/thought/cache token counts and prompt modality totals. They never contain prompts, answers, raw exceptions, filenames, file identifiers, student identifiers or keys. Random request IDs have no student mapping. Records are not added to student reports, exports or ordinary UI. Users see simple progress and elapsed time. Measurements cover the whole generate call (not streaming time-to-first-token); missing SDK usage is omitted.

Interpret a single slow successful call as provider/model/context work, then compare its token counts. Multiple attempts indicate fallback overhead. High thought/output counts identify hypotheses for controlled accuracy testing, not permission to reduce reasoning. Cooldown skips and zero-attempt runs identify rate limiting. No counter infers actual billing.

## Validation and remaining work

Local validation: 176 regression tests, 8 Streamlit interaction tests and 7 real-SDK/intercepted-HTTP tests passed, plus syntax validation and all four benchmark scripts. The earlier revision had one local cold-start timeout; its rerun and the fallback-fix validation passed. CI independently runs these suites.

Tests exercise the real controller, original attachment/schema retention, correct/incorrect/partial score normalization, evidence safeguards, bounded errors/fallbacks, privacy, cooldown isolation, serialization, SDK timeout/retry configuration and completed-run caching. Synthetic model responses test application handling; they do not establish Gemini accuracy after prompt changes.

Before claiming a live improvement, use a confirmed test-only environment/account and synthetic digital and handwriting PDFs. Alternate baseline/current runs with the same free-tier model, question selection and audit setting; record median/tail main-call latency, token counts, attempts and valid completed-result rate within an explicit request cap. Independently compare each awarded/lost method mark and scheme evidence. Do not mix cache hits or audit durations into main-call speed. Investigate any scoring disagreement before release. Lower thinking, tighter output limits and PDF splitting remain unverified and are not enabled.

Official references checked for the investigation:
- https://ai.google.dev/gemini-api/docs/pricing
- https://ai.google.dev/gemini-api/docs/generate-content/thinking
- https://googleapis.github.io/python-genai/
