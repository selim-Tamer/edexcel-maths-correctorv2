# Streamlit marking diagnostics

Baseline: main 8337f7e19980fc6dbada9c0f868904e32a88c3d7 (merged PR #11).

## Findings

Streamlit 1.65.0 promotes top-level string/int/float secrets to environment variables when its secrets store is parsed. A top-level MATH_MARKER_DIAGNOSTICS = "1" should therefore be visible to os.getenv after loading st.secrets. The application already reads st.secrets for its server API key before marking. A nested TOML entry is not the same setting. The new offline integration test loads a synthetic local secrets file and verifies actual environment promotion; no deployed secrets were read.

The prior custom logger set INFO but installed no handler. It depended on the Python root logger, while Streamlit configures console handlers for its own loggers. With no root handler, INFO records disappear (the logging last-resort handler normally handles WARNING and above). Existing assertLogs tests installed a capture handler themselves and could not establish Cloud-console visibility. This is a reproducible defect, but actual deployed logger configuration was not inspected, so it is not definitive proof of the production cause.

## Fix

On the Streamlit script thread, before marking workers, read only the diagnostic switch directly from st.secrets with an environment fallback. Explicit secrets take precedence. No secret value is printed. A dedicated math_marker.marking INFO StreamHandler writes to stderr independently of the root logger; propagation is disabled to avoid duplicate root output. Configuration is idempotent across reruns and protected by a lock. No global logging level or HTTP/SDK debug logging is enabled.

The first app script run (and an enabled-state change) emits exactly this kind of server-side line:

    marking_diagnostics_startup {"enabled": true}

When disabled, the same line contains false and request records are suppressed. Repeated unchanged reruns do not repeat the startup line. Configuration and logging failures cannot interrupt startup or discard successful marks. Console output is the intended Community Cloud log path; live Cloud delivery has not been tested or deployed here.

Enabled marking requests emit one marking_metrics JSON record. The UI support reference matches request_id. Failure records contain failure_category, stop_reason, attempted model names, per-attempt HTTP status when known, elapsed/response/queue times, timeout/cooldown durations and attempt counts. No HTTP status is invented for a local timeout or a zero-attempt cooldown. Available token counts remain metadata only. No student names, answers, PDF bytes/URIs, credentials, raw responses or exception strings are logged. Logging is server-only; reports and browser progress do not receive telemetry.

## What the observed failure means

The exact message "The marking service was temporarily unavailable" maps to service_unavailable. In this application's controller, that category follows a final attempt classified as 500, 502 or 503. Status is read from exception attributes, with a legacy numeric-message fallback. It does not establish which of those statuses occurred, which model failed, whether both attempts failed the same way, or why the provider failed. A support reference is a correlation ID, not a database lookup; a record that was never emitted cannot be reconstructed from the reference.

Google documents 500 as an internal server error and 503 as a service that may be overloaded/unavailable. Provider overload is a hypothesis until the safe request records identify the actual status/model/timings. This message is distinct from this controller's quota, timeout and invalid-response messages. No live Gemini requests, model changes, increased retries or paid features were used to investigate it.

After a maintainer separately approves merging/deployment, inspect the startup line first. False means the switch did not resolve to the enabled value; true confirms configuration and console emission at that point. For an independently authorized future marking run, match its support reference to request_id in private Cloud logs. Share only the safe metadata; do not enable raw HTTP logging or paste secrets/student work. Retain logs only as long as needed and restrict them to deployment administrators.

## Offline validation

Tests capture actual stderr with an empty/root-warning logger configuration, rather than relying solely on assertLogs. Coverage includes success, repeated 503 failure, reference correlation, statuses/models/counts/timings, disabled diagnostics, secret precedence, absent secrets, startup/rerun deduplication, handler setup failure, and real Streamlit secret promotion using a temporary synthetic TOML file. Existing SDK tests intercept HTTP for google-genai 1.40.0 and 2.30.1.

Marking rules, original attachments, scoring, teacher review, historical snapshots, request limits (four total, two costly attempts, 180-second scheduling budget), timeout stopping and PDF caches are unchanged. Faster marking and resolution of provider failures are not claimed.

References:
- https://docs.streamlit.io/develop/concepts/connections/secrets-management
- https://github.com/streamlit/streamlit/blob/1.65.0/lib/streamlit/logger.py
- https://docs.python.org/3/library/logging.html#logging.lastResort
- https://ai.google.dev/gemini-api/docs/generate-content/api-errors

Validation results: 196 regression tests, 11 Streamlit tests and 7 SDK tests passed on each of google-genai 1.40.0 and 2.30.1 (214 distinct tests, 221 executions counting both SDK versions). All four offline benchmarks passed. A test-fixture patch initially tried to modify an immutable Secrets instance; patching the class corrected the fixture, and the complete Streamlit suite passed.
