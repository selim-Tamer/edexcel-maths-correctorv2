# PDF preparation performance

## Baseline and scope

Baseline: PR #4 at 6169dd996aff57a603e2f3520049e0b845907fd6. Its manual flow uploaded the scheme, optional question paper and every student file, extracted the scheme, then checked the marking-result cache. Ordinary Streamlit reruns were already button-gated; repeated **Mark** clicks still incurred all preparation requests. The baseline upload lifecycle and preparation statements are preserved in tests/fixtures/upload_baseline.py for reproducible comparison.

The optimized flow checks successful results first, uploads only missing students and required references, and reuses successful scheme extraction/question inventory. Random mode still makes a fresh question selection; selected question IDs are part of result identity. Failed marking, incomplete audits and failed scheme extraction are not reused. Marking prompts, model lists, validation, audit order and teacher review semantics are unchanged.

## Measured offline comparison

Run: python tests/benchmark_document_pipeline.py

This uses production functions and a fake provider with a virtual clock: upload 2s, processing 10s, each status GET 50ms. These are controlled measurements of scheduling and request counts, **not live Gemini or browser latency**. No network, models, OAuth or credits are used.

| Scenario | PR #4 | Optimized |
| --- | ---: | ---: |
| Cold single document, virtual elapsed | 12.45s | 13.85s |
| Cold document status GETs | 19 | 7 |
| Warm reference, virtual elapsed | 12.45s (new upload) | 0.05s (validated handle) |
| Warm reference upload calls | 1 | 0 |
| Repeat fully marked batch: student + scheme + question paper | 3 uploads + 1 scheme generation | 0 uploads + 0 status GETs + 0 generations |

Cold polling uses 0.5s, 1s, then 2s intervals: 63% fewer GETs in this scenario, with 1.40s extra detection delay. It is deliberately **not claimed as a cold-upload speedup**. At most two required uploads overlap (previous cap eight), and marking remains one student at a time. Large cold batches may take longer than the old burst of eight uploads; reducing quota pressure and eliminating repeat work take priority. Per-file service times cannot be summed to claim concurrent end-to-end latency. Performance tests enforce the request savings and a two-second maximum additional detection delay in the controlled scenario.

## Stage measurements and limitations

- Browser to Streamlit: the native uploader delivers bytes before application code receives the UploadedFile. No supported backend upload-start timestamp is exposed. The UI explicitly reports received MiB, **not a fabricated upload duration**. On a future authorized synthetic test deployment, measure the file upload request in browser Network timing from send to completion; do not include time spent choosing a file or waiting to click Mark. Never export an unredacted HAR containing tokens or student content.
- Local preparation: monotonic timing around temporary-file creation/write; exact bytes preserved. Hash/cache lookup has its own timing.
- Gemini transfer: measured around the SDK files.upload call, including the SDK's resumable-upload protocol. One SDK call can involve multiple HTTP requests; the benchmark counts SDK upload calls separately from explicit status GETs.
- Gemini processing: measured from upload response to terminal state, including poll/retry wait and status-request latency.
- Marking preparation: scheme extraction and question inventory measured separately; cache hits labeled explicitly.
- Manual preparation runs off the UI thread, uses a queue, updates elapsed display without API polling, and shows a completed-stage table. Classroom reference preparation uses the same table; attachment download/transfer/processing elapsed messages use its existing worker queue.

## Cache privacy and lifecycle

Caches live only in this Streamlit browser session; no shared st.cache_data or st.cache_resource. Credential hash and signed-in identity scope the session; role/content SHA-256 and Classroom assignment context scope reference entries. Student result identity includes document position/name/hash, scheme/question hashes, audit setting, selected questions, version and configured models. Distinct students do not borrow remote handles even for identical bytes. Returned cached results are deep copies so teacher edits cannot mutate the AI cache.

Remote reuse is **opt-in and reference-only**, off by default. Student PDFs/images are always deleted after the run. When enabled, reference entries have a non-sliding ten-minute TTL, capacity eight, provider-expiration guard of 60 seconds, and one bounded metadata GET before reuse. Missing/failed/expired files are reuploaded; auth/quota errors propagate rather than creating more files. Unknown expiration means immediate cleanup, never persistent reuse. Parsed metadata/results have ten-minute TTLs and 128-entry bounds. Clear-cache, key/account change, logout, capacity eviction and expired-entry cleanup delete references best-effort. Active leases prevent TTL/capacity eviction during marking. Expired entries are purged on the next interaction/acquisition/close, not by a background scheduler. Closing a browser tab or a crashed server cannot guarantee immediate remote deletion; retained files can remain until Gemini's provider expiry. Deletion failures are counted and surfaced in the sidebar on the next render.

