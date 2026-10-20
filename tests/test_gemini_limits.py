"""Offline Stage S4 regression tests: Gemini quota gate, rate limiting and concurrency.

Scope mirrors the Stage S3 conventions: deterministic, offline, fake clocks and
mocked/faked concurrency; no live provider traffic. Real production boundary
helpers are exercised where headless-loadable, plus source probes for the
top-level Streamlit blocks (the headless loader cannot execute those blocks,
matching the Stage S2/S3 probe convention).
"""
import json
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gemini_limits
import marking_requests

from support import SOURCE, load_app


REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeClock:
    """Deterministic clock satisfying both gate and marking-engine calls."""

    def __init__(self, start=100_000.0):
        self.t = start

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds

    def time(self):
        return 1_700_000_000 + self.t / 1000.0


def denied_reason(callable_, *args, **kwargs):
    try:
        callable_(*args, **kwargs)
    except gemini_limits.RateLimitDenied as denied:
        return denied.reason, denied.retry_after, denied.message
    raise AssertionError("expected RateLimitDenied")


def _raise(exc):
    raise exc


# ---------------------------------------------------------------------------
# Identity handling and fail-closed behaviour (Phase 4)
# ---------------------------------------------------------------------------


class IdentityTests(unittest.TestCase):
    def setUp(self):
        gemini_limits.flush_state()
        self.clock = FakeClock()

    def tearDown(self):
        gemini_limits.flush_state()

    def test_anonymous_owner_fails_closed(self):
        reason, retry_after, _ = denied_reason(
            gemini_limits.acquire_job, None, gemini_limits.MANUAL_MARKING, clock=self.clock)
        self.assertEqual(reason, "identity_missing")
        self.assertIsNone(retry_after)
        self.assertEqual(gemini_limits.privacy_probe()["active"], {})

    def test_malformed_or_empty_identifiers_fail_closed(self):
        for bad in ("", "   ", "s", b"byte-identity", 42, True):
            with self.subTest(bad=bad):
                reason, _, _ = denied_reason(
                    gemini_limits.acquire_job, bad, gemini_limits.MANUAL_MARKING,
                    clock=self.clock)
                self.assertEqual(reason, "identity_missing")

    def test_owner_code_is_stable_and_pseudonymous(self):
        raw = "auth0|teacher-subject-here"
        first = gemini_limits.pseudonymous_owner(raw)
        self.assertEqual(first, gemini_limits.pseudonymous_owner(raw))
        self.assertTrue(first.startswith("ow_"))
        self.assertEqual(len(first), len("ow_") + 32)
        self.assertNotIn(raw, first)

    def test_s2_owner_prefix_normalises_to_same_code(self):
        subject = "sub-functional-test-teacher"
        direct = gemini_limits.pseudonymous_owner(subject)
        owner_token = "google:" + subject
        self.assertEqual(direct, gemini_limits.pseudonymous_owner(owner_token))

    def test_different_accounts_map_to_different_codes(self):
        a = gemini_limits.pseudonymous_owner("alpha-subject-0001")
        b = gemini_limits.pseudonymous_owner("beta-subject-0002")
        self.assertNotEqual(a, b)


# ---------------------------------------------------------------------------
# Per-account active-job cap (Phase 5)
# ---------------------------------------------------------------------------


class AccountJobCapTests(unittest.TestCase):
    def setUp(self):
        gemini_limits.flush_state()
        self.clock = FakeClock()

    def tearDown(self):
        gemini_limits.flush_state()

    def test_valid_owner_can_start_one_marking_job(self):
        permit = gemini_limits.acquire_job(
            "sub-teacher-A", gemini_limits.MANUAL_MARKING, clock=self.clock)
        self.assertIsNotNone(permit)
        self.assertEqual(permit.category, gemini_limits.MANUAL_MARKING)
        self.assertTrue(permit.release())
        self.assertFalse(permit.release(), "release must be idempotent")

    def test_second_simultaneous_tab_is_denied(self):
        permit = gemini_limits.acquire_job(
            "sub-teacher-A", gemini_limits.MANUAL_MARKING, clock=self.clock)
        reason, _, message = denied_reason(
            gemini_limits.acquire_job, "sub-teacher-A",
            gemini_limits.MANUAL_MARKING, clock=self.clock)
        self.assertEqual(reason, "account_active_limit")
        self.assertIn("wait", message.lower())
        permit.release()

    def test_manual_and_classroom_share_one_account_cap(self):
        permit = gemini_limits.acquire_job(
            "sub-teacher-A", gemini_limits.MANUAL_MARKING, clock=self.clock)
        reason, _, _ = denied_reason(
            gemini_limits.acquire_job, "sub-teacher-A",
            gemini_limits.CLASSROOM_MARKING, clock=self.clock)
        self.assertEqual(reason, "account_active_limit")
        permit.release()

    def test_released_permit_becomes_available_again(self):
        permit = gemini_limits.acquire_job(
            "sub-teacher-A", gemini_limits.MANUAL_MARKING, clock=self.clock)
        permit.release()
        second = gemini_limits.acquire_job(
            "sub-teacher-A", gemini_limits.MANUAL_MARKING, clock=self.clock)
        self.assertIsNotNone(second)
        second.release()

    def test_second_account_is_independently_limited(self):
        first = gemini_limits.acquire_job(
            "sub-teacher-A", gemini_limits.MANUAL_MARKING, clock=self.clock)
        second = gemini_limits.acquire_job(
            "sub-teacher-B", gemini_limits.CLASSROOM_MARKING, clock=self.clock)
        self.assertIsNotNone(second)
        first.release()
        second.release()

    def test_worker_exception_released_by_caller_does_not_lock_the_account(self):
        permit = gemini_limits.acquire_job(
            "sub-teacher-A", gemini_limits.MANUAL_MARKING, clock=self.clock)
        try:
            raise RuntimeError("synthetic worker failure")
        except RuntimeError:
            pass
        finally:
            permit.release()
        again = gemini_limits.acquire_job(
            "sub-teacher-A", gemini_limits.MANUAL_MARKING, clock=self.clock)
        self.assertIsNotNone(again)
        again.release()

    def test_leaked_permit_ages_out_after_ttl(self):
        permit = gemini_limits.acquire_job(
            "sub-teacher-A", gemini_limits.MANUAL_MARKING, clock=self.clock)
        reason, _, _ = denied_reason(
            gemini_limits.acquire_job, "sub-teacher-A",
            gemini_limits.MANUAL_MARKING, clock=self.clock)
        self.assertEqual(reason, "account_active_limit")
        self.clock.t += gemini_limits.JOB_HOLD_SECONDS + 1
        recovered = gemini_limits.acquire_job(
            "sub-teacher-A", gemini_limits.MANUAL_MARKING, clock=self.clock)
        self.assertIsNotNone(recovered)
        recovered.release()
        permit.release()  # double release after self-expiry stays harmless

    def test_permit_supports_with_statement(self):
        with gemini_limits.acquire_job(
                "sub-teacher-A", gemini_limits.MANUAL_MARKING, clock=self.clock):
            reason, _, _ = denied_reason(
                gemini_limits.acquire_job, "sub-teacher-A",
                gemini_limits.MANUAL_MARKING, clock=self.clock)
            self.assertEqual(reason, "account_active_limit")
        again = gemini_limits.acquire_job(
            "sub-teacher-A", gemini_limits.MANUAL_MARKING, clock=self.clock)
        again.release()

    def test_unknown_category_fails_closed(self):
        reason, _, _ = denied_reason(
            gemini_limits.acquire_job, "sub-teacher-A", "made_up_category",
            clock=self.clock)
        self.assertEqual(reason, "identity_missing")


