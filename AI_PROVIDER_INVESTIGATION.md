# Gemini availability and alternative providers

Investigation date: 2026-10-16. Baseline: main `fb3abfe84346647b555ebabfa336586b29410c8f` (PR #14). PR #15 was not modified or merged. This PR adds independent tools and fixtures only. No production configuration, marking instructions, attachments, validation, caching, authentication, Classroom or grade-publication behavior changes.

## Conclusions

The supplied logs establish failed generation requests, not their underlying infrastructure cause. No live requests were made. No account access, current project quota, billing state, or live marking accuracy was verified. The three configured model IDs are documented by Google; obsolete model names are not an established cause. Keep the existing Gemini integration pending controlled diagnostics. Do not add retries or change providers on this evidence alone.

Groq is the most promising non-Google candidate for a small synthetic vision evaluation. It is not yet a safe full-paper replacement. OpenRouter requires more model, routing, parser-cost and privacy checks. Cerebras currently requires a payment method for its trial and does not meet the no-card requirement. There is insufficient evidence to name a best model for examination accuracy or measured speed.

## What production evidence establishes

| Evidence | Established | Not established |
| --- | --- | --- |
| Oct 14: 503 from 3.8 Flash after 6.47 s and 3.6 Flash after 12.01 s; total 23.48 s | Both attempted generations failed; response waits sum to 18.48 s. The remaining approximately 5 s is compatible with retry waiting. | Permanent outage; all accounts affected; quota exhaustion; precise source of the remaining time; PDF cause. |
| Oct 15: 504 after 120.05 s, one attempt, no sleep, stop reason timeout | Generation reached the configured 120 s boundary. PR14 deliberately stops on timeouts even when its configured attempt limit is three. | Whether Google's processing, a gateway, client transport or capacity caused the deadline; whether another model would succeed. |
| Two documents, 4,646 input text characters, schema 1,544 characters | The request carries more context than the small diagnostics. | PDF bytes/pages, image token count, thinking tokens, output length, actual inference versus transport time. |

HTTP 503 is compatible with transient service capacity problems; 504 is compatible with a deadline. Neither is evidence of an authentication error or an explicit quota rejection. Account-specific availability still cannot be excluded. A successful upload does not prove successful generation. The support references identify attempts, not their causes. The [Google troubleshooting guide](https://ai.google.dev/gemini-api/docs/troubleshooting) provides general failure guidance, not a diagnosis of these particular requests. The [status dashboard](https://aistudio.google.com/status) did not expose readable incident history during this investigation; no historical outage conclusion is drawn.

### Inspected pipeline

`app5_1_automated.py` supplies the original student PDF, official scheme and optional question PDF to marking. Selected questions and maxima add text. Reference documents use existing hash/expiry-aware caching; student documents retain their existing isolation policy. Preparation and upload precede generation. Classroom processing remains sequential, and failed results cannot become approved marks through these investigation tools.

`marking_requests.py` bounds scheduling at 180 s, individual requests at 120 s, accumulated retry waiting at 30 s and all requests at four. The configured generation attempt limit is 1–3. Its per-request SDK retry override uses one attempt, so the application's explicit controller owns retries. PR14's conservative timeout stop is intentional, not a failure to read the attempt setting. PR15's proposed recovery policy is a separate change and is not incorporated here.

The SDK can translate its timeout into a server timeout header as well as transport configuration; see the versioned [google-genai 2.31.0 client source](https://raw.githubusercontent.com/googleapis/python-genai/v2.31.0/google/genai/_api_client.py). Thus the 120 s boundary alone cannot separate a local timeout from server processing reaching a communicated deadline. There is no evidence here of hidden repeated SDK generations. No independently verified production defect was found that justifies changing the marking pipeline in this PR.

### Reproducible local request profile

Run `python -m investigation.profile`. Counts below describe serialized local requests, not Gemini tokens or inference timings.

| Request | Text characters | Schema characters | PDF bytes |
| --- | ---: | ---: | --- |
| Production base prompt before dynamic question selection | 3,861 | 1,544 | Unknown for the failed submission |
| Observed production log | 4,646 | 1,544 | Unknown |
| Diagnostic A, arithmetic | 47 | 82 | None |
| Diagnostic B, two-mark algebra | 424 | 408 | None |
| Diagnostic C, digital | 313 | 408 | 1,563 + 1,665 |
| Diagnostic C, vector strokes | 313 | 408 | 2,128 + 1,665 |

Production has 13 question-level output fields and potentially many questions. Diagnostics have one tiny task. No explicit thinking-budget or output-token-cap override is present in the production generation configuration inspected here. Do not infer excessive thinking from that absence. Document bytes do not translate directly into visual token usage. Diagnostics C uses inline PDFs to isolate document reading in one request; it does **not** exercise production Files API upload, processing or cached references. A separate authorized experiment would be needed to isolate that path. Prompt, schema, document and output complexity are plausible contributors, not demonstrated causes. No mathematical evidence was removed to reduce latency.

## Current Gemini documentation verification

All three IDs appear in the current [model catalog](https://ai.google.dev/gemini-api/docs/models). Each individual model page documents PDF and image input, text output, structured outputs and thinking:

| ID | Documented capabilities | Access actually tested |
| --- | --- | --- |
| [gemini-3.8-flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash) | PDF/image understanding, JSON, reasoning; low/medium/high thinking levels, not minimal | No |
| [gemini-3.6-flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.6-flash) | PDF/image understanding, JSON, reasoning | No |
| [gemini-3.5-flash-lite](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite) | PDF/image understanding, JSON, reasoning; advertised efficiency makes it a speed-test candidate | No |

The [pricing page](https://ai.google.dev/gemini-api/docs/pricing) currently lists free standard input/output tiers for these IDs. Free eligibility is not a guarantee of free requests on a billed project. Paid-only catalog additions are excluded from the diagnostic allowlist. Recheck pricing on the execution date. [Rate limits](https://ai.google.dev/gemini-api/docs/rate-limits) depend on project/tier and available capacity; consult the test project's AI Studio limits instead of assuming a universal RPM allowance. [PDF support](https://ai.google.dev/gemini-api/docs/document-processing) is documented, but handwritten mathematics grading accuracy is not established by modality support.

## Safe diagnostics

See [operator instructions](investigation/README.md). The default is offline. The live command requires an interactive terminal, today's pricing attestation, confirmation of an unbilled test-only project and a hidden operator-entered key. Environment variables, Streamlit secrets and production credentials are not discovered. No uploaded file path or arbitrary prompt is accepted. There are no retries, fallback, model discovery calls, tools or grade writes.

Each invocation permits **one generation request for one model and one level**, with a 5–60 s transport timeout and a parent-process watchdog (timeout plus 2 s startup allowance and bounded cleanup). Local termination cannot guarantee cancellation of remote computation. Do not automatically repeat a timed-out diagnostic. A complete A/B/C comparison across three models would require nine separately authorized requests, selecting one PDF variant for C; another variant or repetition is additional testing. No such requests have been made.

The explicit fixed Google endpoint, disabled redirects/proxy-environment discovery and zero transport retries reduce accidental credential forwarding and hidden requests. Output contains allowlisted status, model identity, numeric usage, duration, structure validity and agreement flags. Raw prompts, PDFs, model answers, keys and exception messages are not logged. Responses are bounded to 1 MiB. These are local developer tools, not a public web service. Normal operating-system memory and process-security precautions still apply.

The script **cannot inspect billing**. Its live gates prevent unattended execution, not a dishonest or mistaken operator using a paid account. Zero-cost operation requires independently checking an unbilled project and current model eligibility. Do not enable live mode until authorized. It never calls another provider.

### Interpretation sequence

1. Authorize A first: establishes a tiny request can reach that model and return JSON. A failure does not alone identify account versus service versus network.
2. B adds criterion-based synthetic marking. Success requires valid evidence fields and consistent method/accuracy awards; arithmetic correctness alone is insufficient.
3. C adds two tiny PDFs. Digital versus vector-stroke differences can inform a modality hypothesis, but these clean glyphs are not representative real handwriting.
4. If A/B/C succeed but production fails, investigate full request shape, Files API references, document volume and output complexity with separately authorized synthetic experiments.
5. If even A consistently fails, examine project access/limits and provider status before changing PDF processing. A matched REST/SDK or local/Cloud comparison could isolate transport differences, but requires explicit additional requests. Do not combine account, model, input and timeout changes in one experiment.

## Alternative providers

Capabilities below are documentation claims, not independent accuracy, latency or uptime measurements. All can technically be called from Streamlit through server-side HTTP. None has been integrated.

| Candidate | Free / card requirement | PDF and vision | JSON | Limits and reliability | Privacy | Suitability |
| --- | --- | --- | --- | --- | --- | --- |
| Gemini configured Flash models | Documented free API tier; verify unbilled project | Native PDF and images | Structured schema | Account/tier limits; observed failures; no free availability guarantee | Unpaid-service data-use restrictions below | Best fit with current architecture, accuracy unverified |
| Groq Qwen `qwen/qwen3.8-27b` | Free plan; payment required to upgrade, do not upgrade | Images; PDF page rendering needed | Strict schema documented | 30 RPM, 1,000 RPD, 8,000 TPM, 200,000 TPD; max 3 images/request. No measured reliability here | Default non-retention has exceptions; ZDR available | Medium; first backup candidate for small evidence-complete page sets |
| OpenRouter pinned free vision model | Restricted free plan, no payment needed for its allowance | Depends on model; native PDF or parsing/rendering | Depends on model | Free 20 RPM/50 RPD without purchasing credits; variable availability | Router and downstream policies both matter | Medium research candidate; no specific free vision route verified here |
| Cerebras Qwen `qwen-3.8-27b` | $5/30-day trial, verified payment method required; not renewable free | Image input supported; PDF conversion needed | Structured output | Trial limits; no independent availability measurements | Contract/policy scope needs review | Excluded by no-card constraint |
| Hugging Face Inference Providers | Free-user $0.10 monthly credits, subject to change; no purchase proposed | Provider/model-dependent vision; PDF conversion or provider-specific support | Provider/model-dependent | Tiny credit allowance; provider limits | HF routing policy plus downstream policies | Low for sustained classroom marking; possible micro-evaluation |

### Provider evidence and important constraints

**Groq:** [Vision documentation](https://console.groq.com/docs/vision) lists Qwen vision, three-image maximum, 20 MB image request limit and image token accounting. [Structured outputs](https://console.groq.com/docs/structured-outputs) lists strict-schema support; required fields and `additionalProperties: false` constraints need a schema adapter. Their combination on this application's schema is untested. [Free rate-limit table](https://console.groq.com/docs/rate-limits) supplies the figures above. Three images alone can consume much of the 8,000 TPM budget before scheme and answer text. [Billing FAQ](https://console.groq.com/docs/billing-faqs) distinguishes free access from upgrading with payment. Do not silently split away necessary working to fit three images.

Groq's [data policy](https://console.groq.com/docs/your-data) describes non-retention by default, with reliability/abuse exceptions up to 30 days and optional zero data retention. Metadata remains. This is not a blanket conclusion about every contractual training use or persistence feature; review the applicable agreement before sending student data.

**OpenRouter:** [Free routing](https://openrouter.ai/docs/guides/routing/model-variants/free) and [limits](https://openrouter.ai/docs/api_reference/limits) describe restricted availability. The higher daily allowance requires buying credits and is outside scope. [Pricing](https://openrouter.ai/pricing/) shows a free plan but marks data-policy-based routing unavailable there, while [provider privacy documentation](https://openrouter.ai/docs/guides/privacy/provider-logging) describes account privacy filters. Verify actual free-plan controls rather than assuming that these filters or ZDR are available.

Its [PDF documentation](https://openrouter.ai/docs/guides/overview/multimodal/pdfs) distinguishes native handling from parsing: the non-native default can use paid Mistral OCR. A free Markdown parser does not establish preservation of handwritten work or diagrams. Extracted images also have limits. Do not use automatic parsing or a random free router for grading. A future zero-cost test needs a pinned eligible vision model, verified routing and local page rendering or verified native PDF support. Model-specific image/JSON capabilities must be checked in the [catalog](https://openrouter.ai/docs/guides/overview/models).

**Cerebras:** Current [rate-limit/trial documentation](https://inference-docs.cerebras.ai/support/rate-limits) requires a verified payment method and describes expiring credits. Older descriptions of a permanent free tier must not guide this project. [Image inputs](https://inference-docs.cerebras.ai/capabilities/image-inputs) and [structured outputs](https://inference-docs.cerebras.ai/capabilities/structured-outputs) are available capabilities, not proof of marking accuracy. [Privacy policy](https://www.cerebras.ai/privacy-policy) describes inference-data handling; no account or contract was evaluated. This provider is excluded regardless of performance claims.

**Hugging Face:** [Pricing](https://huggingface.co/docs/inference-providers/pricing) documents the small monthly credit. [Security documentation](https://huggingface.co/docs/inference-providers/security) says routing does not store request/response bodies or use them for training, with limited debugging metadata retention. Downstream provider terms still apply. No specific vision model, card-free onboarding flow or PDF/JSON combination was independently tested.

**Student privacy:** Google's [Gemini API terms](https://ai.google.dev/gemini-api/terms) describe unpaid-service content use for product/model improvement and potential human review, and warn against sensitive/confidential/personal data. Regional exceptions exist for the EEA, Switzerland and UK. This investigation does not establish which policy applies to the user's account. Synthetic-only testing avoids this issue here; free-tier availability alone is not a basis for sending identifiable student work. File expiry is not a general guarantee about training or retention. A future teacher-facing service needs a separate privacy assessment.

## Proposed separate-PR architecture

Keep provider selection outside grading logic. Define an adapter accepting an immutable evidence bundle (student, question and official scheme references), the marking schema and a shared request/time budget. Return either a fully validated normalized result or a typed failure. Keep original provenance and uncertainty in the result. Provider-specific schema conversions must not remove method marks or evidence fields.

Use a capability gate before any backup: configured test-approved provider, confirmed no-billing account, permitted privacy policy, supported images/schema, complete relevant pages within its limits and remaining global budget. Otherwise stop for teacher review. Render original pages locally without OCR substitution; do not remove diagrams or handwritten working. Page selection requires evidence that question and scheme context remains complete. The three-image Groq limit can make fallback infeasible for full papers.

Keep sequential per-student processing and one active generation. Share limits across provider attempts rather than resetting the budget. Never switch after an ambiguous timeout automatically. Handle explicit unavailable responses only under a separately reviewed policy. Do not merge partial responses from failed generations. Apply existing arithmetic, transcription, evidence and teacher-review checks to every provider. Preserve successful assignment/student/revision results and overrides; no automatic approval or publication. Scope caches by session, account, provider, model, evidence hash and expiry; never reuse credentials or document handles across providers. The adapter and policy are a proposal, not implemented here.

## Reproducible accuracy evaluation

`investigation/accuracy_cases.json` contains 13 original synthetic fixtures spanning algebra, geometry, trigonometry, graph gradients, statistics, multistep work, partial credit, follow-through, wrong methods, valid alternative methods, rounding and unreadable evidence. Gold awards and their mathematical derivations were authored independently of any model output. They are **not official Pearson schemes and have not been independently teacher-moderated**. Obtain blinded examiner review before treating them as validated gold labels.

`prompt_case` excludes gold awards/derivations. The offline evaluator reports total-score agreement separately from criterion-vector accuracy: a right total can hide wrongly awarded method marks. It also reports method-mark agreement, false awards, incorrect deductions, uncertainty detection, latency and failure rate. Missing/invalid results remain in denominators. Ambiguous gold criteria are excluded from exact-mark comparisons and require uncertainty; they are not silently treated as zero. Metric self-tests using gold-shaped inputs only test arithmetic in the evaluator; they are not model results.

For a future authorized benchmark: freeze and teacher-moderate the rubric, create balanced unseen correct/partial/incorrect variants, and present the same complete evidence to each model. Add original synthetic graph/geometry diagrams and realistic handwritten/raster variants with legibility labels. Current textual graph/geometry cases and clean vector strokes do not cover that domain adequately. Record model/version, settings, input representation, latency, failures and criterion decisions. Use paired question comparisons, confidence intervals and multiple authorized runs to measure stochastic variability, keeping a holdout set. Adjudicate disputed evidence blind to provider. Report failure-inclusive coverage and accuracy rather than only successful responses. Require teacher approval before any operational grade use.

## Offline validation and remaining work

See `investigation/offline_results.json` for measured local results. All tests use synthetic data or mocked transports. New tests exercise the actual diagnostic request construction, HTTP client, schema validation, process deadline, privacy filtering, PDF generation, profile extraction and accuracy evaluator. SDK tests validate configuration against installed SDK types. Existing application, Streamlit and SDK suites remain applicable and unchanged in behavior.

No live Gemini, Groq, OpenRouter, Cerebras or Hugging Face requests were performed. No provider latency improvement, real handwriting accuracy, account quota, free-tier billing status or production recovery is claimed. Local test duration is not API response time.

Recommended next steps: retain Gemini as the provisional primary because it already supports the complete PDF workflow; authorize the staged A/B/C investigation on a separate unbilled test project; compare 3.8/3.6 for quality and 3.5 Flash Lite as a speed candidate only after gold review. Evaluate Groq next on evidence-complete small synthetic documents. Consider OpenRouter only after pinning a suitable free vision route and resolving privacy/parser-cost controls. Do not merge PR15 on the assumption that this investigation proves its recovery succeeds.
