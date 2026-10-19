# Gemini deadline recovery and performance investigation

## Evidence and root cause

Baseline: main `fb3abfe84346647b555ebabfa336586b29410c8f`, containing PR14.
Production reported one HTTP504 in 120.053 seconds, total120.054 seconds, zero queueing,
zero retry sleeps, two document references, 4,646 input text characters and a
1,544-character schema. Almost all the recorded marking-controller duration was the
generation call. These metrics do not include earlier file preparation/upload.

PR14 explicitly stopped after every 504 because a timeout might represent a request
already accepted by the provider. Its configured three attempts did not override
that safety rule. The SDK request timeout was 120,000 milliseconds and SDK retries
were explicitly limited to one attempt. SDK2.31.0 also derives X-Server-Timeout from
HttpOptions.timeout. The observed boundary is consistent with that deadline, but
does not identify the original provider/transport cause. The old diagnostic record
does not prove that Gemini returned structured DEADLINE_EXCEEDED rather than a
wrapper reporting 504.

There is no evidence that PDF size, reasoning tokens, schema complexity, prompt
length, network faults or model capacity individually caused this failure. Successful
responses already expose token counts when supplied; failed responses need not.
No live requests were made, and no accuracy or latency claim is inferred from mocks.

References:
- https://ai.google.dev/gemini-api/docs/generate-content/api-errors
- https://raw.githubusercontent.com/googleapis/python-genai/v2.31.0/google/genai/_api_client.py
- https://www.python-httpx.org/exceptions/

## Targeted changes

- A received HTTP504 plus structured numeric code504 and status DEADLINE_EXCEEDED
  permits at most one alternative-model request. Message text alone never qualifies.
- First request remains capped at120s. The single post-deadline request is capped
  at45s, further reduced by the remaining180s scheduling budget. At least15s must
  remain after the wait. There is no second120s deadline-recovery request.
- Existing jittered backoff/Retry-After handling and combined30s wait cap remain.
  Configured1–3 generation attempts and absolute4 HTTP requests remain binding.
  A fast404 on the post-deadline request also consumes that sole fallback.
- Confirmed deadline models are skipped for180s within the same client. This avoids
  immediately sending subsequent students to a model that just exhausted a deadline.
  Fresh clients are independent. Shared/unknown429 and ambiguous read/write failures
  remain conservative. No extra simultaneous marking requests are introduced.
- This is a bounded availability trade-off, not an idempotency guarantee: the failed
  provider attempt may have consumed quota or compute. The additional call can also
  consume quota. No abandoned/partial response is saved and no remote cancellation is
  assumed. Operators can set MATH_MARKER_MAX_ATTEMPTS=1 to disable automatic fallback.
- Private stage records separate local preparation, upload, file processing,
  Classroom download, generation, audit and validation durations. Marking attempts
  additionally separate API wait and response parsing, classify timeout evidence,
  and record bounded fallback decisions. Student jobs share their random support
  reference with generation records; nested document jobs have a parent reference.
  Manual preparation/upload workers have their own records because uploads precede
  individual marking jobs. No identifying document names or data are logged.
- Manual progress now forwards generation/fallback and validation stages to the main
  Streamlit thread with elapsed time. Classroom shows batch elapsed time and evidence
  checking. Existing preparation/upload and completion messages remain.

## Accuracy and unchanged features

No changes to prompts, response schema, thinking configuration, output-token limits,
PDF bytes, attachment selection, official scheme extraction, arithmetic checks,
method/follow-through/partial-credit rules, second-pass audit, or teacher approval.
No new models, paid features or concurrent generations. Existing session-scoped
reference/metadata/result caches and expiration checks are retained. No PDF
compression, rasterization or evidence removal is introduced. Reordering models or
reducing reasoning without live teacher-scored evidence was rejected.

Existing sequential Classroom processing continues after individual failures; only
successful revisions enter the completed set. Failed rows have no result or suggested
grade. Existing historical snapshots and teacher overrides are preserved. No grade
publication behavior changes. Session-only caches/history still disappear on restart.

## Reproducible offline comparison

Run `python tests/benchmark_deadline_recovery.py`. The frozen fixture is PR14's
controller; both sides use identical scripted outcomes and virtual time. A successful
model takes20s, confirmed deadline failures consume the supplied request timeout,
and jitter is fixed at0. These are simulations, not production measurements.

| Scenario | PR14 outcome / requests / seconds | Proposed outcome / requests / seconds |
|---|---|---|
| Healthy first model | success /1 /20 | success /1 /20 |
| Primary deadline, healthy alternative | failure /1 /120 | success /2 /145 |
| Persistent provider deadlines | failure /1 /120 | failure /2 /170 |
| Ambiguous timeout | failure /1 /120 | failure /1 /120 |
| Two sequential students, primary deadline | both fail /2 /240 | both succeed /3 /165 |

The two-student script avoids75s of repeated waiting (31.25%) while making one extra
request. It does not measure a live success rate. Persistent failure is50s slower;
normal successful marking and ambiguous-timeout latency are unchanged. The45s/15s
recovery limits are conservative policy bounds, not empirically optimal thresholds.
All request timeouts are SDK/transport limits rather than hard wall-clock process
termination guarantees; the controller checks its deadline between calls.

Run all regression tests, Streamlit tests, intercepted SDK tests, and all seven
`tests/benchmark_*.py` scripts. CI covers Python3.11/SDK1.40.0 and Python3.14 with
SDK2.30.1/Streamlit1.65.0 and SDK2.31.0/Streamlit1.66.0. Synthetic handwritten-style
and digital PDF inspection benchmarks are retained. Local import/processing timings
are machine-dependent; do not infer Gemini latency from them.

## Production validation still required

Local validation: 235 regression tests, 11 Streamlit interaction tests and 10 SDK
tests passed (256 distinct tests); the SDK suite also passed on minimum version
1.40.0. All seven offline benchmarks passed. Local Python was3.12; CI additionally
covers3.11 and3.14. Initial Streamlit runs hit a20s cold-import timeout; the fixture
now imports pandas before its UI interaction deadline and accepts the new progress
callback. CI pins PyArrow24 to match the dependency replacement visible in the supplied
Community Cloud logs. This is not a change to the application's dependency policy.

After human review and an authorized test deployment, use synthetic PDFs and a
strict free-tier request cap to check provider timeout categories, latency/token
distributions by model, and teacher-scored correct/incorrect/partial solutions.
Evaluate whether45s fallback often truncates otherwise successful jobs before changing
it. A structured provider error does not prove all upstream computation has stopped.
The initial120s wait remains where no earlier failure evidence is available.