# ---------------------------------------------------------------------------
# Rolling-window start budgets (Phase 5: rapid repeated jobs)
# ---------------------------------------------------------------------------


class StartBudgetTests(unittest.TestCase):
    def setUp(self):
        gemini_limits.flush_state()
        self.clock = FakeClock()

    def tearDown(self):
        gemini_limits.flush_state()

    def _start_once(self, owner="sub-teacher-A"):
        permit = gemini_limits.acquire_job(
            owner, gemini_limits.MANUAL_MARKING, clock=self.clock)
        permit.release()

    def test_rapid_repeated_starts_are_limited(self):
        for _ in range(gemini_limits.MAX_ACCOUNT_JOB_STARTS):
            self._start_once()
        reason, retry_after, _ = denied_reason(
            gemini_limits.acquire_job, "sub-teacher-A",
            gemini_limits.MANUAL_MARKING, clock=self.clock)
        self.assertEqual(reason, "account_start_rate")
        self.assertTrue(1 <= retry_after <= gemini_limits.ACCOUNT_START_WINDOW_SECONDS)

    def test_window_expiry_restores_the_budget(self):
        for _ in range(gemini_limits.MAX_ACCOUNT_JOB_STARTS):
            self._start_once()
        self.clock.t += gemini_limits.ACCOUNT_START_WINDOW_SECONDS + 1
        permit = gemini_limits.acquire_job(
            "sub-teacher-A", gemini_limits.MANUAL_MARKING, clock=self.clock)
        self.assertIsNotNone(permit)
        permit.release()

    def test_denial_never_counts_as_a_window_start(self):
        for _ in range(gemini_limits.MAX_ACCOUNT_JOB_STARTS):
            self._start_once()
        self.clock.t += gemini_limits.ACCOUNT_START_WINDOW_SECONDS + 1
        try:
            gemini_limits.acquire_job(
                "sub-teacher-A", gemini_limits.MANUAL_MARKING, clock=self.clock)
        except gemini_limits.RateLimitDenied:
            self.fail("a fully expired window must accept a new start")

    def test_global_start_window_limits_many_accounts(self):
        limit = gemini_limits.MAX_GLOBAL_JOB_STARTS
        for index in range(limit):
            self._start_once(f"sub-teacher-{index:03d}")
        reason, _, _ = denied_reason(
            gemini_limits.acquire_job, "sub-teacher-new-account",
            gemini_limits.MANUAL_MARKING, clock=self.clock)
        self.assertEqual(reason, "global_start_rate")

    def test_global_start_window_expires(self):
        limit = gemini_limits.MAX_GLOBAL_JOB_STARTS
        for index in range(limit):
            self._start_once(f"sub-teacher-{index:03d}-x")
        reason, _, _ = denied_reason(
            gemini_limits.acquire_job, "sub-teacher-too-many-x",
            gemini_limits.MANUAL_MARKING, clock=self.clock)
        self.assertEqual(reason, "global_start_rate")
        self.clock.t += gemini_limits.GLOBAL_START_WINDOW_SECONDS + 2
        for index in range(limit):
            self._start_once(f"sub-teacher-next-{index:03d}-x")
        # A start denied inside the window must not consume budget either.
        try:
            gemini_limits.acquire_job(
                "sub-teacher-too-many-x", gemini_limits.MANUAL_MARKING, clock=self.clock)
        except gemini_limits.RateLimitDenied as denied:
            self.assertEqual(denied.reason, "global_start_rate")
        self.clock.t += gemini_limits.GLOBAL_START_WINDOW_SECONDS + 2
        permit = gemini_limits.acquire_job(
            "sub-teacher-last-one-x", gemini_limits.MANUAL_MARKING, clock=self.clock)
        self.assertIsNotNone(permit)
        permit.release()


