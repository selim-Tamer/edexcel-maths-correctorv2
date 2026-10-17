"""Evaluation-only Gemini pilot runner (Phase B4 Stage 3B-2 hardened). OFFLINE BUILD.

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
- STRICT abort policy: the remaining requests are aborted immediately after
  401 authentication failures, 403 permission failures, model-unavailable
  404 responses, 429 quota failures, configuration errors (including
  unsupported generation parameters), ambiguous timeouts (408/504) and
  ambiguous transport errors, where a retried or repeated request could
  duplicate spend. Only qualified service-side 5xx responses keep the
  pilot moving to the next case (still without any retry);
- a wrong opaque-ID echo fails that case instead of guessing an assignment;
- dry runs never read credentials or environment variables.

Generation settings: the temperature parameter was REMOVED after Google's
Gemini 3.x migration guidance deprecated it across the 3 family (it is
ignored today and errors in future generations). Determinism for this
evaluation comes from the explicit-rules system instruction. Thinking is
explicitly configured at the documented "medium" level. Thinking tokens
count toward max_output_tokens=2048 (billed as output tokens); the budget
was assessed as sufficient for the small structured marking responses the
schema requests, and MAX_TOKENS truncation is handled as a per-case
"truncated_response" failure, never a silent retry.

The live path is fully implemented but was NOT executed; all its behavior
is verified offline through dependency-injected mock factories.

A request budget of five applies to the WHOLE PILOT CAMPAIGN, not one
process. A persistent campaign ledger (evaluation_runs/
gemini-pilot-campaign-ledger.json) records an attempt before each request
is sent; deleting, copying or editing that ledger can weaken the ceiling
(see CampaignLedger docs). No hidden reset switch exists: the ceiling can
only be lifted by removing the ledger AND all campaign artifacts, which
runners refuse to do automatically and require manual operator action.

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
import contextlib
import copy
import datetime
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from investigation import accuracy, marking_adapter, marking_metrics

ARTIFACT_VERSION = "3b.2"
PILOT_CASE_IDS = (
    "algebra-correct",
    "algebra-partial",
    "probability-tree-arithmetic-error",
    "follow-through",
    "geometry-conflicting-methods",
)
MAX_PILOT_REQUESTS = 5
# Pilot-CAMPAIGN budget: five requests across ALL runs of this five-case
# campaign, tracked in a persistent local ledger. A per-process cap alone
# would allow an accidental restart to consume five more requests.
CAMPAIGN_ID = "stage-3b-gemini-pilot"
CAMPAIGN_LEDGER_FILENAME = "gemini-pilot-campaign-ledger.json"
CAMPAIGN_LEDGER_VERSION = 1
STALE_LOCK_SECONDS = 30.0
LOCK_WAIT_SECONDS = 10.0
OPT_IN_ENV = "MATH_MARKER_EVAL_OPT_IN"
API_KEY_ENV = "GEMINI_API_KEY"
DEFAULT_MODEL = "gemini-3.8-flash"
REQUEST_TIMEOUT_MS = 120_000
# Pinned per the Stage 3B-2 pre-flight audit (2026-10-18): temperature was
# REMOVED because Google's Gemini 3.x migration guidance deprecates it for
# this model generation (ignored today; HTTP 400 in future generations).
# Determinism comes from the explicit-rules system instruction. Thinking is
# pinned explicitly at the documented "medium" level (supported by
# google-genai's GenerateContentConfig.thinking_config). Thinking tokens
# count toward max_output_tokens AND are billed as output tokens. Keeping
# 2048 unchanged: the structured marking responses requested here are small
# (a few hundred tokens each, well under the 4.x-scale budgets), so raising
# the cap would not materially change worst-case cost while expanding the
# truncation surface; MAX_TOKENS remains a handled per-case failure.
PILOT_GENERATION_SETTINGS = {
    "max_output_tokens": 2048,
    "thinking_config": {"thinking_level": "medium"},
}

# Failure categories that abort ALL remaining requests (fail-closed):
# duplicated spend or predictable repeat failures must be avoided. This
# includes AMBIGUOUS TRANSMISSION OUTCOMES — timeouts (408/504 and SDK
# timeout exceptions) and transport errors — because a request that "timed
# out" may in fact have been received and processed; a retry could double
# bill, and continuing could repeat an infrastructure failure. Timeouts and
# transport errors are ALREADY recorded as consumed campaign attempts and
# never retried. Only qualified service-side 5xx responses remain per-case
# failures (the pilot proceeds to the next case without a repeat request).
ABORT_CATEGORIES = frozenset({
    "model_unavailable",       # 404 model-is-not-found for the requested ID
    "request_rejected",        # 400/401/403 errors incl. bad configuration
    "quota_limited",           # 429
    "timeout",                 # 408/504/SDK timeouts: ambiguous transmission
    "transport_or_client_error",  # ambiguous network layer errors
})

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
# Persistent campaign-request ledger (Phase 3B-2 hardening, local-only)
# ---------------------------------------------------------------------------

class CampaignLedgerError(RuntimeError):
    """Raised when the campaign budget says a run must NOT start.

    Content-free, operator-directed messages only.
    """


def _utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(path, payload):
    """Durably replace `path` with `payload` (same-directory temp + rename)."""
    path = Path(path)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def claim_exclusive_path(target, *, attempts=50):
    """Return a zero-byte-exclusively-created variant of `target`.

    Tries `target` itself first, then suffixed variants; never overwrites an
    existing file. Used for run artifact names so concurrent or repeated
    runs cannot clobber previous records.
    """
    target = Path(target)
    for index in range(attempts):
        candidate = target if index == 0 else target.with_name(
            f"{target.stem}-{index}{target.suffix}")
        try:
            handle = open(candidate, "x", encoding="utf-8")
        except FileExistsError:
            continue
        handle.close()
        return candidate
    raise CampaignLedgerError(
        "could not claim an unoccupied artifact filename; refusing to overwrite")


class InMemoryCampaignLedger:
    """Per-process ledger standing in when the caller supplies none.

    Used by tests and programmatic callers; enforces the per-call budget and
    replay prevention within one process only. File-based runs (the CLI)
    always inject CampaignLedger instead, which is the hardened path.
    """

    campaign_id = CAMPAIGN_ID

    def __init__(self, max_requests=MAX_PILOT_REQUESTS):
        self.max_requests = max_requests
        self._reservations = []

    def ensure_openable(self):
        return None

    def check_run_permitted(self, case_ids):
        remaining = self.max_requests - len(self._reservations)
        if len(case_ids) > remaining:
            raise CampaignLedgerError(
                "campaign request budget exhausted: "
                f"{remaining} of {self.max_requests} requests remain")
        already = sorted(set(case_ids) & {r["case_id"] for r in self._reservations})
        if already:
            raise CampaignLedgerError(
                "case already attempted in this campaign; replay refused")

    def reserve(self, case_id, opaque_case_id):
        ordinal = len(self._reservations) + 1
        if ordinal > self.max_requests:
            raise CampaignLedgerError("campaign request budget exhausted")
        already = {r["case_id"] for r in self._reservations}
        if case_id in already:
            raise CampaignLedgerError(
                "case already attempted in this campaign; replay refused")
        self._reservations.append({
            "campaign_request_ordinal": ordinal,
            "campaign_id": self.campaign_id,
            "case_id": case_id,
            "opaque_case_id": opaque_case_id,
            "reserved_utc": _utc_now_iso(),
            "outcome": None,
            "completed_utc": None,
        })
        return ordinal

    def complete(self, campaign_request_ordinal, outcome):
        for record in self._reservations:
            if record["campaign_request_ordinal"] == campaign_request_ordinal:
                record["outcome"] = outcome
                record["completed_utc"] = _utc_now_iso()
                return
        raise CampaignLedgerError("cannot mark an unknown campaign request")

    def count_used(self):
        return len(self._reservations)

    def count_remaining(self):
        return self.max_requests - len(self._reservations)


class CampaignLedger(InMemoryCampaignLedger):
    """Persistent ledger enforcing the five-request PILOT-CAMPAIGN ceiling.

    Behavior:
    - one JSON file (evaluation_runs/gemini-pilot-campaign-ledger.json)
      bound to CAMPAIGN_ID and the golden benchmark hash;
    - an attempt is RESERVED (durable, locked, atomic) BEFORE its request is
      sent, so ambiguous or interrupted attempts are conservatively consumed
      even if the process dies between send and bookkeeping;
    - refusals are content-free and operator-directed;
    - the ceiling CANNOT be flagged away: there is no secret reset. If the
      ledger file is missing but campaign artifacts exist, the runner
      refuses with a manual-review error instead of silently restarting.

    Documented limitations (the ceiling is local state, not an API contract):
    - deleting the ledger AND all campaign artifacts can restart the
      campaign — that requires manual operator action on the computer,
      which is intentional (no secret reset flag exists);
    - editing/copying the ledger file outside the runner can weaken the
      ceiling; the runner validates campaign_id, benchmark hash, ledger
      version and max_requests and refuses mismatches, but it cannot defend
      against deliberate crafted files;
    - the lock file (".lock" sibling) has a stale-age escape hatch for
      crashed processes; a lock held longer than stale-lock seconds may be
      reclaimed by another runner.
    """

    def __init__(self, directory, *, benchmark_sha=None,
                 max_requests=MAX_PILOT_REQUESTS,
                 clock=time, wait_seconds=LOCK_WAIT_SECONDS,
                 stale_seconds=STALE_LOCK_SECONDS):
        self.path = Path(directory) / CAMPAIGN_LEDGER_FILENAME
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.benchmark_sha = (benchmark_sha if benchmark_sha is not None
                              else benchmark_sha256())
        self.max_requests = max_requests
        self._clock = clock
        self._wait_seconds = wait_seconds
        self._stale_seconds = stale_seconds
        self._reservations = []

    # -- state helpers ------------------------------------------------------
    def _new_state(self):
        return {
            "ledger_version": CAMPAIGN_LEDGER_VERSION,
            "campaign_id": self.campaign_id,
            "benchmark_sha256": self.benchmark_sha,
            "max_requests": self.max_requests,
            "created_utc": _utc_now_iso(),
            "reservations": [],
        }

    def _validate_state(self, state):
        if not isinstance(state, dict):
            raise CampaignLedgerError("campaign ledger file is corrupt; "
                                      "manual review required")
        checks = {
            "campaign_id": self.campaign_id,
            "benchmark_sha256": self.benchmark_sha,
            "max_requests": self.max_requests,
            "ledger_version": CAMPAIGN_LEDGER_VERSION,
        }
        for key, expected in checks.items():
            if state.get(key) != expected:
                raise CampaignLedgerError(
                    f"campaign ledger {key} mismatch; manual review required")
        reservations = state.get("reservations")
        if not isinstance(reservations, list) or any(
                not isinstance(r, dict) or not isinstance(r.get("case_id"), str)
                for r in reservations):
            raise CampaignLedgerError("campaign ledger reservations are "
                                      "corrupt; manual review required")

    def _load(self):
        state = json.loads(self.path.read_text(encoding="utf-8"))
        self._validate_state(state)
        return state

    @contextlib.contextmanager
    def _locked(self):
        """Cross-process advisory lock; reclaims only stale lock files."""
        deadline = self._clock.monotonic() + self._wait_seconds
        while True:
            try:
                handle = open(self.lock_path, "x", encoding="utf-8")
            except FileExistsError:
                try:
                    age = self._clock.time() - self.lock_path.stat().st_mtime
                except OSError:
                    age = 0.0
                if age > self._stale_seconds:
                    with contextlib.suppress(OSError):
                        self.lock_path.unlink()
                if self._clock.monotonic() > deadline:
                    raise CampaignLedgerError(
                        "campaign ledger lock is held by another process")
                self._sleep(0.02)
                continue
            break
        handle.write(f"pid={os.getpid()}\n")
        handle.close()
        try:
            yield
        finally:
            # Remove the lock only if it is still ours at read-back time;
            # foreign or unreadable locks are left for the stale reclaim.
            with contextlib.suppress(OSError):
                try:
                    held = self.lock_path.read_text(encoding="utf-8")
                except OSError:
                    held = None
                if held == f"pid={os.getpid()}\n":
                    self.lock_path.unlink()

    def _sleep(self, seconds):
        self._clock.sleep(seconds)

    def ensure_openable(self):
        """Fail-closed preconditions, including the deleted-ledger guard."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            artifacts = [p for p in self.path.parent.glob("*.json")
                         if p.name.endswith("-gemini-pilot.json")]
            if artifacts:
                raise CampaignLedgerError(
                    "campaign artifacts exist but the campaign ledger is "
                    "missing; manual review required — no silent campaign "
                    "restart is permitted")
            try:
                handle = open(self.path, "x", encoding="utf-8")
            except FileExistsError:      # concurrent creation: validate theirs
                self._validate_state(self._load())
                return
            with handle:
                json.dump(self._new_state(), handle, indent=2, sort_keys=True)
            return
        self._validate_state(self._load())

    def check_run_permitted(self, case_ids):
        self.ensure_openable()
        with self._locked():
            state = self._load()
            self._reservations = list(state["reservations"])
            super().check_run_permitted(case_ids)

    def reserve(self, case_id, opaque_case_id):
        self.ensure_openable()
        with self._locked():
            state = self._load()
            self._reservations = list(state["reservations"])
            ordinal = super().reserve(case_id, opaque_case_id)
            state["reservations"] = self._reservations
            _atomic_write_json(self.path, state)
        return ordinal

    def complete(self, campaign_request_ordinal, outcome):
        self.ensure_openable()
        with self._locked():
            state = self._load()
            self._reservations = list(state["reservations"])
            super().complete(campaign_request_ordinal, outcome)
            state["reservations"] = self._reservations
            _atomic_write_json(self.path, state)



