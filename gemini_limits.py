"""Central Gemini quota, rate-limiting and concurrency policy (Stage S4, finding F-05).

One source of truth for the server-side limits that protect the operator's
Gemini quota from repeated marking runs, many browser tabs, one teacher
monopolising the service and shared-quota exhaustion. It mirrors the Stage S3
pattern of `document_limits.py`: tunable by environment variables for review
and testing, read once at import, never a billing or subscription mechanism.

Scope and honesty of guarantees
-------------------------------
All limiter state lives in the memory of ONE application server process and is
protected by a single RLock. Streamlit serves script reruns and marking
workers as threads of that one process, so the limits are effective across
browser tabs and teacher sessions *of one server process*. They are NOT
durable across process restarts and NOT globally consistent across multiple
deployed replicas; real cross-replica enforcement is deferred to the future
Python API architecture (durable store or gateway limiter). Nothing here
pretends otherwise.

Privacy
-------
Limiter records never store names, emails, OAuth/API tokens or student
content. Owners are stable pseudonymous codes: an SHA-256 digest of the
server-side authenticated OIDC subject (or the S2 "google:<subject>" owner
token). Diagnostics are a bounded ring of allowlisted fields only (category,
granted/denied, safe reason, bounded retry hint, aggregate active-jobs count
and an internal correlation id).

Threading
---------
One lock guards every decision; each critical section is O(1)/tiny-window
work, so concurrent Streamlit worker threads can never both pass a capacity
check. Nothing waits inside the lock: acquisition is pass/fail with no
blocking, so the gate cannot deadlock a marking worker or the UI thread.
"""
import hashlib
import math
import os
import re
import threading
import time
import uuid
from collections import deque


def _ceiling_seconds(value):
    """Ceiling of a non-negative cooldown in whole seconds (never 0 by rounding)."""
    return int(math.ceil(max(0.0, float(value))))


def _limit_from_env(name, default, minimum, maximum):
    """Read an integer environment override, clamped to safe bounds."""
    value = default
    raw = os.environ.get(name)
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = default
    return max(minimum, min(value, maximum))


# A marking job (manual or Classroom) holds its permit until the run's own
# cleanup frees it. The TTL is a crash-safety net so an abruptly killed
# Streamlit rerun can never lock an account out permanently; it behaves as if
# the permit had been released a bounded time later. A sequential
# 30-student batch needs roughly 30-60 minutes (PR #15/#28 budgets).
JOB_HOLD_SECONDS = _limit_from_env("MARKER_JOB_TTL_MINUTES", 90, 10, 480) * 60

# One account, one marking run at a time. Marking is sequential by design
# (PR #15: one worker per batch), so a second concurrent run cannot do useful
# work: it duplicatively re-prepares the scheme and doubles quota. This also
# covers "Simultaneous manual and Classroom marking" from the S4 requirement
# list, and makes double-click/second-tab submissions idempotent in effect.
MAX_ACCOUNT_ACTIVE_JOBS = _limit_from_env("MARKER_ACCOUNT_ACTIVE_JOBS", 1, 1, 16)

# Rapid repeated run starts (restart-whack-a-mole after failures) are limited
# to a rolling window. 3 starts per 5 minutes still lets a teacher whose run
# failed retry promptly after fixing a local problem.
ACCOUNT_START_WINDOW_SECONDS = 300
MAX_ACCOUNT_JOB_STARTS = _limit_from_env("MARKER_ACCOUNT_JOB_STARTS", 3, 1, 30)

# Global caps protect the shared key against many accounts in aggregate while
# keeping ordinary classroom traffic flowing (each run is sequential).
MAX_GLOBAL_ACTIVE_JOBS = _limit_from_env("MARKER_GLOBAL_ACTIVE_JOBS", 3, 1, 32)
GLOBAL_START_WINDOW_SECONDS = 300
MAX_GLOBAL_JOB_STARTS = _limit_from_env("MARKER_GLOBAL_JOB_STARTS", 10, 1, 120)

# The sidebar key test issues a lightweight models.list() call on the shared
# key: Stage S2 authenticated it, Stage S4 rate-limits it per account.
PROBE_WINDOW_SECONDS = 300
MAX_PROBE_STARTS = _limit_from_env("MARKER_PROBE_STARTS", 5, 1, 60)