# ---------------------------------------------------------------------------
# Global active-job cap (Phase 6: shared-resource protection)
# ---------------------------------------------------------------------------


class GlobalCapTests(unittest.TestCase):
    def setUp(self):
        gemini_limits.flush_state()
        self.clock = FakeClock()

    def tearDown(self):
        gemini_limits.flush_state()

    def test_global_concurrency_is_enforced_across_accounts(self):
        holders = []
        for index in range(gemini_limits.MAX_GLOBAL_ACTIVE_JOBS - 1):
            holders.append(gemini_limits.acquire_job(
                f"sub-teacher-{index}", gemini_limits.MANUAL_MARKING, clock=self.clock))
        holders.append(gemini_limits.acquire_job(
            "sub-teacher-spare", gemini_limits.CLASSROOM_MARKING, clock=self.clock))
        reason, _, _ = denied_reason(
            gemini_limits.acquire_job, "sub-teacher-new",
            gemini_limits.MANUAL_MARKING, clock=self.clock)
        self.assertEqual(reason, "global_active_limit")
        holders[0].release()
        allowed = gemini_limits.acquire_job(
            "sub-teacher-new", gemini_limits.MANUAL_MARKING, clock=self.clock)
        self.assertIsNotNone(allowed)
        allowed.release()
        for permit in holders[1:]:
            permit.release()

    def test_global_cap_message_is_service_level_not_account_level(self):
        holders = [
            gemini_limits.acquire_job(
                f"sub-teacher-{i}", gemini_limits.MANUAL_MARKING, clock=self.clock)
            for i in range(gemini_limits.MAX_GLOBAL_ACTIVE_JOBS)]
        reason, _, message = denied_reason(
            gemini_limits.acquire_job, "sub-teacher-new",
            gemini_limits.MANUAL_MARKING, clock=self.clock)
        self.assertEqual(reason, "global_active_limit")
        self.assertIn("temporarily busy", message.lower())
        for permit in holders:
            permit.release()


# ---------------------------------------------------------------------------
# Bridged shared-quota cooldown (Phases 6 and 7)
# ---------------------------------------------------------------------------


class SharedQuotaCooldownTests(unittest.TestCase):
    def setUp(self):
        gemini_limits.flush_state()
        self.clock = FakeClock()

    def tearDown(self):
        gemini_limits.flush_state()

    def test_bridged_cooldown_denies_new_marking_runs(self):
        gemini_limits.note_shared_quota(240, clock=self.clock)
        self.assertTrue(gemini_limits.shared_quota_active(self.clock))
        for category in gemini_limits.JOB_CATEGORIES:
            reason, retry_after, message = denied_reason(
                gemini_limits.acquire_job, "sub-teacher-A", category, clock=self.clock)
            self.assertEqual(reason, "shared_quota_cooldown")
            self.assertTrue(30 <= retry_after <= 240)
            self.assertIn("rate-limit", message.lower())

    def test_bridged_cooldown_decays_with_time(self):
        gemini_limits.note_shared_quota(120, clock=self.clock)
        self.clock.t += 121
        self.assertFalse(gemini_limits.shared_quota_active(self.clock))
        permit = gemini_limits.acquire_job(
            "sub-teacher-A", gemini_limits.MANUAL_MARKING, clock=self.clock)
        permit.release()

    def test_bridged_values_are_clamped_to_a_conservative_range(self):
        self.assertEqual(
            gemini_limits.note_shared_quota(1, clock=self.clock),
            gemini_limits.MIN_BRIDGED_COOLDOWN_SECONDS)
        gemini_limits.flush_state()
        self.assertEqual(
            gemini_limits.note_shared_quota(10 ** 9, clock=self.clock),
            gemini_limits.MAX_BRIDGED_COOLDOWN_SECONDS)
        gemini_limits.flush_state()
        self.assertEqual(
            gemini_limits.note_shared_quota(120, clock=self.clock), 120)

    def test_extension_only_grows_the_deadline(self):
        gemini_limits.note_shared_quota(600, clock=self.clock)
        self.clock.t += 10
        remaining = gemini_limits.note_shared_quota(300, clock=self.clock)
        self.assertGreaterEqual(remaining, 590)  # 600 minus decay, not 300

    def test_non_numeric_bridge_falls_back_to_the_conservative_default(self):
        self.assertEqual(
            gemini_limits.note_shared_quota(None, clock=self.clock),
            gemini_limits.SHARED_QUOTA_COOLDOWN_SECONDS)
        gemini_limits.flush_state()
        self.assertEqual(
            gemini_limits.note_shared_quota(float("nan"), clock=self.clock),
            gemini_limits.SHARED_QUOTA_COOLDOWN_SECONDS)
        gemini_limits.flush_state()
        self.assertEqual(
            gemini_limits.note_shared_quota(True, clock=self.clock),
            gemini_limits.SHARED_QUOTA_COOLDOWN_SECONDS)

    def test_probe_is_not_blocked_by_the_shared_cooldown(self):
        gemini_limits.note_shared_quota(600, clock=self.clock)
        token = gemini_limits.acquire_probe("sub-teacher-A", clock=self.clock)
        self.assertIsNotNone(token)
        token.release()

    def test_long_bridged_cooldown_hint_is_honest(self):
        self.assertEqual(
            gemini_limits.format_retry_hint(21600), "Try again in about 6 hours.")
        self.assertEqual(
            gemini_limits.format_retry_hint(3600), "Try again in about an hour.")
        self.assertEqual(
            gemini_limits.format_retry_hint(300), "Try again in a few minutes.")
        self.assertEqual(
            gemini_limits.format_retry_hint(45), "Try again in about a minute.")
        self.assertEqual(gemini_limits.format_retry_hint(None), "")
        self.assertEqual(gemini_limits.format_retry_hint(0), "")

    def test_many_sessions_cannot_bypass_the_shared_cooldown(self):
        gemini_limits.note_shared_quota(
            gemini_limits.MAX_BRIDGED_COOLDOWN_SECONDS, clock=self.clock)
        for owner in ("sub-teacher-A", "sub-teacher-B", "sub-teacher-C"):
            reason, _, _ = denied_reason(
                gemini_limits.acquire_job, owner,
                gemini_limits.MANUAL_MARKING, clock=self.clock)
            self.assertEqual(reason, "shared_quota_cooldown")


