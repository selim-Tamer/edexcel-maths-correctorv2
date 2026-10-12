# Transcription and audit reliability investigation

## Findings

The supplied Q29 expression totals **1026**, not 1040. The earlier source-page investigation supported 1040 using different products. The current report builder and normalization copy the AI working text; they do not recalculate products or replace numbers. A synthetic regression confirms this data flow. This points to model transcription/reconstruction, not a report arithmetic formatter. The historical raw model response is unavailable, so visual misreading cannot be separated conclusively from invented intermediate working. No student error is inferred.

For Q9, `67 + 1 / 2` is 67.5, whereas `(67 + 1) / 2` is 34. Lost grouping during transcription is plausible, but the supplied fragment alone cannot prove what is on the page. Normalization, Markdown export and Streamlit preserve supplied parentheses in the tests. The old arithmetic tokenizer could not check grouped expressions completely: even `(67 + 1) / 2 = 35` escaped detection. The new tokenizer supports up to three grouping levels and checks operator precedence. It never inserts missing parentheses or rewrites the transcript.

The previous audit initialized every selected question to “No usable audit response.” Exceptions were collected into a local list that was discarded. All failed model attempts or all models on cooldown therefore produced the same message. Simple rejected proposals already had separate messages. The historical failure cannot now be attributed to quota, invalid JSON, token exhaustion or model availability without the discarded run details.

Other confirmed weaknesses: finish reasons and usage metadata were ignored, even parseable output marked MAX_TOKENS could be accepted, all proposal fields were required even for an unresolved question, and SDK-internal retries were not explicitly disabled. Token exhaustion is a possible failure mode, not a confirmed explanation of the historical report.

## Changes

- Marking instructions require literal visible working, preserved fraction grouping and a separate transcription-uncertainty field. Reconstructed calculations belong in the model solution. Uncertainty forces source review without deducting credit. The question UI shows both the final answer and working rather than hiding the working behind a nonempty answer.
- The audit receives uncertainty together with the previous evidence. An unresolved answer needs only its question identifier, classification and reason; it need not invent a score or source citation. Complete proposals still require source evidence and consistent method/answer-mark decisions. Contradictory proposed solutions, or purportedly corrected transcripts with contradictory arithmetic, stay unresolved.
- Audit generation uses JSON Schema with an 8192-output-token cap and a 120-second per-request timeout. There are at most two attempts across distinct configured models; SDK HTTP retries are disabled. Quota, authentication, rejected requests, timeouts, blocked/incomplete output and truncation stop immediately. Empty/malformed JSON or transport/unavailable-model failures can use the remaining single fallback. Missing/invalid proposal evidence does not trigger more calls.
- Parsed SDK dictionaries and JSON text/fences are supported. Wrong envelopes are distinguished from JSON parse errors. Truncated/blocked output is rejected before examining potentially usable partial JSON.

Original scores and mark breakdowns remain unchanged by audits. Valid method credit is retained in proposals for a teacher to inspect. No automatic grade publication or approval is added.

## Safe diagnostics

Each question's `audit_review.diagnostics` is visible in the question report and machine-readable JSON and survives normalization/exports. It contains only attempt number, a configured model name (otherwise a fixed placeholder), numeric HTTP status, allowlisted finish reason, token counts, response character count and a fixed outcome code. No raw response, request, exception text, credentials, student text or file identifiers are copied into diagnostics. Evidence/proposals remain separate report content, not telemetry; there is no external logging service.

Useful outcomes include `quota_limited`, `authentication_failed`, `request_rejected`, `model_unavailable`, `timeout`, `transport_or_sdk_error`, `truncated_response`, `blocked_or_incomplete_response`, `invalid_json`, `invalid_response_shape`, `empty_response` and `all_models_on_cooldown`. A received envelope can still leave a question unresolved: `validation_code` and the fixed reason identify missing/duplicate reviews or rejected evidence. Token counts distinguish output-budget exhaustion from parse failure when the provider supplies them.

## Compatibility and validation limits

Google documents structured output for the configured 3.8 Flash, 3.6 Flash and 3.5 Flash-Lite models. The support table does not establish availability of every configured fallback, including 3.5 Flash, for a particular account. Model names/order are unchanged; unavailable responses now have an explicit diagnostic. See [Google's structured-output guide](https://ai.google.dev/gemini-api/docs/generate-content/structured-output).

The SDK documents `response_json_schema` as an alternative to `response_schema`, and retry `attempts` includes the initial request. The repository's minimum SDK, **google-genai 1.40.0**, is tested using its real request serialization and response parsing with HTTP intercepted by a local mock transport. Tests cover schema round-trip, quota/no retry and MAX_TOKENS rejection. See [Google Gen AI SDK reference](https://googleapis.github.io/python-genai/genai.html).

Run:

```
python -m py_compile app5_1_automated.py document_pipeline.py
python -m unittest discover -s tests -p 'test_*.py' -v
python -m unittest discover -s tests/ui -p 'test_*.py' -v
python -m unittest discover -s tests/sdk -p 'test_*.py' -v
python tests/benchmark_document_pipeline.py
```

Fixtures are synthetic. The new regressions call production normalization, arithmetic parsing, audit generation/response handling, evidence validation and report functions; the SDK tests intercept only HTTP. The pre-fix source fails the grouped-arithmetic and truncated-response regressions. PDF caching, privacy scopes and processing concurrency are unchanged.

These checks do not measure live Gemini transcription/marking accuracy, actual model access, service latency or how often the output cap is reached. Those require a confirmed test-only account/environment and synthetic documents. The app does not repair previously exported reports. Version 3.3.15 invalidates versioned result-cache identities for new marking runs.