# Process-wide cooldown after a shared/unknown-scope provider 429, bridged
# from the PR #15 engine (whose cooldown state is per-client and would be
# bypassed by the fresh client the next run creates). The default matches the
# provider's minute-level 90 s interval; a bridged daily-quota value may
# extend the deadline up to 6 hours. Env-tunable for review and testing.
SHARED_QUOTA_COOLDOWN_SECONDS = _limit_from_env(
    "MARKER_SHARED_QUOTA_COOLDOWN", 90, 30, 21600)
MIN_BRIDGED_COOLDOWN_SECONDS = 30
MAX_BRIDGED_COOLDOWN_SECONDS = 21600

# State bounding: at most a few thousand pseudonymous owners are tracked,
# oldest fully-expired ones evicted first. Start windows hold one bounded
# timestamp per allowed start, so per-owner growth is O(1).
MAX_TRACKED_OWNERS = 2048
DIAGNOSTIC_RING_CAPACITY = 100

# Server-side OIDC subjects are opaque printable tokens; matching subjects are
# hashed to a pseudonymous code and everything else fails closed.
SUBJECT_PATTERN = re.compile(r"^[\x21-\x7e]{8,256}$")

MANUAL_MARKING = "manual_marking"
CLASSROOM_MARKING = "classroom_marking"
API_KEY_PROBE = "api_key_probe"
JOB_CATEGORIES = (MANUAL_MARKING, CLASSROOM_MARKING)
ALL_CATEGORIES = JOB_CATEGORIES + (API_KEY_PROBE,)

# Failure categories voluntarily bridged into the shared cooldown (Phase 7):
# a shared/unknown-scope 429 or an entirely-cooled-down marking attempt.
BRIDGE_FAILURE_CATEGORIES = frozenset({"quota_limited", "cooldown"})

# Internal, content-free denial reasons (also the diagnostic allowlist).
_IDENTITY_MISSING = "identity_missing"
_ACCOUNT_ACTIVE_LIMIT = "account_active_limit"
_ACCOUNT_START_RATE = "account_start_rate"
_GLOBAL_ACTIVE_LIMIT = "global_active_limit"
_GLOBAL_START_RATE = "global_start_rate"
_SHARED_QUOTA_COOLDOWN = "shared_quota_cooldown"
_PROBE_RATE = "probe_rate"

_lock = threading.RLock()
_active_by_owner = {}       # owner code -> active permit count
_starts_by_owner = {}       # owner code -> bounded list of job start times
_probe_starts = {}          # owner code -> bounded list of probe start times
_global_starts = []         # process-wide job start window
_permits = {}               # permit id -> {owner, category, expires}
_shared_quota_until = 0.0   # monotonic deadline of the shared-quota cooldown
_diagnostics = deque(maxlen=DIAGNOSTIC_RING_CAPACITY)

# Phase 9: content-free user guidance. No account identifiers, no provider
# bodies, no document references.
MESSAGES = {
    _IDENTITY_MISSING:
        "Sign in with Google before starting a marking run. "
        "Nothing was uploaded or sent to the AI service.",
    _ACCOUNT_ACTIVE_LIMIT:
        "Too many marking jobs are already running for your account. "
        "Wait until the current run finishes, then start another.",
    _ACCOUNT_START_RATE:
        "Marking runs are being started too quickly for this account. "
        "Please wait a few minutes and try again.",
    _GLOBAL_ACTIVE_LIMIT:
        "The marking service is temporarily busy with other teacher runs. "
        "Please try again in a few minutes.",
    _GLOBAL_START_RATE:
        "The marking service is temporarily busy. "
        "Please try again in a few minutes.",
    _SHARED_QUOTA_COOLDOWN:
        "Gemini quota is temporarily rate-limited. "
        "Please wait before starting another marking run.",
    _PROBE_RATE:
        "Too many API key checks were requested. "
        "Please wait a few minutes before trying again.",
}