class BridgeMarkingFailureTests(unittest.TestCase):
    def setUp(self):
        gemini_limits.flush_state()
        self.clock = FakeClock()

    def tearDown(self):
        gemini_limits.flush_state()

    def test_shared_quota_failure_bridges_remaining_cooldown(self):
        failure = SimpleNamespace(
            category="quota_limited", request_id="abc123", remaining=3600)
        self.assertEqual(
            gemini_limits.bridge_marking_failure(failure, clock=self.clock), 3600)
        self.assertTrue(gemini_limits.shared_quota_active(self.clock))
        reason, retry_after, _ = denied_reason(
            gemini_limits.acquire_job, "sub-teacher-A",
            gemini_limits.MANUAL_MARKING, clock=self.clock)
        self.assertEqual(reason, "shared_quota_cooldown")
        self.assertEqual(retry_after, 3600)

    def test_cooldown_failure_bridges_conservatively(self):
        failure = SimpleNamespace(
            category="cooldown", request_id="abc124", remaining=None)
        self.assertEqual(
            gemini_limits.bridge_marking_failure(failure, clock=self.clock),
            gemini_limits.SHARED_QUOTA_COOLDOWN_SECONDS)
        self.assertTrue(gemini_limits.shared_quota_active(self.clock))

    def test_non_marking_failures_are_ignored(self):
        for failure in (
                SimpleNamespace(category="timeout", request_id="x", remaining=99999),
                SimpleNamespace(category="transport_or_client_error", remaining=10),
                RuntimeError("plain failure"),
                None):
            self.assertEqual(gemini_limits.bridge_marking_failure(failure), 0)
        self.assertFalse(gemini_limits.shared_quota_active(self.clock))

    def test_failure_without_correlation_id_is_ignored(self):
        failure = SimpleNamespace(category="quota_limited", remaining=600)
        self.assertEqual(gemini_limits.bridge_marking_failure(failure), 0)
        self.assertFalse(gemini_limits.shared_quota_active(self.clock))


# ---------------------------------------------------------------------------
# Real marking-engine bridge (Phase 7): Retry-After respected and bounded
# ---------------------------------------------------------------------------


class MarkingEngineBridgeIntegrationTests(unittest.TestCase):
    """Drive the REAL PR #15 engine with a shared-scope 429 and verify the bridge."""

    def setUp(self):
        gemini_limits.flush_state()
        self.clock = FakeClock()

    def tearDown(self):
        gemini_limits.flush_state()

    class _Shared429(Exception):
        code = 429

    @staticmethod
    def _schema():
        return {"type": "object"}

    def test_generate_attaches_remaining_and_the_bridge_denies_the_next_run(self):
        client = SimpleNamespace(
            models=SimpleNamespace(
                generate_content=Mock(side_effect=MarkingEngineBridgeIntegrationTests._Shared429(
                    "429 quota exceeded (shared scope synthetic for offline test)"))))
        with self.assertRaises(marking_requests.MarkingFailure) as caught:
            marking_requests.generate(
                client, ["gemini-2.5-flash"], ["synthetic contents"], "system",
                self._schema(), lambda **kwargs: {},
                lambda text: json.loads(text), progress=None, clock=self.clock)
        failure = caught.exception
        self.assertEqual(failure.category, "quota_limited")
        self.assertEqual(failure.remaining, 90)  # conservative provider interval
        self.assertEqual(
            gemini_limits.bridge_marking_failure(failure, clock=self.clock), 90)
        reason, retry_after, _ = denied_reason(
            gemini_limits.acquire_job, "sub-teacher-A",
            gemini_limits.MANUAL_MARKING, clock=self.clock)
        self.assertEqual(reason, "shared_quota_cooldown")
        self.assertEqual(retry_after, 90)

    def test_fresh_clients_have_empty_retry_state_but_the_gate_refuses(self):
        client_a = SimpleNamespace()
        client_b = SimpleNamespace()
        state_a = marking_requests.state_for(client_a)
        state_b = marking_requests.state_for(client_b)
        self.assertIsNot(state_a, state_b)  # cooldowns are per SDK client
        self.assertEqual(state_b["blocked"], {})
        self.assertEqual(state_b["account_until"], 0)
        failure = marking_requests.MarkingFailure(
            "quota_limited", "corr-s4-fresh", remaining=3600)
        self.assertEqual(
            gemini_limits.bridge_marking_failure(failure, clock=self.clock), 3600)
        reason, _, _ = denied_reason(
            gemini_limits.acquire_job, "sub-teacher-A",
            gemini_limits.MANUAL_MARKING, clock=self.clock)
        self.assertEqual(reason, "shared_quota_cooldown")

    def test_ambiguous_timeout_is_not_bridged_or_replayed(self):
        class AmbiguousTimeout(Exception):
            code = None

        client = SimpleNamespace(
            models=SimpleNamespace(
                generate_content=Mock(side_effect=AmbiguousTimeout("synthetic transport"))))
        with self.assertRaises(marking_requests.MarkingFailure) as caught:
            marking_requests.generate(
                client, ["gemini-2.5-flash"], ["synthetic contents"], "system",
                self._schema(), lambda **kwargs: {},
                lambda text: json.loads(text), progress=None, clock=self.clock)
        self.assertEqual(caught.exception.category, "timeout")
        self.assertEqual(
            gemini_limits.bridge_marking_failure(caught.exception, clock=self.clock), 0)
        self.assertFalse(gemini_limits.shared_quota_active(self.clock))