# ---------------------------------------------------------------------------
# Live execution (implemented; NOT actually executed — offline verification)
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


def _assert_live_authorization(model, case_count, budget_confirmation,
                               opt_in=None):
    """Every live-mode authorization gate, enforced BEFORE any campaign
    state (ledger file, artifact placeholders) can be created or modified.

    Called from both main() (before the CampaignLedger touches the
    filesystem) and run_live() (idempotent re-check). Raises the historical
    error types and messages; returns nothing on success.
    """
    if not MODEL_ID_PATTERN.fullmatch(model):
        raise ValueError("Invalid requested model ID")
    if case_count > MAX_PILOT_REQUESTS:
        raise ValueError(
            f"Pilot budget exceeded: at most {MAX_PILOT_REQUESTS} cases per run")
    if budget_confirmation != case_count:
        raise RuntimeError(
            "Request budget must be explicitly confirmed as the exact case count")
    opt_value = opt_in if opt_in is not None else _read_opt_in()
    if opt_value != "1":
        raise RuntimeError(
            f"Live evaluation requires {OPT_IN_ENV}=1 operator opt-in")


def run_live(cases, manifest, *, model=DEFAULT_MODEL,
             client_factory=None, config_factory=None, clock=time,
             opt_in=None, budget_confirmation=None,
             campaign_ledger=None, checkpoint_writer=None):
    """Execute the five-case pilot sequentially. HARD GATES, no network here.

    client_factory/config_factory: dependency-injected for tests; the
    defaults construct SDK objects. opt_in: environment gate value; the
    default reads OPT_IN_ENV (live mode only). budget_confirmation must
    equal the exact case count; live mode refuses otherwise.

    campaign_ledger: optional CampaignLedger (or test double). When None an
    in-memory per-process ledger is created — the CLI passes the persistent
    campaign ledger instead, which is what enforces the five-request
    campaign ceiling across runs. An attempt is RESERVED before its request
    is sent and marked completed afterwards (best effort; the reservation
    already made spending durable). checkpoint_writer: optional callable
    invoked after each attempted case with a snapshot dict, so progress is
    durable even if the run is interrupted before the final artifact.

    Strict failure behavior: any exception classified into ABORT_CATEGORIES
    aborts all remaining requests (fail-closed); NO case is ever retried and
    no model is ever substituted.
    """
    _assert_live_authorization(model, len(cases), budget_confirmation,
                               opt_in)
    if not cases:
        raise ValueError("Live run requires at least one case")

    # Campaign ceiling: refuse BEFORE prompt rendering / client construction.
    case_ledger = campaign_ledger if campaign_ledger is not None \
        else InMemoryCampaignLedger()
    case_ledger.check_run_permitted([c["id"] for c in cases])

    prompts = render_prompts(cases)
    mapping = {p["canonical_case_id"]: p["opaque_case_id"] for p in prompts}
    schema = evaluation_response_schema()
    config_factory = config_factory or _default_config_factory
    client_factory = client_factory or _default_client_factory
    client = client_factory()

    ledger = []
    predictions = []
    stop_reason = None          # category that triggered the abort, or None
    generation_settings = dict(PILOT_GENERATION_SETTINGS)
    campaign_id = getattr(case_ledger, "campaign_id", CAMPAIGN_ID)

    for index, (case, prompt) in enumerate(zip(cases, prompts)):
        case_id = case["id"]
        attempt_number = index + 1
        if attempt_number > MAX_PILOT_REQUESTS:
            ledger.append(_skipped_entry(case_id, mapping[case_id],
                                         "request_budget_exhausted"))
            continue
        if stop_reason is not None:
            ledger.append(_skipped_entry(
                case_id, mapping[case_id], f"aborted_after_{stop_reason}"))
            continue

        # Reserve the campaign attempt BEFORE sending anything: ambiguous or
        # interrupted attempts are conservatively consumed (see ledger docs).
        campaign_request_ordinal = case_ledger.reserve(case_id,
                                                       mapping[case_id])
        entry = {
            "case_id": case_id,
            "opaque_case_id": prompt["opaque_case_id"],
            "requested_model": model,
            "attempt_number": attempt_number,
            "campaign_request_ordinal": campaign_request_ordinal,
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
            _record_campaign_outcome(case_ledger, campaign_request_ordinal,
                                     {"outcome": "failed",
                                      "failure_category": category})
            ledger.append(entry)
            if category in ABORT_CATEGORIES:
                stop_reason = category
            _write_checkpoint(checkpoint_writer, ledger, predictions,
                              stop_reason, campaign_id, model)
            continue
        entry["latency_seconds"] = round(clock.monotonic() - started, 3)
        entry["actual_model_version"] = _sanitize_version(response)
        entry["usage"] = _usage_facts(response) or None
        finish_value, block_value = _final_text_response(response)
        entry["finish_reason"] = block_value or finish_value
        if block_value and block_value not in ("BLOCK_REASON_UNSPECIFIED",):
            entry["outcome"] = "failed"
            entry["failure_category"] = "blocked_response"
            _record_campaign_outcome(case_ledger, campaign_request_ordinal,
                                     {"outcome": "failed",
                                      "failure_category": "blocked_response"})
            ledger.append(entry)
            _write_checkpoint(checkpoint_writer, ledger, predictions,
                              stop_reason, campaign_id, model)
            continue
        if finish_value not in (None, "STOP"):
            entry["outcome"] = "failed"
            entry["failure_category"] = ("truncated_response"
                                          if finish_value == "MAX_TOKENS"
                                          else "blocked_response")
            _record_campaign_outcome(case_ledger, campaign_request_ordinal,
                                     {"outcome": "failed",
                                      "failure_category": entry["failure_category"]})
            ledger.append(entry)
            _write_checkpoint(checkpoint_writer, ledger, predictions,
                              stop_reason, campaign_id, model)
            continue

        text = getattr(response, "text", None)
        try:
            parsed = parse_model_json(text)
        except ValueError as parse_failure:
            entry["outcome"] = "failed"
            entry["failure_category"] = str(parse_failure)
            entry["json_valid"] = False
            _record_campaign_outcome(case_ledger, campaign_request_ordinal,
                                     {"outcome": "failed",
                                      "failure_category": entry["failure_category"]})
            ledger.append(entry)
            _write_checkpoint(checkpoint_writer, ledger, predictions,
                              stop_reason, campaign_id, model)
            continue
        entry["json_valid"] = True

        expected_opaque = prompt["opaque_case_id"]
        if parsed.get("id") != expected_opaque:
            entry["outcome"] = "failed"
            entry["failure_category"] = "id_mismatch"
            entry["opaque_id_echo_ok"] = False
            _record_campaign_outcome(case_ledger, campaign_request_ordinal,
                                     {"outcome": "failed",
                                      "failure_category": "id_mismatch"})
            ledger.append(entry)
            _write_checkpoint(checkpoint_writer, ledger, predictions,
                              stop_reason, campaign_id, model)
            continue
        entry["opaque_id_echo_ok"] = True

        if not validate_against_schema(parsed, schema):
            entry["outcome"] = "failed"
            entry["failure_category"] = "schema_validation_failure"
            entry["schema_valid"] = False
            _record_campaign_outcome(case_ledger, campaign_request_ordinal,
                                     {"outcome": "failed",
                                      "failure_category": "schema_validation_failure"})
            ledger.append(entry)
            _write_checkpoint(checkpoint_writer, ledger, predictions,
                              stop_reason, campaign_id, model)
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
            _record_campaign_outcome(case_ledger, campaign_request_ordinal,
                                     {"outcome": "failed",
                                      "failure_category": "adapter_batch_error"})
            ledger.append(entry)
            _write_checkpoint(checkpoint_writer, ledger, predictions,
                              stop_reason, campaign_id, model)
            stop_reason = "program_error"
            continue
        entry["adapter_failure_reason"] = (prediction.get("failure_reason")
                                           if prediction.get("failed") else None)
        predictions.append(prediction)
        _record_campaign_outcome(case_ledger, campaign_request_ordinal,
                                 {"outcome": "parsed", "failure_category": None,
                                  "adapter_failure_reason":
                                      entry["adapter_failure_reason"]})
        ledger.append(entry)
        _write_checkpoint(checkpoint_writer, ledger, predictions,
                          stop_reason, campaign_id, model)

    artifact = build_artifact(cases, manifest, model=model, mode="live",
                              ledger=ledger, predictions=predictions,
                              stop_reason=stop_reason)
    artifact["campaign_id"] = campaign_id
    artifact["campaign_requests_used"] = case_ledger.count_used()
    artifact["campaign_requests_remaining"] = case_ledger.count_remaining()
    return artifact


def _record_campaign_outcome(case_ledger, campaign_request_ordinal, outcome):
    """Best-effort completion bookkeeping; the pre-send reservation already
    made the budget consumption durable."""
    try:
        case_ledger.complete(campaign_request_ordinal, outcome)
    except Exception:
        pass  # reservation stands; artifact ledger carries the case result


def _write_checkpoint(checkpoint_writer, ledger, predictions, stop_reason,
                      campaign_id, model):
    """Durable progress snapshot after each attempted case (no-op if absent)."""
    if checkpoint_writer is None:
        return
    attempted = [e for e in ledger if e.get("attempted")]
    snapshot = {
        "checkpoint_version": ARTIFACT_VERSION,
        "campaign_id": campaign_id,
        "mode": "checkpoint",
        "run_status": "running",
        "requested_model": model,
        "generated_utc": _utc_now_iso(),
        "aborted_after": stop_reason,
        "attempted_count": len(attempted),
        "skipped_count": sum(1 for e in ledger if e.get("outcome") == "skipped"),
        "completed_count": len(predictions),
        "cases": ledger,
        "predictions": predictions,
    }
    try:
        checkpoint_writer(snapshot)
    except Exception:
        pass  # progress files are best-effort; the campaign ledger is durable


def _clear_placeholder_or_mark(partial_path, run_status):
    """Preserve durable checkpoints on error paths.

    A genuinely EMPTY zero-byte placeholder may be removed; any non-empty
    partial checkpoint SURVIVES with an explicit run status recorded
    ("interrupted" or "finalization_failed"). Unreadable snapshots keep
    their original bytes untouched.
    """
    try:
        size = partial_path.stat().st_size
    except OSError:
        return
    if size == 0:                 # placeholder claimed, nothing recorded yet
        with contextlib.suppress(OSError):
            partial_path.unlink()
        return
    try:
        snapshot = json.loads(partial_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return                    # keep the recorded bytes as-is
    if isinstance(snapshot, dict):
        snapshot["run_status"] = run_status
        snapshot["marked_utc"] = _utc_now_iso()
        with contextlib.suppress(Exception):
            _atomic_write_json(partial_path, snapshot)


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
            "Stage 3B-2 hardening: temperature removed per Google Gemini 3.x "
            "migration guidance (deprecated parameter); thinking explicitly "
            "configured at the documented medium level; thinking tokens count "
            "toward max_output_tokens and are billed as output tokens; 2048 "
            "retained after budget assessment (small structured responses, "
            "truncation handled as per-case failure).",
            "Strict abort policy: auth (401/403), permission/configuration "
            "errors (400), model-unavailable 404s, 429 quota errors, "
            "ambiguous timeouts (408/504) and ambiguous transport errors "
            "abort all remaining requests; failed cases are never retried "
            "and models are never substituted.",
            "Campaign budget: at most five requests for the whole pilot "
            "campaign, enforced by a persistent local ledger that reserves "
            "each attempt before its request is sent.",
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
            "abort_categories": sorted(ABORT_CATEGORIES),
            "explicit_thinking_level": (PILOT_GENERATION_SETTINGS
                                        ["thinking_config"]["thinking_level"]),
            "campaign_budget": {
                "campaign_id": CAMPAIGN_ID,
                "max_total_requests": MAX_PILOT_REQUESTS,
                "ledger_file": f"evaluation_runs/{CAMPAIGN_LEDGER_FILENAME}",
                "scope": "persistent campaign ledger; survives process restarts",
            },
        },
        "status": "dry-run-no-requests-made",
    }