No PDF bytes are retained inside the file-handle cache. The SDK client is retained only with its session's reference entries so cleanup can use the correct credentials; no credential is logged or exported. Cached scheme contents and marking results are session data, not a public cache.

## Timeouts, retry budget and cleanup

Processing has a 180s monotonic deadline. Each status request is capped at the lesser of 30s and the remaining budget. Retry only 429/500/502/503/504 status requests, at most three attempts per status check, respect Retry-After seconds/date, and stop if backoff would exhaust the deadline. Per-request SDK retries are disabled to prevent multiplication of this budget. Failed/unknown terminal state and processing timeout delete the known remote file and always remove the local temporary copy. All batch workers are joined before cleanup, including late successes.

An ambiguous upload POST is never retried automatically: it could have created a remote file without returning its ID. SDK transfer requests have a 120s request timeout; a multi-request/resumable transfer is not falsely represented as a strict 120s end-to-end deadline. SDK/network timeouts and failed deletes remain best-effort service boundaries. The provider's automatic expiry is the final cleanup fallback for unknown orphan IDs.

## PDF size and splitting decision

No automatic recompression, rasterization, image downsampling, page removal or splitting is enabled. Splitting can separate a question from its working and requires reliable page-to-question mapping, additional requests and reconciliation of maxima; this is inappropriate as a blind upload optimization. Smaller bytes can help network transfer but do not prove faster Gemini interpretation of the same page count.

A separate local-only experiment used supplied reference PDFs to compare lossless content-stream compression and duplicate-object removal. All pages were rendered before/after at 108 DPI and compared pixel-for-pixel, with visual spot checks. No supplied PDF, filename, content, image or local analysis output is included in this PR. Savings were modest for a scan and negative for a compact scheme, supporting the decision not to transform originals automatically. Pixel identity at one resolution is not an OCR/marking-accuracy claim; no Gemini marking was performed. Runtime uploads and regression tests preserve bytes exactly.

## Classroom requests intentionally retained

Unchanged turned-in revisions still return before Gemini work. Drive revision metadata reads detect edits that Classroom timestamps miss; download-time metadata checks enforce current MIME/export/download permissions. Although both call files.get, they serve different freshness/authorization purposes; this change does not remove the latter based on an earlier stale snapshot. No real Classroom reads/writes were made during testing, and no grade publication path was added.

## Validation

The stdlib suite covers byte preservation, timeout/retry limits, active/no-wait handling, reference expiry and deletion, credential/account/session/assignment/owner separation, concurrent same-owner requests, bounded caches, partial/full result hits, changed inputs/configuration, random selection, failed audits, failed preparation cleanup, and benchmark budgets. Four real Streamlit AppTest tests cover upload-free ordinary reruns, repeated Mark clicks, teacher override callbacks and historical Classroom approval controls. AppTest still uses synthetic files and mocked services.

Live browser-to-server latency, Gemini processing/generation latency, provider acceptance, end-to-end OAuth and actual marking accuracy remain unmeasured. Live calls were explicitly disallowed for this work. No new paid feature or context-cache API is used; existing model selection and free-tier quota handling remain unchanged.

## Primary documentation

- [Gemini Files API and automatic expiry](https://ai.google.dev/gemini-api/docs/files)
- [File state and expiration metadata](https://ai.google.dev/api/files)
- [PDF document processing and limits](https://ai.google.dev/gemini-api/docs/document-processing)
- [Streamlit file uploader lifecycle](https://docs.streamlit.io/develop/api-reference/widgets/st.file_uploader)
- [Streamlit uploaded-file memory](https://docs.streamlit.io/knowledge-base/using-streamlit/where-file-uploader-store-when-deleted)
- [Minimum supported SDK request config](https://github.com/googleapis/python-genai/blob/v1.40.0/google/genai/types.py)
- [SDK retry and resumable-upload implementation](https://github.com/googleapis/python-genai/blob/v1.40.0/google/genai/_api_client.py)