# ---------------------------------------------------------------------------
# API-key probe budget (Phase 6)
# ---------------------------------------------------------------------------


class ProbeGateTests(unittest.TestCase):
    def setUp(self):
        gemini_limits.flush_state()
        self.clock = FakeClock()

    def tearDown(self):
        gemini_limits.flush_state()

    def test_authenticated_probe_is_allowed_within_budget(self):
        token = gemini_limits.acquire_probe("sub-teacher-A", clock=self.clock)
        self.assertIsNotNone(token)
        token.release()

    def test_repeated_probes_are_rate_limited(self):
        for _ in range(gemini_limits.MAX_PROBE_STARTS):
            token = gemini_limits.acquire_probe("sub-teacher-A", clock=self.clock)
            token.release()
        reason, retry_after, message = denied_reason(
            gemini_limits.acquire_probe, "sub-teacher-A", clock=self.clock)
        self.assertEqual(reason, "probe_rate")
        self.assertTrue(0 < retry_after <= gemini_limits.PROBE_WINDOW_SECONDS)
        self.assertIn("api key checks", message.lower())

    def test_probe_window_expiry(self):
        for _ in range(gemini_limits.MAX_PROBE_STARTS):
            token = gemini_limits.acquire_probe("sub-teacher-A", clock=self.clock)
            token.release()
        self.clock.t += gemini_limits.PROBE_WINDOW_SECONDS + 1
        token = gemini_limits.acquire_probe("sub-teacher-A", clock=self.clock)
        self.assertIsNotNone(token)
        token.release()

    def test_probes_are_per_account(self):
        for _ in range(gemini_limits.MAX_PROBE_STARTS):
            token = gemini_limits.acquire_probe("sub-teacher-A", clock=self.clock)
            token.release()
        token = gemini_limits.acquire_probe("sub-teacher-B", clock=self.clock)
        self.assertIsNotNone(token)
        token.release()

    def test_probe_denials_do_not_block_marking_jobs(self):
        for _ in range(gemini_limits.MAX_PROBE_STARTS + 3):
            try:
                gemini_limits.acquire_probe("sub-teacher-A", clock=self.clock).release()
            except gemini_limits.RateLimitDenied:
                pass
        permit = gemini_limits.acquire_job(
            "sub-teacher-A", gemini_limits.MANUAL_MARKING, clock=self.clock)
        self.assertIsNotNone(permit)
        permit.release()