def main(argv=None):
    """CLI: default is --dry-run; --live carries the campaign safety stack."""
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

    # Authorization FIRST: no campaign file may be created or modified while
    # opt-in/request-budget/model gates are unsatisfied.
    _assert_live_authorization(args.model, len(cases),
                               args.confirm_request_budget)

    runs_dir = Path("evaluation_runs")
    # The persistent campaign ledger enforces the five-request ceiling for
    # the WHOLE pilot campaign and must pass identity/manual-review checks
    # BEFORE any request-capable object is constructed.
    campaign_ledger = CampaignLedger(runs_dir)
    campaign_ledger.check_run_permitted([c["id"] for c in cases])

    # Exclusive artifact claim: this run's final artifact name is claimed
    # upfront (zero-byte) so no run can overwrite a previous artifact; the
    # durable per-case checkpoint is written into a partial file that is
    # promoted to the final name atomically at completion.
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    partial_path = claim_exclusive_path(runs_dir / f"{stamp}-gemini-pilot.partial.json")
    final_path = claim_exclusive_path(runs_dir / f"{stamp}-gemini-pilot.json")

    run_finished = False
    try:
        artifact = run_live(
            cases, governance, model=args.model,
            budget_confirmation=args.confirm_request_budget,
            campaign_ledger=campaign_ledger,
            checkpoint_writer=lambda snapshot: _atomic_write_json(
                partial_path, snapshot))
        run_finished = True
        _atomic_write_json(final_path, artifact)
    except BaseException:
        # NEVER destroy recorded progress: a genuinely empty placeholder may
        # be cleared, but non-empty partial checkpoints survive and record
        # their status. The final placeholder is cleared only while it is
        # still empty (a complete artifact was never promoted). The campaign
        # ledger is left untouched: reserved attempts stay consumed.
        _clear_placeholder_or_mark(
            partial_path, "interrupted" if not run_finished
            else "finalization_failed")
        try:
            if final_path.stat().st_size == 0:
                with contextlib.suppress(OSError):
                    final_path.unlink()
        except OSError:
            pass
        raise
    with contextlib.suppress(OSError):
        partial_path.unlink()
    print(json.dumps({"artifact": str(final_path), "report_version":
                      artifact["report"]["report_version"],
                      "prediction_status":
                      artifact["report"]["prediction_status"],
                      "campaign_requests_remaining":
                      artifact["campaign_requests_remaining"]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
