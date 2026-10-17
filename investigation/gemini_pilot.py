"""Evaluation-only Gemini pilot runner (Phase B4 Stage 3B-1). OFFLINE BUILD.

This module is never called by the application. It imports no production
marking code (app5_1_automated.py, marking_requests.py) and performs no
Gemini SDK import at module load; SDK objects are created only inside the
live-mode default factories. Default mode is a dry-run that renders the
plan and prompts WITHOUT any network, credential or environment access.

Safety gates (see run_live()):
- live mode requires MATH_MARKER_EVAL_OPT_IN=1 AND an explicitly confirmed
  request budget equal to the exact case count (--confirm-request-budget 5);
- exactly ONE request per case, ONE model, at most five requests total;
- no automatic retries, no fallback, no parallelism, no model substitution;
- an unavailable-model response aborts the remaining requests immediately;
- a wrong opaque-ID echo fails that case instead of guessing an assignment;
- dry runs never read credentials or environment variables.

The live path is fully implemented but was NOT executed in Stage 3B-1; all
its behavior is covered by mocked-client offline tests only.

Structured-output contract: reuse of
investigation.marking_adapter.EVALUATION_RESPONSE_SCHEMA with ONE documented
evaluation-only transformation: "seconds" is removed from the model-visible
schema (a type-union field is a structured-output risk; latency is measured
by the runner and injected into the parsed response as monitoring metadata
before adapter conversion; the adapter accepts any valid seconds value).

Prompt information flow: user prompts are built ONLY from
investigation.accuracy.prompt_case() fields plus a deterministic opaque case
ID (PILOT-01..05). Canonical case IDs (e.g. "algebra-partial"), expected
awards, uncertain flags, derivations and governance labels never appear in
any prompt. The opaque->canonical mapping stays inside this process: the
model must echo the opaque ID; the runner swaps the canonical ID back in
before adapter conversion, so adapter/reporting alignment stays exact.
"""
import argparse
import copy
import datetime
import hashlib
import json
import math
import re
import subprocess
import sys
import time
from pathlib import Path

from investigation import accuracy, marking_adapter, marking_metrics

ARTIFACT_VERSION = "3b.1"
PILOT_CASE_IDS = (
    "algebra-correct",
    "algebra-partial",
    "probability-tree-arithmetic-error",
    "follow-through",
    "geometry-conflicting-methods",
)
MAX_PILOT_REQUESTS = 5
OPT_IN_ENV = "MATH_MARKER_EVAL_OPT_IN"
API_KEY_ENV = "GEMINI_API_KEY"
DEFAULT_MODEL = "gemini-3.8-flash"
REQUEST_TIMEOUT_MS = 120_000
# Generation settings are pinned explicitly. DOCUMENTED DIVERGENCE from
# production marking (which sets no temperature); revisit before Stage 3B-2.
PILOT_GENERATION_SETTINGS = {"temperature": 0.0, "max_output_tokens": 2048}

MODEL_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.\-]{2,80}$")
_MODEL_VERSION_PATTERN = re.compile(r"gemini-[a-z0-9.\-]{1,80}")
_HTTP_CODE_PATTERN = re.compile(r"\b(400|401|403|404|408|429|500|502|503|504)\b")


# ---------------------------------------------------------------------------
# Reproducibility metadata
# ---------------------------------------------------------------------------

def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def benchmark_sha256():
    return sha256_file(Path(accuracy.__file__).with_name("accuracy_cases.json"))


def governance_sha256():
    return sha256_file(Path(marking_metrics.__file__).with_name("case_governance.json"))


def _sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def git_head_sha():
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip() or "unavailable"
    except Exception:
        return "unavailable"


# ---------------------------------------------------------------------------
# Prompt construction (corrected Stage 3A prompts)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are an evaluation-only marking judge for Pearson Edexcel
International GCSE Mathematics A (4MA1) benchmark material. You are NOT an
official Pearson examiner. Some benchmark rubric decisions are explicitly
synthetic and provisional: apply each criterion text exactly as supplied
and treat its wording as the authority for that criterion. Never import
external Pearson conventions that a criterion does not state, and never
let these general rules override a criterion-specific convention.

