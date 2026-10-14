# PR14: bounded automatic recovery

Baseline: merged PR13, main `8d4b79f03bb85dfa7e69bf581ac3e5374f301097`.
Investigation and tests: 14 October 2026. All Gemini traffic in tests is mocked
or intercepted locally. No live requests, billing changes, deployment or grades.

## What the production evidence establishes

The reported run received two HTTP 503 responses, waited five seconds, then
stopped at the configured two-generation limit after 23.479 seconds. That policy
prevented trying the existing third model. The log does not establish why Google
returned 503, whether the third model would have succeeded, or whether the problem
was widespread. The PR13 controller behaved as designed, but its recovery budget
was too restrictive to reach every existing fallback after two service failures.

Offline reproduction also identifies a batch limitation: service cooldown checks
could reject a later student immediately without allowing a short recovery window.
The batch already catches individual failures and retains completed revisions;
the problem was the shared client's short cooldown, not an exception escaping the
batch loop. Both paths now have direct regression coverage.

## Model verification and accuracy

Official pages checked:
[3.8 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash),
[3.6 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.6-flash), and
[3.5 Flash-Lite](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite).
All three configured IDs are documented with PDF and structured-output support.
[Google pricing](https://ai.google.dev/gemini-api/docs/pricing) lists free-tier
input/output for them. No model is added, removed or reordered in this PR; the
redirected 3.5 Flash alias was already removed in PR13. Documentation does not
prove account-specific availability or quota. No model-discovery calls were made.

Flash-Lite was already configured, but will now be reached after two transient
failures. Its real handwritten marking accuracy has not been measured here.
All original PDFs, scheme evidence, prompt/schema instructions, mathematical
validation, uncertainty flags, partial credit, second-pass audit and teacher
approval rules remain unchanged. AI results are not automatically verified.
No model chooser exists in the current main UI; existing configured lists and
the grade function's supplied model order remain supported without UI redesign.

## One coordinated policy

- Default maximum: three non-fast-rejection attempts when a failure permits
  fallback (server/connection errors, confirmed model-specific quota or missing
  models). Each distinct supplied model is called at most once.
- Keep an absolute four-request cap, including quick confirmed model-unavailable
  or model-scoped quota rejections. With three default models, at most three calls.
- Keep the two-attempt ceiling for repeated invalid/empty output. Never retry
  truncated/blocked results, authentication errors or rejected input.
- HTTP 500/502/503 and known connection-establishment failures use backoff of
  5–6 seconds, then 10–11 seconds (uniform 0–1 second jitter). Retry-After seconds
  and HTTP dates are honored when longer. The third failure can set a 20–21s
  cooldown for the next student but does not cause a fourth generation.
- All sleeps combined are capped at 30 seconds per marking job. Long Retry-After
  values stop safely instead of being shortened. Cooldowns remain client-isolated.
- If every configured model has a short temporary cooldown, a later student can
  wait for the earliest one. Quota and 404 cooldowns are never treated this way.
- HTTPX ConnectTimeout, ConnectError and PoolTimeout, plus native connection
  refusal, can fall back. Read/write/protocol failures and ambiguous timeouts
  remain terminal because the request might already have reached the provider.
- Shared/unknown 429 still stops and sets an account cooldown. Only reliably
  structured model-specific quota permits another model. No rapid quota retries.
- Keep 120-second request timeouts and a 180-second scheduling budget including
  waits. SDK internal retries remain disabled, and marking remains serialized
  per client. No independent retry loops multiply requests.

The operator may set top-level Streamlit Secret or environment variable
`MATH_MARKER_MAX_ATTEMPTS = "1"`, `"2"` or `"3"`; default is `"3"`.
Secrets take precedence; invalid values use the bounded default. This setting
cannot raise hard limits. It controls non-fast-rejection attempts, not the separate
four-request absolute ceiling. It does not configure second-pass audits/uploads.

The 180 seconds remains a scheduling/transport-timeout budget, not a hard process
kill: queue time is measured separately, and the OS/transport can overshoot.
The controller never starts another request once its deadline is reached.
The cap applies per student, not to an entire multi-student batch.

## Offline performance comparison

`python tests/benchmark_automatic_recovery.py` runs the frozen PR13 implementation
and current code with identical scripted errors and virtual timing. Successful
generation costs 60 seconds, each failure one second, jitter is fixed at 0.5s.

| Scenario | PR13 | PR14 | Requests before / after |
| --- | --- | --- | --- |
| Immediate success | Completed, 60s | Completed, 60s | 1 / 1 |
| Two 503s then healthy third | Failed, 7s | Completed, 78s | 2 / 3 |
| All models return 503 | Failed, 7s | Failed, 19s | 2 / 3 |
| Connection failure then recovery | Failed, 1s | Completed, 66.5s | 1 / 2 |
| Failed student then provider recovery | Both fail, 7s | First fails, second completes, 99.5s | 2 / 4 across two students |

These demonstrate controlled recovery opportunities and their cost, not live
speed or success rates. Persistent outages now take longer and may use one more
request. Original document preparation, expiration checks, reference/result
caching and cleanup are unchanged. Automatic fallback reuses the same attachments.
Manual retries still require student reupload after cleanup; optional retained
references can be reused if valid. No extra student retention was introduced.

## Validation and remaining limits

Tests cover default and conservative configurable limits, 503 recovery/exhaustion,
exponential jitter, Retry-After, timeout budgets, safe/ambiguous network failures,
400/401/403/404/429/500/502/504 behavior, model-specific/shared quota, privacy,
client isolation, cache reuse and actual application marking/validation paths.
The actual Classroom batch test runs a successful student, a failed student,
then another success; a subsequent retry processes only the failed revision and
preserves the first student's teacher override. SDK transport tests run on both
supported versions with real serialization and locally intercepted HTTP.

All existing regression, Streamlit, SDK and five benchmarks are rerun, plus the
new PR13/current benchmark. One initial local Streamlit run hit its existing
20-second startup timeout; it is rerun without changing the timeout or app UI.
Final local results: 221 regression, 11 Streamlit and 9 SDK tests pass (241 distinct
tests; 250 executions including both SDK versions). All six offline benchmarks
and syntax validation pass. The full Streamlit rerun passed without code changes
after the initial startup timeout. See the PR for CI status.

Unknowns remain: Google's underlying 503 cause, account-specific capacity, and
live accuracy/latency on original scans. Connection errors can also be permanent,
so bounded fallback may not help. Manual runs create fresh clients and do not
inherit previous cooldowns. Session-only caches/history are lost on restart.
No retry can eliminate external outages; approval and live synthetic testing
remain necessary before making production reliability claims.

References: [Google retry guidance](https://ai.google.dev/gemini-api/docs/troubleshooting)
and [HTTPX exception categories](https://www.python-httpx.org/exceptions/).