def format_retry_hint(retry_after):
    """Bounded, human-safe wait hint; honest about long cooldowns too."""
    if type(retry_after) is not int or retry_after <= 0:
        return ""
    if retry_after <= 120:
        minutes = max(1, retry_after // 60)
        if minutes <= 1:
            return "Try again in about a minute."
        return f"Try again in about {minutes} minutes."
    if retry_after <= 600:
        return "Try again in a few minutes."
    hours = -(-retry_after // 3600)  # ceiling, minimum 1 hour shown
    if hours == 1:
        return "Try again in about an hour."
    return f"Try again in about {hours} hours."


class RateLimitDenied(Exception):
    """Fail-closed denial carrying a content-free message and a bounded hint.

    `retry_after` is a conservative integer of seconds (never negative, never
    presented as provider data), or None when no honest hint exists. The
    message never contains account identifiers or provider exception text.
    """

    def __init__(self, reason, retry_after=None, message=None):
        self.reason = reason
        self.retry_after = retry_after
        chosen = message
        if not isinstance(chosen, str) or not chosen:
            chosen = MESSAGES.get(reason, "The marking service is temporarily unavailable. Please try again later.")
            hint = format_retry_hint(retry_after)
            if hint:
                chosen = f"{chosen} {hint}"
        super().__init__(chosen)
        self.message = chosen


class Permit:
    """An acquired reservation whose release is idempotent and time-bounded."""

    def __init__(self, permit_id, owner_code, category, expires):
        self._id = permit_id
        self._owner_code = owner_code
        self._category = category
        self._released = False

    @property
    def category(self):
        return self._category

    def release(self):
        """Idempotent release; a no-op after TTL expiry or double release."""
        with _lock:
            if self._released:
                return False
            self._released = True
            if _permits.pop(self._id, None) is None:
                return False
            remaining = _active_by_owner.get(self._owner_code, 0) - 1
            if remaining > 0:
                _active_by_owner[self._owner_code] = remaining
            else:
                _active_by_owner.pop(self._owner_code, None)
            _diagnostic("permit_released", None, self._category, True, None)
            return True

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.release()
        return False


class ProbeToken:
    """Acknowledgement of one rate-limited API-key probe (nothing to reserve)."""

    def release(self):
        with _lock:
            _diagnostic("probe_released", None, API_KEY_PROBE, True, None)
            return True

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.release()
        return False


def pseudonymous_owner(identity):
    """Stable pseudonymous owner code, or None to fail closed.

    Accepts an OIDC subject or the S2 "google:<subject>" owner token. The
    digest is not practically reversible and no raw identifier is retained.
    """
    if not isinstance(identity, str):
        return None
    value = identity.strip()
    if value[:7].lower() == "google:":
        value = value[7:].strip()
    if not SUBJECT_PATTERN.fullmatch(value):
        return None
    return "ow_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _deny(reason, retry_after=None):
    raise RateLimitDenied(reason=reason, retry_after=retry_after)


def _global_active_count():
    return sum(count for count in _active_by_owner.values() if count > 0)


def _diagnostic(event, reason, category, granted, retry_after):
    """Append one allowlisted, content-free diagnostic entry (bounded ring)."""
    entry = {
        "event": event,
        "category": category if category in ALL_CATEGORIES else "unclassified",
        "granted": bool(granted),
        "reason": reason if reason in MESSAGES else None,
        "retry_after_seconds":
            retry_after if type(retry_after) is int and retry_after >= 0 else None,
        "active_jobs_global": _global_active_count(),
        "correlation_id": uuid.uuid4().hex,
    }
    _diagnostics.append(entry)
    return entry


def flush_state():
    """Test helper: clear every record so scenarios are independent."""
    global _shared_quota_until
    with _lock:
        _active_by_owner.clear()
        _starts_by_owner.clear()
        _probe_starts.clear()
        _global_starts.clear()
        _permits.clear()
        _shared_quota_until = 0.0
        _diagnostics.clear()


def release_expired(now):
    """Auto-expire permits whose owning run leaked (crash / killed rerun).

    Called at the top of every job acquisition while holding the lock, so a
    leaked permit ages out within JOB_HOLD_SECONDS and can never lock an
    account out permanently.
    """
    expired = [pid for pid, info in _permits.items() if info["expires"] <= now]
    for pid in expired:
        info = _permits.pop(pid)
        remaining = _active_by_owner.get(info["owner"], 0) - 1
        if remaining > 0:
            _active_by_owner[info["owner"]] = remaining
        else:
            _active_by_owner.pop(info["owner"], None)


def _prune_owners(now):
    """Keep owner tables bounded; evict fully-expired owners oldest-first."""
    while len(_starts_by_owner) > MAX_TRACKED_OWNERS:
        evicted = False
        for owner in list(_starts_by_owner):
            if owner in _active_by_owner:
                continue
            window = [t for t in _starts_by_owner[owner]
                      if t > now - ACCOUNT_START_WINDOW_SECONDS]
            if isinstance(window, list) and window:
                _starts_by_owner[owner] = window
            else:
                _starts_by_owner.pop(owner, None)
                evicted = True
            if evicted:
                break
        if not evicted:
            break


def acquire_job(identity, category, clock=time):
    """Server-side execution-boundary gate for one marking run.

    Raises :class:`RateLimitDenied` (fail closed) when the identity is missing
    or malformed, a marking job is already active for the account, the global
    active-job cap is reached, the account or process start-window budget is
    spent, or a bridged shared-quota cooldown is in force. The reservation is
    held until the caller's finally block releases it; leaked permits expire
    within JOB_HOLD_SECONDS.
    """
    if category not in JOB_CATEGORIES:
        # Unknown category: fail closed rather than guess a limit set.
        _deny(_IDENTITY_MISSING, None)
    with _lock:
        now = clock.monotonic()
        release_expired(now)
        owner_code = pseudonymous_owner(identity)
        if owner_code is None:
            _diagnostic("permit_denied", _IDENTITY_MISSING, category, False, None)
            _deny(_IDENTITY_MISSING, None)
        if now < _shared_quota_until:
            retry_after = _ceiling_seconds(_shared_quota_until - now)
            _diagnostic("permit_denied", _SHARED_QUOTA_COOLDOWN, category,
                        False, retry_after)
            _deny(_SHARED_QUOTA_COOLDOWN, retry_after)
        active = _active_by_owner.get(owner_code, 0)
        if active >= MAX_ACCOUNT_ACTIVE_JOBS:
            _diagnostic("permit_denied", _ACCOUNT_ACTIVE_LIMIT, category,
                        False, None)
            _deny(_ACCOUNT_ACTIVE_LIMIT, None)
        if _global_active_count() >= MAX_GLOBAL_ACTIVE_JOBS:
            _diagnostic("permit_denied", _GLOBAL_ACTIVE_LIMIT, category,
                        False, None)
            _deny(_GLOBAL_ACTIVE_LIMIT, None)
        starts = [t for t in _starts_by_owner.get(owner_code, [])
                  if t > now - ACCOUNT_START_WINDOW_SECONDS]
        if len(starts) >= MAX_ACCOUNT_JOB_STARTS:
            retry_after = _ceiling_seconds(starts[0] + ACCOUNT_START_WINDOW_SECONDS - now)
            _diagnostic("permit_denied", _ACCOUNT_START_RATE, category,
                        False, retry_after)
            _deny(_ACCOUNT_START_RATE, retry_after)
        if len(_global_starts) > MAX_GLOBAL_JOB_STARTS * 64:
            del _global_starts[:len(_global_starts) - MAX_GLOBAL_JOB_STARTS * 64]
        recent_global = [t for t in _global_starts
                         if t > now - GLOBAL_START_WINDOW_SECONDS]
        if len(recent_global) >= MAX_GLOBAL_JOB_STARTS:
            retry_after = _ceiling_seconds(recent_global[0] + GLOBAL_START_WINDOW_SECONDS - now)
            _diagnostic("permit_denied", _GLOBAL_START_RATE, category,
                        False, retry_after)
            _deny(_GLOBAL_START_RATE, retry_after)
        permit_id = uuid.uuid4().hex
        _active_by_owner[owner_code] = active + 1
        starts.append(now)
        _starts_by_owner[owner_code] = starts
        _global_starts[:] = recent_global
        _global_starts.append(now)
        expires = now + JOB_HOLD_SECONDS
        _permits[permit_id] = {"owner": owner_code, "category": category,
                               "expires": expires}
        _diagnostic("permit_granted", None, category, True, None)
        _prune_owners(now)
        return Permit(permit_id, owner_code, category, expires)


def acquire_probe(identity, clock=time):
    """Rate-limit the lightweight, authenticated API-key verification call.

    Not subject to job caps or the shared-quota cooldown: a model listing can
    still succeed while generation quota is spent, and a useful key check must
    remain available to the teacher. Denials and grants are rate-limited per
    account over a rolling window.
    """
    with _lock:
        now = clock.monotonic()
        owner_code = pseudonymous_owner(identity)
        if owner_code is None:
            _diagnostic("permit_denied", _IDENTITY_MISSING, API_KEY_PROBE,
                        False, None)
            _deny(_IDENTITY_MISSING, None)
        starts = [t for t in _probe_starts.get(owner_code, [])
                  if t > now - PROBE_WINDOW_SECONDS]
        if len(starts) >= MAX_PROBE_STARTS:
            retry_after = _ceiling_seconds(starts[0] + PROBE_WINDOW_SECONDS - now)
            _diagnostic("permit_denied", _PROBE_RATE, API_KEY_PROBE, False,
                        retry_after)
            _deny(_PROBE_RATE, retry_after)
        starts.append(now)
        _probe_starts[owner_code] = starts
        _diagnostic("permit_granted", None, API_KEY_PROBE, True, None)
        return ProbeToken()


def bridge_marking_failure(exc, clock=time):
    """Cooperative bridge from the PR #15 engine to the shared-quota cooldown.

    The engine's cooldowns (`state['account_until']`, `state['blocked']`) are
    per-client and a new run constructs a fresh client, so this voluntarily
    re-exports only the two categories that represent provider-dictated
    shared pressure, classified content-free from the exception's own
    attributes (never its message body). Non-marking exceptions are ignored.
    Returns the remaining shared-cooldown seconds (0 when untouched).
    """
    category = getattr(exc, "category", None)
    if category not in BRIDGE_FAILURE_CATEGORIES:
        return 0
    if getattr(exc, "request_id", None) is None:
        return 0
    remaining = getattr(exc, "remaining", None)
    if isinstance(remaining, bool) or not isinstance(remaining, (int, float)) \
            or not math.isfinite(remaining) or remaining <= 0:
        remaining = SHARED_QUOTA_COOLDOWN_SECONDS
    return note_shared_quota(float(remaining), clock=clock)


def note_shared_quota(seconds, clock=time):
    """Bridge a shared/unknown-scope provider quota failure into this module.

    The bridged value is clamped to a conservative range so a malformed value
    can never lock the process out indefinitely; the deadline is extended only
    when the new value is longer, and it always decays with time. Returns the
    resulting remaining cooldown seconds (0 when none).
    """
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) \
            or seconds <= 0 or seconds != seconds:  # NaN check
        seconds = SHARED_QUOTA_COOLDOWN_SECONDS
    seconds = int(max(MIN_BRIDGED_COOLDOWN_SECONDS,
                      min(seconds, MAX_BRIDGED_COOLDOWN_SECONDS)))
    now = clock.monotonic()
    global _shared_quota_until
    with _lock:
        if now + seconds > _shared_quota_until:
            _shared_quota_until = now + seconds
            _diagnostic("shared_cooldown_extended", _SHARED_QUOTA_COOLDOWN,
                        MANUAL_MARKING, True, seconds)
        return int(max(0, _shared_quota_until - now))


def shared_quota_active(clock=time):
    """True while the process-wide shared-quota cooldown is in force."""
    return clock.monotonic() < _shared_quota_until


def privacy_probe():
    """Expose raw internal records for tests to prove privacy-safety.

    Returned structures contain only pseudonymous owner codes, timestamps,
    counts and allowlisted diagnostics; tests assert that no raw subject-like,
    token-like or content-bearing string is stored. Lock-protected snapshot.
    """
    with _lock:
        return {
            "active": dict(_active_by_owner),
            "job_starts": {k: list(v) for k, v in _starts_by_owner.items()},
            "probe_starts": {k: list(v) for k, v in _probe_starts.items()},
            "global_starts": list(_global_starts),
            "permits": {k: dict(v) for k, v in _permits.items()},
            "shared_until": _shared_quota_until,
            "diagnostics": list(_diagnostics),
        }


def diagnostic_snapshot():
    """Allowlisted diagnostics only: safe aggregate fields, never identity."""
    with _lock:
        return [dict(entry) for entry in _diagnostics]