CORE RULES
1. Judge EVERY listed criterion independently from the student's visible
   work. Return exactly one row per listed criterion, echoing its unique
   criterion_id. Never skip, merge or add rows.
2. Award a criterion only when THIS student's evidence satisfies the
   criterion text. A solution you could construct never justifies an
   award. Distinguish two kinds of criteria:
   (a) criteria whose text credits an implied or equivalent method ("or
       equivalent method") may be earned even without every written step,
       when the student's result and shown context satisfy the stated
       condition;
   (b) criteria whose text demands visible, stated or shown working are
       not earned when the demanded working is missing. Missing required
       work is normally a withheld mark ("awarded": false, "unknown":
       false), NOT an uncertainty.
3. Absent required working is evidence of absence (withheld mark).
   Unreadable, ambiguous or self-contradictory evidence that might
   otherwise earn the criterion must be declared uncertain instead:
   "unknown": true together with "awarded": false, and the case-level
   "uncertain" flag set to true. NEVER return "awarded": true together
   with "unknown": true. When "unknown" is false, "awarded" must still be
   declared.
4. Preserve dependencies and follow-through rules stated in the criterion
   text (for example "Dependent on earning <criterion_id>", "follow
   through", "their <value>", "or equivalent"). A dependent or
   follow-through criterion may only be earned when its prerequisite is
   satisfied by THIS student's work.
5. An arithmetic slip in an otherwise valid method does not defeat a
   method criterion when the criterion text preserves it (e.g. "even if
   a product was evaluated incorrectly"); it normally withholds the
   matching accuracy criterion.
6. Crossed-out work is generally not scoreable, EXCEPT where the
   criterion text or benchmark convention says otherwise (for example
   crossed-out work with no replacement remains eligible). Apply exactly
   what the material states.
7. Do not guess unreadable notation and do not invent student working,
   official answers or maximum marks.
8. A correct final result or a plausible-looking answer alone never earns
   a criterion; award strictly from the student's evidence under the
   criterion text.
9. JSON only. Echo "id" exactly as given in CASE ID. Set the case-level
   "uncertain" flag to true if ANY row is declared unknown. If you cannot
   mark at all, return
   {"id": "<CASE ID>", "failed": true, "uncertain": <bool>}
   and nothing else.

The application's evaluation code validates every response by criterion
ID; reported marks must follow these rules exactly.
"""

USER_PROMPT_TEMPLATE = """\
CASE ID: {case_id}
TOPIC: {topic}

QUESTION:
{question}

STUDENT WORK (synthetic transcription; judge exactly what is written):
{student}

MARKING CRITERIA ({n} criteria, {total_marks} marks available):
{criteria_block}

TASK
For EACH of the {n} criteria above decide "awarded" (true or false) from
the student's work, and instead declare "unknown": true with "awarded":
false only when the evidence is unreadable, ambiguous or cannot be
adjudicated. Echo "code" and "weight" for every row. Give a short "reason"
per row citing the visible evidence or the applicable criterion rule. Set
the case-level "uncertain" flag to true if ANY row is declared unknown.
Echo "id" exactly as CASE ID above.
{schema_instruction}"""

SCHEMA_INSTRUCTION = """\

Return one JSON object matching this structure:
{"id": "<CASE ID>", "failed": false, "uncertain": <bool>,
 "breakdown": [{"criterion_id": "<id>", "awarded": <bool>,
                "unknown": <bool>, "code": "<code>", "weight": <int>,
                "reason": "<text>"}, ...]}
with exactly one breakdown row per criterion above."""


def opaque_case_id(index):
    return f"PILOT-{index + 1:02d}"


def opaque_ids(cases):
    return {case["id"]: opaque_case_id(i) for i, case in enumerate(cases)}


def build_user_prompt(prompt_view, opaque_id):
    """prompt_view comes from accuracy.prompt_case() (labels stripped upstream)."""
    scheme = prompt_view["scheme"]
    criteria_block = "\n".join(
        f"  [{c['id']}] {c['code']} ({c['weight']} mark)"
        f"{'s' if c['weight'] != 1 else ''} - {c['criterion']}"
        for c in scheme)
    return USER_PROMPT_TEMPLATE.format(
        case_id=opaque_id,
        topic=prompt_view.get("topic", ""),
        question=prompt_view["question"],
        student=prompt_view["student"],
        n=len(scheme),
        total_marks=sum(c["weight"] for c in scheme),
        criteria_block=criteria_block,
        schema_instruction=SCHEMA_INSTRUCTION,
    )


def load_pilot_cases(case_data=None):
    gold = case_data if case_data is not None else accuracy.cases()
    by_id = {c["id"]: c for c in gold}
    missing = [cid for cid in PILOT_CASE_IDS if cid not in by_id]
    if missing:
        raise ValueError(f"Pilot cases missing from benchmark: {missing}")
    return [by_id[cid] for cid in PILOT_CASE_IDS]


def evaluation_response_schema():
    """EVALUATION_RESPONSE_SCHEMA minus 'seconds' (evaluation-only transform)."""
    schema = copy.deepcopy(marking_adapter.EVALUATION_RESPONSE_SCHEMA)
    schema["properties"].pop("seconds", None)
    return schema


def render_prompts(cases):
    """Return the model-visible prompts for the pilot (dry-run + live shared)."""
    mapping = opaque_ids(cases)
    rendered = []
    for index, case in enumerate(cases):
        prompt_view = accuracy.prompt_case(case)
        opaque = opaque_case_id(index)
        rendered.append({
            "canonical_case_id": case["id"],
            "opaque_case_id": opaque,
            "system_prompt": SYSTEM_PROMPT,
            "user_prompt": build_user_prompt(prompt_view, opaque),
        })
    return rendered


# ---------------------------------------------------------------------------
# Strict parsing / validation
# ---------------------------------------------------------------------------

def _strip_code_fences(text):
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned


def parse_model_json(text):
    """Strict JSON parse; raises with a content-free rejection category."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty_response")
    try:
        parsed = json.loads(_strip_code_fences(text))
    except json.JSONDecodeError as exc:
        raise ValueError("invalid_json") from exc
    if not isinstance(parsed, dict):
        raise ValueError("invalid_json")
    return parsed


def validate_against_schema(value, schema):
    """Strict subset validator: object/array/boolean/string/number/integer."""
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            return False
        for key in schema.get("required", ()):  # declared required keys
            if key not in value:
                return False
        properties = schema.get("properties", {})
        for key, sub_schema in properties.items():
            if key in value and not validate_against_schema(value[key], sub_schema):
                return False
        return True
    if expected == "array":
        if not isinstance(value, list):
            return False
        items = schema.get("items")
        return items is None or all(validate_against_schema(v, items) for v in value)
    if expected == "boolean":
        return type(value) is bool
    if expected == "string":
        return type(value) is str
    if expected == "integer":
        return type(value) is int and not isinstance(value, bool)
    if expected == "number":
        return type(value) in (int, float) and not isinstance(value, bool) \
            and math.isfinite(value)
    # Unknown construct: fail closed.
    return False


def classify_failure(exc, requested_model):
    """Map an SDK exception to a content-free failure category."""
    code = getattr(exc, "code", None)
    if code is None:
        code = getattr(exc, "status_code", None)
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    text = str(exc)
    if code == 404 and re.search(
            r"(?<![\w/])models/" + re.escape(requested_model)
            + r"\s+(?:is\s+)?(?:not found|not supported)\b", text, re.I):
        return "model_unavailable"
    if (isinstance(exc, TimeoutError) or code in (408, 504)
            or "timeout" in type(exc).__name__.lower()):
        return "timeout"
    if code == 429:
        return "quota_limited"
    if code in (401, 403, 400):
        return "request_rejected"
    if code in (500, 502, 503):
        return "service_unavailable"
    return "transport_or_client_error"


def _final_text_response(response):
    """Minimal finish-reason inspection; content-free outcome categories."""
    candidates = getattr(response, "candidates", None) or []
    finish = getattr(candidates[0], "finish_reason", None) if candidates else None
    finish_value = getattr(finish, "value", finish)
    block = getattr(getattr(response, "prompt_feedback", None), "block_reason", None)
    block_value = getattr(block, "value", block)
    return finish_value, block_value


def _usage_facts(response):
    usage = getattr(response, "usage_metadata", None)
    facts = {}
    if usage is None:
        return facts
    for key in ("prompt_token_count", "candidates_token_count",
                "total_token_count"):
        value = getattr(usage, key, None)
        if type(value) is int and 0 <= value <= 10 ** 9:
            facts[key] = value
    return facts


# ---------------------------------------------------------------------------
# Live execution (implemented; NOT executed in Stage 3B-1)
# ---------------------------------------------------------------------------

def _default_client_factory():
    import os
    api_key = os.environ.get(API_KEY_ENV, "").strip()
    if not api_key:
        raise RuntimeError(f"{API_KEY_ENV} is not set; refusing to start live mode")
    from google import genai
    from google.genai import types
    return genai.Client(api_key=api_key,
                        http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS))


def _default_config_factory(**kwargs):
    from google.genai import types
    return types.GenerateContentConfig(**kwargs)


def run_live(cases, manifest, *, model=DEFAULT_MODEL,
             client_factory=None, config_factory=None, clock=time,
             opt_in=None, budget_confirmation=None):
    """Execute the five-case pilot sequentially. HARD GATES, no network here.

    client_factory/config_factory: dependency-injected for tests; the
    defaults construct SDK objects. opt_in: environment gate value; the
    default reads OPT_IN_ENV (live mode only). budget_confirmation must
    equal the exact case count; live mode refuses otherwise.
    """
    if not MODEL_ID_PATTERN.fullmatch(model):
        raise ValueError("Invalid requested model ID")
    if len(cases) > MAX_PILOT_REQUESTS:
        raise ValueError(
            f"Pilot budget exceeded: at most {MAX_PILOT_REQUESTS} cases per run")
    if budget_confirmation != len(cases):
        raise RuntimeError(
            "Request budget must be explicitly confirmed as the exact case count")
    opt_value = opt_in if opt_in is not None else _read_opt_in()
    if opt_value != "1":
        raise RuntimeError(
            f"Live evaluation requires {OPT_IN_ENV}=1 operator opt-in")
    if not cases:
        raise ValueError("Live run requires at least one case")

    prompts = render_prompts(cases)
    mapping = {p["canonical_case_id"]: p["opaque_case_id"] for p in prompts}
    schema = evaluation_response_schema()
    config_factory = config_factory or _default_config_factory
    client_factory = client_factory or _default_client_factory
    client = client_factory()

    ledger = []
    predictions = []
    stop_reason = None          # "model_unavailable" | "program_error"
    generation_settings = dict(PILOT_GENERATION_SETTINGS)

    for index, (case, prompt) in enumerate(zip(cases, prompts)):
        case_id = case["id"]
        attempt_number = index + 1
        if attempt_number > MAX_PILOT_REQUESTS:
            ledger.append(_skipped_entry(case_id, mapping[case_id],
                                         "request_budget_exhausted"))
            continue
        if stop_reason is not None:
            ledger.append(_skipped_entry(
                case_id, mapping[case_id],
                "aborted_after_model_unavailable" if stop_reason == "model_unavailable"
                else "aborted_after_program_error"))
            continue

        entry = {
            "case_id": case_id,
            "opaque_case_id": prompt["opaque_case_id"],
            "requested_model": model,
            "attempt_number": attempt_number,
            "attempted": True,
            "outcome": "parsed",
            "failure_category": None,
            "http_status": None,
            "actual_model_version": None,
            "finish_reason": None,
            "prompt_fingerprint": _sha256_text(prompt["user_prompt"]),
            "system_prompt_fingerprint": _sha256_text(prompt["system_prompt"]),
            "latency_seconds": None,
            "usage": None,
        }
        started = clock.monotonic()
        try:
            response = client.models.generate_content(
                model=model,
                contents=[prompt["user_prompt"]],
                config=config_factory(
                    system_instruction=prompt["system_prompt"],
                    response_mime_type="application/json",
                    response_schema=copy.deepcopy(schema),
                    **generation_settings,
                    http_options={"timeout": REQUEST_TIMEOUT_MS,
                                  "retry_options": {"attempts": 1}},
                ),
            )
        except Exception as exc:  # single attempt; never retried, never substituted
            entry["latency_seconds"] = round(clock.monotonic() - started, 3)
            category = classify_failure(exc, model)
            entry["outcome"] = "failed"
            entry["failure_category"] = category
            ledger.append(entry)
            if category == "model_unavailable":
                stop_reason = "model_unavailable"
            continue
        entry["latency_seconds"] = round(clock.monotonic() - started, 3)
        entry["actual_model_version"] = _sanitize_version(response)
        entry["usage"] = _usage_facts(response) or None
        finish_value, block_value = _final_text_response(response)
        entry["finish_reason"] = block_value or finish_value
        if block_value and block_value not in ("BLOCK_REASON_UNSPECIFIED",):
            entry["outcome"] = "failed"
            entry["failure_category"] = "blocked_response"
            ledger.append(entry)
            continue
        if finish_value not in (None, "STOP"):
            entry["outcome"] = "failed"
            entry["failure_category"] = ("truncated_response"
                                          if finish_value == "MAX_TOKENS"
                                          else "blocked_response")
            ledger.append(entry)
            continue

        text = getattr(response, "text", None)
        try:
            parsed = parse_model_json(text)
        except ValueError as parse_failure:
            entry["outcome"] = "failed"
            entry["failure_category"] = str(parse_failure)
            entry["json_valid"] = False
            ledger.append(entry)
            continue
        entry["json_valid"] = True

        expected_opaque = prompt["opaque_case_id"]
        if parsed.get("id") != expected_opaque:
            entry["outcome"] = "failed"
            entry["failure_category"] = "id_mismatch"
            entry["opaque_id_echo_ok"] = False
            ledger.append(entry)
            continue
        entry["opaque_id_echo_ok"] = True

        if not validate_against_schema(parsed, schema):
            entry["outcome"] = "failed"
            entry["failure_category"] = "schema_validation_failure"
            entry["schema_valid"] = False
            ledger.append(entry)
            continue
        entry["schema_valid"] = True

        # Canonical alignment: swap the echoed opaque ID back for the adapter.
        parsed["id"] = case_id
        parsed.setdefault("seconds", None)
        parsed["seconds"] = round(entry["latency_seconds"], 6)
        try:
            prediction = marking_adapter.prediction_from_response(
                parsed, {case_id: case["scheme"]})
        except ValueError as batch_error:  # programmer error; abort defensively
            entry["outcome"] = "failed"
            entry["failure_category"] = "adapter_batch_error"
            ledger.append(entry)
            stop_reason = "program_error"
            continue
        entry["adapter_failure_reason"] = (prediction.get("failure_reason")
                                           if prediction.get("failed") else None)
        predictions.append(prediction)
        ledger.append(entry)

    artifact = build_artifact(cases, manifest, model=model, mode="live",
                              ledger=ledger, predictions=predictions,
                              stop_reason=stop_reason)
    return artifact


def _sanitize_version(response):
    version = getattr(response, "model_version", None)
    if isinstance(version, str) and _MODEL_VERSION_PATTERN.fullmatch(version):
        return version
    return None


def _read_opt_in():
    import os
    return os.environ.get(OPT_IN_ENV)


def _skipped_entry(case_id, opaque, reason):
    return {"case_id": case_id, "opaque_case_id": opaque,
            "requested_model": None, "attempt_number": None,
            "attempted": False, "outcome": "skipped",
            "failure_category": reason}


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------

def build_artifact(cases, manifest, *, model, mode, ledger, predictions,
                   stop_reason):
    report = marking_metrics.build_report(cases, predictions, manifest)
    legacy = accuracy.evaluate(predictions, case_data=cases)
    prompts = render_prompts(cases)
    generation_settings = dict(PILOT_GENERATION_SETTINGS)
    try:
        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds")
    except Exception:  # pragma: no cover - clock failure must not lose results
        timestamp = None
    return {
        "artifact_version": ARTIFACT_VERSION,
        "mode": mode,
        "timestamp_utc": timestamp,
        "git_commit_sha": git_head_sha(),
        "benchmark_sha256": benchmark_sha256(),
        "governance_sha256": governance_sha256(),
        "system_prompt_sha256": _sha256_text(SYSTEM_PROMPT),
        "user_prompt_template_sha256": _sha256_text(USER_PROMPT_TEMPLATE),
        "evaluation_response_schema": evaluation_response_schema(),
        "requested_model": model,
        "generation_settings": generation_settings,
        "max_requests": MAX_PILOT_REQUESTS,
        "aborted_after": stop_reason,
        "opaque_id_map": opaque_ids(cases),
        "cases": ledger,
        "predictions": predictions,
        "report": report,
        "legacy_evaluator": legacy,
        "notes": [
            "Value-free pilot: five synthetic cases cannot establish overall "
            "model marking accuracy.",
            "All pilot findings are synthetic-benchmark results; provisional "
            "rubric cases are never presented as verified Pearson decisions.",
            "The legacy accuracy.evaluate() output is unchanged and separate "
            "from Stage 2B abstention-aware metrics.",
        ],
    }


# ---------------------------------------------------------------------------
# Dry-run mode (default; no network, no credentials, no environment reads)
# ---------------------------------------------------------------------------

def build_preview(case_data=None):
    cases = load_pilot_cases(case_data)
    prompts = render_prompts(cases)
    return {
        "artifact_version": ARTIFACT_VERSION,
        "mode": "dry-run",
        "git_commit_sha": git_head_sha(),
        "benchmark_sha256": benchmark_sha256(),
        "governance_sha256": governance_sha256(),
        "system_prompt_sha256": _sha256_text(SYSTEM_PROMPT),
        "user_prompt_template_sha256": _sha256_text(USER_PROMPT_TEMPLATE),
        "evaluation_response_schema": evaluation_response_schema(),
        "requested_model": DEFAULT_MODEL,
        "generation_settings": dict(PILOT_GENERATION_SETTINGS),
        "pilot_case_ids": list(PILOT_CASE_IDS),
        "opaque_id_map": opaque_ids(cases),
        "max_requests": MAX_PILOT_REQUESTS,
        "prompts_rendered": prompts,
        "live_gates": {
            "opt_in_env": OPT_IN_ENV,
            "budget_confirmation_required": len(PILOT_CASE_IDS),
            "retries": 0, "fallbacks": 0, "parallelism": 0,
            "model_substitution": "never",
            "abort_on_model_unavailable": True,
        },
        "status": "dry-run-no-requests-made",
    }


def main(argv=None):
    """CLI: default is --dry-run; --live is gated and never run in Stage 3B-1."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live", action="store_true",
                        help="gated live mode; requires opt-in env + budget flag")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--confirm-request-budget", type=int, default=None)
    args = parser.parse_args(argv)

    cases = load_pilot_cases()
    governance = marking_metrics.load_governance()
    if not args.live:
        preview = build_preview(cases)
        print(json.dumps(preview, sort_keys=True, indent=2))
        return 0

    artifact = run_live(cases, governance, model=args.model,
                        budget_confirmation=args.confirm_request_budget)
    out = Path("evaluation_runs") / (
        f"{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        f"-gemini-pilot.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"artifact": str(out), "report_version":
                      artifact["report"]["report_version"],
                      "prediction_status":
                      artifact["report"]["prediction_status"]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