# ---------------------------------------------------------------------------
# Concurrency and atomicity (Phase 8)
# ---------------------------------------------------------------------------


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        gemini_limits.flush_state()

    def tearDown(self):
        gemini_limits.flush_state()

    def test_threads_cannot_both_pass_the_account_cap(self):
        owner = "sub-teacher-race"
        grants, denial_reasons = [], []

        def attempt():
            try:
                gemini_limits.acquire_job(owner, gemini_limits.MANUAL_MARKING)
                grants.append(1)
            except gemini_limits.RateLimitDenied as denied:
                denial_reasons.append(denied.reason)

        threads = [threading.Thread(target=attempt) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(len(grants), 1)
        self.assertNotIn("identity_missing", denial_reasons)
        self.assertNotIn("shared_quota_cooldown", denial_reasons)
        self.assertTrue(all(
            reason in ("account_active_limit", "account_start_rate")
            for reason in denial_reasons))

    def test_global_cap_holds_under_mixed_account_contention(self):
        cap = gemini_limits.MAX_GLOBAL_ACTIVE_JOBS
        holders, global_denials = [], []
        holders_lock = threading.Lock()

        def attempt(index):
            try:
                permit = gemini_limits.acquire_job(
                    f"sub-teacher-race-{index}", gemini_limits.MANUAL_MARKING)
                with holders_lock:
                    holders.append(permit)
            except gemini_limits.RateLimitDenied as denied:
                with holders_lock:
                    global_denials.append(denied.reason)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(cap + 6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(len(holders), cap)
        self.assertEqual(global_denials.count("global_active_limit"), 6)
        for permit in list(holders):
            permit.release()

    def test_no_negative_counters_after_release_storm(self):
        permits = [
            gemini_limits.acquire_job(
                f"sub-teacher-{i}", gemini_limits.MANUAL_MARKING)
            for i in range(gemini_limits.MAX_GLOBAL_ACTIVE_JOBS)]
        for permit in permits:
            permit.release()
        permit.release()  # extra idempotent release attempts stay harmless
        records = gemini_limits.privacy_probe()
        self.assertEqual(records["active"], {})
        self.assertEqual(
            [count for count in records["active"].values() if count < 0], [])

    def test_leaked_permit_from_killed_rerun_ages_out(self):
        clock = FakeClock()
        gemini_limits.acquire_job(
            "sub-teacher-leaky", gemini_limits.MANUAL_MARKING, clock=clock)
        clock.t += gemini_limits.JOB_HOLD_SECONDS + 1
        permit = gemini_limits.acquire_job(
            "sub-teacher-leaky", gemini_limits.MANUAL_MARKING, clock=clock)
        self.assertIsNotNone(permit)
        permit.release()


class StateBoundingTests(unittest.TestCase):
    def setUp(self):
        gemini_limits.flush_state()

    def tearDown(self):
        gemini_limits.flush_state()

    def test_owner_windows_are_pruned_to_a_bounded_table(self):
        clock = FakeClock()
        for index in range(gemini_limits.MAX_TRACKED_OWNERS + 50):
            permit = gemini_limits.acquire_job(
                f"sub-teacher-bulk-{index:05d}", gemini_limits.MANUAL_MARKING, clock=clock)
            permit.release()
            clock.t += 305  # keep each separate start inside a fresh window
        records = gemini_limits.privacy_probe()
        self.assertLessEqual(
            len(records["job_starts"]), gemini_limits.MAX_TRACKED_OWNERS)

    def test_diagnostic_ring_is_bounded(self):
        clock = FakeClock()
        for index in range(gemini_limits.DIAGNOSTIC_RING_CAPACITY * 4):
            try:
                gemini_limits.acquire_job(
                    "sub-teacher-A", gemini_limits.MANUAL_MARKING, clock=clock).release()
            except gemini_limits.RateLimitDenied:
                pass
            clock.t += 7
        self.assertEqual(
            len(gemini_limits.diagnostic_snapshot()),
            gemini_limits.DIAGNOSTIC_RING_CAPACITY)

    def test_global_start_window_list_is_bounded(self):
        clock = FakeClock()
        for index in range(gemini_limits.MAX_GLOBAL_JOB_STARTS * 70):
            permit = gemini_limits.acquire_job(
                f"sub-teacher-global-{index:05d}", gemini_limits.MANUAL_MARKING, clock=clock)
            permit.release()
            clock.t += 305  # avoid the shared 10-per-5-minute start budget
        self.assertLessEqual(
            len(gemini_limits.privacy_probe()["global_starts"]),
            gemini_limits.MAX_GLOBAL_JOB_STARTS * 64)


# ---------------------------------------------------------------------------
# Privacy-safe state and diagnostics (Phases 4 and 9)
# ---------------------------------------------------------------------------


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        gemini_limits.flush_state()
        self.clock = FakeClock()

    def tearDown(self):
        gemini_limits.flush_state()

    def test_internal_state_stores_no_raw_identifiers(self):
        raw = "sub-teacher-sensitive-subject-9f3a71c0"
        permit = gemini_limits.acquire_job(
            raw, gemini_limits.MANUAL_MARKING, clock=self.clock)
        permit.release()
        blob = json.dumps(gemini_limits.privacy_probe())
        self.assertNotIn(raw, blob)
        self.assertNotIn("9f3a71c0", blob)

    def test_diagnostics_expose_only_allowlisted_fields(self):
        permitted = {
            "event", "category", "granted", "reason",
            "retry_after_seconds", "active_jobs_global", "correlation_id",
        }
        permit = gemini_limits.acquire_job(
            "sub-teacher-A", gemini_limits.MANUAL_MARKING, clock=self.clock)
        permit.release()
        try:
            gemini_limits.acquire_job(
                "sub-teacher-A", gemini_limits.MANUAL_MARKING, clock=self.clock)
        except gemini_limits.RateLimitDenied:
            pass
        snapshot = gemini_limits.diagnostic_snapshot()
        self.assertGreaterEqual(len(snapshot), 2)
        for entry in snapshot:
            self.assertEqual(set(entry), permitted)
            self.assertRegex(entry["correlation_id"], r"^[0-9a-f]{32}$")

    def test_all_user_visible_denials_are_content_free(self):
        clock = FakeClock()
        seen = []
        raw = "sub-teacher-content-test-93ab"
        for _ in range(gemini_limits.MAX_ACCOUNT_JOB_STARTS + 4):
            try:
                gemini_limits.acquire_job(
                    raw, gemini_limits.MANUAL_MARKING, clock=clock)
            except gemini_limits.RateLimitDenied as denied:
                seen.append((denied.reason, denied.message))
            clock.t += 2
        self.assertTrue(seen)
        for reason, message in seen:
            self.assertIn(reason, gemini_limits.MESSAGES)
            self.assertNotIn(raw, message)
            self.assertLess(len(message), 400)

    def test_every_declared_message_is_content_free_and_helpful(self):
        for reason, message in gemini_limits.MESSAGES.items():
            with self.subTest(reason=reason):
                self.assertIsInstance(message, str)
                self.assertTrue(message.strip())
                self.assertLess(len(message), 400)


# ---------------------------------------------------------------------------
# Import-time policy overrides (subprocess, mirrors test_document_limits)
# ---------------------------------------------------------------------------


class EnvOverrideTests(unittest.TestCase):
    REPO_ROOT = REPO_ROOT

    def subprocess_value(self, env_name, value, probe):
        """Run one fresh-interpreter probe from two different directories."""
        code = (
            "import os, sys\n"
            f"sys.path.insert(0, {str(self.REPO_ROOT)!r})\n"
            f"os.environ.pop({env_name!r}, None)\n"
            + (f"os.environ[{env_name!r}] = {value!r}\n" if value is not None else "")
            + probe
        )
        outputs = set()
        for directory in (self.REPO_ROOT, Path(__file__).resolve().parent):
            result = subprocess.run(
                [sys.executable, "-c", code], capture_output=True, text=True,
                cwd=str(directory))
            self.assertEqual(
                result.returncode, 0,
                f"subprocess failed (cwd={directory}):\n{result.stderr}")
            outputs.add(result.stdout.strip())
        return outputs

    def test_account_active_cap_override_is_read_once_and_clamped(self):
        probe = (
            "import gemini_limits as g\n"
            "try:\n"
            "    g.acquire_job('sub-teacher-x', g.MANUAL_MARKING)\n"
            "    g.acquire_job('sub-teacher-x', g.MANUAL_MARKING)\n"
            "    print('granted:second')\n"
            "except g.RateLimitDenied as denied:\n"
            "    print('denied:' + denied.reason)\n"
        )
        self.assertEqual(
            self.subprocess_value("MARKER_ACCOUNT_ACTIVE_JOBS", None, probe),
            {"denied:account_active_limit"})
        self.assertEqual(
            self.subprocess_value("MARKER_ACCOUNT_ACTIVE_JOBS", "2", probe),
            {"granted:second"})
        self.assertEqual(
            self.subprocess_value("MARKER_ACCOUNT_ACTIVE_JOBS", "0", probe),
            {"denied:account_active_limit"})
        self.assertEqual(
            self.subprocess_value("MARKER_ACCOUNT_ACTIVE_JOBS", "9999", probe),
            {"granted:second"})

    def test_shared_cooldown_override_is_clamped(self):
        probe = (
            "import os, gemini_limits as g\n"
            "first = g.SHARED_QUOTA_COOLDOWN_SECONDS\n"
            "os.environ['MARKER_SHARED_QUOTA_COOLDOWN'] = '9999'\n"
            "after = g.SHARED_QUOTA_COOLDOWN_SECONDS\n"
            "print(f'{first}|{after}')\n"
        )
        self.assertEqual(
            self.subprocess_value("MARKER_SHARED_QUOTA_COOLDOWN", None, probe),
            {"90|90"})
        self.assertEqual(
            self.subprocess_value("MARKER_SHARED_QUOTA_COOLDOWN", "100", probe),
            {"100|100"})
        self.assertEqual(
            self.subprocess_value("MARKER_SHARED_QUOTA_COOLDOWN", "5", probe),
            {"30|30"})
        self.assertEqual(
            self.subprocess_value("MARKER_SHARED_QUOTA_COOLDOWN", "50000", probe),
            {"21600|21600"})


# ---------------------------------------------------------------------------
# Production app integration (Phases 5-7): real loaded app code + fakes
# ---------------------------------------------------------------------------


class AppMarkingBoundaryTests(unittest.TestCase):
    """Run the loaded app's per-student marking path with coerced fakes."""

    def setUp(self):
        gemini_limits.flush_state()
        self.f = load_app()
        self.f["model_on_quota_cooldown"] = lambda _model: False
        self.f["remember_model_failure"] = lambda *_args, **_kwargs: None
        self.clock = FakeClock()

    def tearDown(self):
        gemini_limits.flush_state()

    @staticmethod
    def _good_generation(holder):
        def generate_content(*args, **kwargs):
            holder.append(kwargs.get("model"))
            return SimpleNamespace(text=json.dumps({
                "questions": [{
                    "question_number": "1", "max_marks": 5, "awarded_marks": 5,
                    "student_answer": "x with visible student working shown",
                    "working_summary": "x", "correct_method": "m",
                    "full_mark_solution": "s", "scheme_answer_evidence": "e",
                    "transcription_uncertainty": None,
                    "mark_breakdown": [{"code": "A1", "awarded": True, "reason": "r"}],
                }]
            }))
        return generate_content

    def test_valid_owner_marks_one_student_through_the_real_path(self):
        holder = []
        client = SimpleNamespace(
            models=SimpleNamespace(generate_content=self._good_generation(holder)))
        result = self.f["mark_one_submission"](
            client, "synthetic-student", "synthetic-file", "synthetic-scheme",
            None, False)
        self.assertEqual(result.get("status"), "Marked")
        self.assertEqual(len(holder), 1)
        self.assertIn(holder[0], self.f["PRIMARY_MODELS"])

    def test_bridge_records_shared_quota_for_marking_failure(self):
        failure = marking_requests.MarkingFailure(
            "quota_limited", "correlation-0001", remaining=180)

        def failing_generation(*args, **kwargs):
            raise failure

        self.f["grade_with_model"] = failing_generation
        with self.assertRaises(marking_requests.MarkingFailure):
            self.f["_mark_one_submission"](
                SimpleNamespace(), "synthetic-student", "file", "scheme",
                None, False)
        self.assertTrue(gemini_limits.shared_quota_active())
        reason, retry_after, _ = denied_reason(
            gemini_limits.acquire_job, "sub-teacher-A", gemini_limits.MANUAL_MARKING)
        self.assertEqual(reason, "shared_quota_cooldown")
        self.assertEqual(retry_after, 180)

    def test_job_wrapper_also_bridges_the_failure(self):
        failure = marking_requests.MarkingFailure(
            "cooldown", "correlation-0007", remaining=None)
        self.f["grade_with_model"] = lambda *a, **k: _raise(failure)
        with self.assertRaises(marking_requests.MarkingFailure):
            self.f["mark_one_submission"](
                SimpleNamespace(), "synthetic-student", "file", "scheme",
                None, False)
        self.assertTrue(gemini_limits.shared_quota_active())

    def test_plain_worker_failure_is_never_bridged(self):
        def failing_generation(*args, **kwargs):
            raise RuntimeError("synthetic ordinary failure")

        self.f["grade_with_model"] = failing_generation
        with self.assertRaises(RuntimeError):
            self.f["_mark_one_submission"](
                SimpleNamespace(), "synthetic-student", "file", "scheme",
                None, False)
        self.assertFalse(gemini_limits.shared_quota_active())

    def test_per_model_cooldown_filters_models_at_the_generation_boundary(self):
        blocked = self.f["PRIMARY_MODELS"][0]
        self.f["model_on_quota_cooldown"] = lambda model: model == blocked
        holder = []
        client = SimpleNamespace(
            models=SimpleNamespace(generate_content=self._good_generation(holder)))
        self.f["mark_one_submission"](
            client, "synthetic-student", "file", "scheme", None, False)
        self.assertEqual(len(holder), 1)
        self.assertNotIn(blocked, holder)

    def test_every_model_cooled_down_fails_before_any_provider_call(self):
        self.f["model_on_quota_cooldown"] = lambda _model: True
        client = SimpleNamespace(
            models=SimpleNamespace(generate_content=Mock(
                side_effect=AssertionError("unexpected provider call"))))
        with self.assertRaises(marking_requests.MarkingFailure):
            self.f["mark_one_submission"](
                client, "synthetic-student", "file", "scheme", None, False)
        self.assertFalse(client.models.generate_content.called)


# ---------------------------------------------------------------------------
# Top-level boundary source probes (loader cannot execute those blocks)
# ---------------------------------------------------------------------------


class TopLevelBoundarySourceTests(unittest.TestCase):
    def test_manual_gate_precedes_client_creation_and_covers_release(self):
        source = SOURCE.read_text(encoding="utf-8")
        acquiring = source.index("manual_permit = gemini_limits.acquire_job(")
        client_creation = source.index("client = genai.Client(", acquiring)
        self.assertLess(acquiring, client_creation)
        denied_handler = source.index(
            "except gemini_limits.RateLimitDenied as denied:", acquiring)
        generic_handler = source.index("except Exception as exc:", acquiring)
        self.assertLess(denied_handler, generic_handler)
        manual_finally = source.index("finally:", generic_handler)
        release = source.index("manual_permit.release()", manual_finally)
        region_end = source.index(
            'if "last_batch_results" in st.session_state:', manual_finally)
        self.assertLess(release, region_end)

    def test_manual_denial_message_reaches_the_teacher_content_free(self):
        source = SOURCE.read_text(encoding="utf-8")
        acquiring = source.index("manual_permit = gemini_limits.acquire_job(")
        denied_handler = source.index(
            "except gemini_limits.RateLimitDenied as denied:", acquiring)
        self.assertIn(
            "st.error(denied.message)",
            source[denied_handler:source.index("except Exception as exc:", denied_handler)])

    def test_classroom_gate_precedes_classroom_client_creation(self):
        source = SOURCE.read_text(encoding="utf-8")
        job_owner = source.index("job_owner = sensitive_state_owner()")
        reservation = source.index("classroom_permit = (", job_owner)
        acquiring = source.index("gemini_limits.acquire_job(", job_owner)
        marking = source.index("gemini_limits.CLASSROOM_MARKING", acquiring)
        client_creation = source.index("classroom_client = genai.Client(", acquiring)
        self.assertLess(reservation, acquiring)
        self.assertLess(marking, client_creation)
        self.assertLess(acquiring, client_creation)
        denied_handler = source.index(
            "except gemini_limits.RateLimitDenied as denied:", acquiring)
        generic_handler = source.index(
            "except Exception as classroom_error:", acquiring)
        self.assertLess(denied_handler, generic_handler)
        release = source.index("classroom_permit.release()", denied_handler)
        self.assertGreater(release, denied_handler)

    def test_api_key_probe_is_rate_limited_after_the_authentication_guard(self):
        source = SOURCE.read_text(encoding="utf-8")
        guard = source.index("if authenticated_google_identity() is None:")
        probe = source.index("acquire_probe(", guard)
        key_call = source.index("test_api_key(api_key)", probe)
        self.assertLess(probe, key_call)
        denied_handler = source.index(
            "except gemini_limits.RateLimitDenied as denied:", probe)
        self.assertLess(denied_handler, key_call)

    def test_probe_denial_never_replaces_the_key_test(self):
        source = SOURCE.read_text(encoding="utf-8")
        probe = source.index("acquire_probe(")
        denied_handler = source.index(
            "except gemini_limits.RateLimitDenied as denied:", probe)
        else_block = source.index("else:", denied_handler)
        key_call = source.index("test_api_key(api_key)", else_block)
        release = source.index("probe_token.release()", else_block)
        success = source.index("st.success(message)", else_block)
        self.assertLess(key_call, release)
        self.assertLess(release, success)

    def test_marking_failure_bridge_present_in_the_student_path(self):
        source = SOURCE.read_text(encoding="utf-8")
        bridge = source.index("gemini_limits.bridge_marking_failure(marking_exc)")
        self.assertGreater(bridge, source.index("def _mark_one_submission("))
        self.assertLess(bridge, source.index("def test_api_key("))

    def test_model_cooldown_filter_present_in_the_student_path(self):
        source = SOURCE.read_text(encoding="utf-8")
        labelled = source.index("usable_models = [m for m in PRIMARY_MODELS")
        self.assertGreater(labelled, source.index("def _mark_one_submission("))
        self.assertLess(labelled, source.index("def test_api_key("))
        self.assertLess(
            labelled, source.index("gemini_limits.bridge_marking_failure"))


# ---------------------------------------------------------------------------
# Earlier-stage guarantees are untouched (spot checks)
# ---------------------------------------------------------------------------


class ExistingProtectionSpotChecks(unittest.TestCase):
    def test_document_upload_semaphore_is_untouched(self):
        source = (REPO_ROOT / "document_pipeline.py").read_text(encoding="utf-8")
        self.assertIn("BoundedSemaphore(2)", source)
        self.assertIn("with self.cache.upload_slots:", source)

    def test_marking_attempt_ceilings_are_untouched(self):
        source = (REPO_ROOT / "marking_requests.py").read_text(encoding="utf-8")
        self.assertIn("MAX_ATTEMPTS = 2", source)
        self.assertIn("MAX_REQUESTS = 4", source)
        self.assertIn("TOTAL_BUDGET = 180", source)

    def test_marking_failure_message_contract_is_unchanged(self):
        failure = marking_requests.MarkingFailure("quota_limited", "sys-correlation")
        self.assertEqual(failure.remaining, None)
        self.assertIn("rate-limited", str(failure))
        legacy = marking_requests.MarkingFailure("timeout", "sys-correlation-2")
        self.assertIn("timed out", str(legacy))


if __name__ == "__main__":
    unittest.main()
