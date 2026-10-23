"""C6-B5 tests: named expired-token (401) mid-decision fail-closed proof.

Closes IR gap #2 (C6-B4 review report section 3/4): C6-B2 proved a GENERIC
provider failure (``RuntimeError("network down")`` on a faked listing
function) fails closed, and C6-B4 proved the whole review chain, but no
named test drove an EXPIRED Google credential through the REAL HTTP
boundary — ``classroom_request`` -> ``classroom_list_all`` ->
``reverify_classroom_review_access`` -> ``apply_question_review_decision`` —
while a decision was in flight.

Offline only: the provider boundary is a routed fake of the loaded app's
own ``requests`` object returning synthetic status codes and plain listing
dicts (exactly the shapes the production helpers parse). No live
Classroom/Google request, no credential, no real student data. The
token and error-body canaries below are synthetic strings; every assertion
that names them proves LEAK-FREE fail-closed behaviour, not a secret.

Covered here:
  - the same decision path that succeeds over a fresh 200 listing
    (positive control through the real HTTP boundary, not a mock listing);
  - once the token expires (HTTP 401 mid-decision) the decision fails
    closed with exactly ONE new provider call, no session decision record,
    no override, no durable audit event and the earlier accepted decision
    untouched;
  - the raised provider error carries ONLY the status code: neither the
    bearer token nor the response body canary ever reaches the exception
    text or the user-facing notice (S6-R1 privacy);
  - after the credential refreshes (the OIDC layer renews the token
    between attempts) the very next decision succeeds on its own fresh
    re-verification — nothing from the expired attempt is repaired,
    replayed or reused as authorization.
"""
import hashlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_c6b2_security as c6b2
import test_c6b3_m2_app_integration as m2
from support import response

EXPIRED_TOKEN = "synthetic-expired-bearer-4f01"
BODY_CANARY = "expired_credential_body_canary_5bd3"
FRESH_TOKEN = "synthetic-refreshed-bearer-0k2"


class HttpBoundaryState:
    """Routed fake of the app's own ``requests`` object.

    ``status`` flips the provider into 401 (expired credential) mid-journey;
    ``calls`` records every outbound request so the test can count retries.
    """

    def __init__(self):
        self.status = 200
        self.calls = []

    def request(self, *, method, url, headers, timeout, params=None, **kwargs):
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "params": dict(params or {})})
        if self.status != 200:
            # A real expired token answers 401 with a JSON error body; the
            # body canary must never surface through the privacy-safe error.
            return response(status=self.status,
                            content=('{\"canary\": \"%s\"}' % BODY_CANARY).encode())
        if url.endswith("/courses"):
            return response(data={"courses": [dict(item) for item in c6b2.COURSES]})
        if url.endswith("/courseWork"):
            return response(data={"courseWork": [dict(item)
                                                 for item in c6b2.ASSIGNMENTS]})
        raise AssertionError("unexpected listing url %r" % url)

    def install(self, scope):
        scope["requests"] = SimpleNamespace(
            request=Mock(side_effect=self.request),
            get=Mock(side_effect=AssertionError("Unexpected HTTP download")))


class ExpiredTokenBoundaryTests(m2.AppPersistenceTestCase):
    """The named 401 path runs through the REAL classroom HTTP boundary."""

    def setUp(self):
        super().setUp()
        self.enable_store()
        self.http = HttpBoundaryState()
        self.http.install(self.f)
        # The app reads the token fresh at every decision (Streamlit OIDC
        # hands back an expired or renewed access token between attempts).
        self.token = {"value": EXPIRED_TOKEN}
        self.f["get_google_access_token"] = lambda: self.token["value"]
        self.f["record_classroom_grants"](list(c6b2.COURSES),
                                          list(c6b2.ASSIGNMENTS), "course-1")
        self.row = self.journey_row()
        self.key = self.row["review_key"]
        self.install(self.row)

    def journey_row(self, numbers=("1", "2", "3")):
        row = self.classroom_row(tuple(numbers))
        earned, available, percentage = self.f["calculate_totals"](row["result"])
        row["totals"] = {"earned": earned, "available": available,
                         "percentage": percentage,
                         "provisional_grade":
                             self.f["percentage_band_grade"](percentage)}
        return row

    def test_fresh_token_decision_succeeds_through_real_http_boundary(self):
        # Positive control: this is the real list_teacher_courses /
        # list_assignments / classroom_request chain, not a mock listing —
        # so the 401 tests below prove the failure at the SAME gate that
        # actually works while the credential is valid.
        self.assertEqual(self.token["value"], EXPIRED_TOKEN)
        self.assertEqual(self.decision(self.key, "1", action="approve")[0], "ok")
        urls = [call["url"] for call in self.http.calls]
        self.assertTrue(any(url.endswith("/courses") for url in urls), urls)
        self.assertTrue(any(url.endswith("/courseWork") for url in urls), urls)
        self.assertEqual(self.http.calls[0]["headers"]["Authorization"],
                         "Bearer " + EXPIRED_TOKEN)
        _, events = self.stream(self.row)
        self.assertEqual(len(events), 1)

    def test_expired_token_401_mid_decision_fails_closed_with_no_partial_state(self):
        # One accepted decision first, then the credential expires.
        self.assertEqual(self.decision(self.key, "1", action="approve")[0], "ok")
        calls_before = len(self.http.calls)
        self.http.status = 401

        code, number, detail = self.decision(self.key, "2", action="correct",
                                             reason="must not survive expiry",
                                             mark=2)
        self.assertEqual(code, "classroom_verification_unavailable")
        # Exactly ONE new provider call: the courses listing answered 401 and
        # the decision stopped immediately — no retry storm, no assignment
        # listing attempted, no cached authorization replayed.
        self.assertEqual(len(self.http.calls), calls_before + 1)
        # And the refused decision left NOTHING behind anywhere in the chain.
        self.assertNotIn("%s::q2" % self.key,
                         self.session.get("question_review_decisions", {}))
        self.assertNotIn("2", self.session.get("teacher_overrides", {}).get(
            self.key, {}))
        _, events = self.stream(self.row)
        self.assertEqual([event["sequence"] for event in events], [1])
        self.assertEqual(events[0]["question_number"], "1")
        # The earlier accepted decision is untouched by the expired attempt.
        self.assertEqual(self.session["question_review_decisions"]
                         ["%s::q1" % self.key]["status"], "approved")
        # Neither the bearer token nor the provider error body can reach the
        # user-facing notice (S6-R1 privacy: only the fail-closed code shows).
        notice_repr = repr((code, number, detail))
        self.assertNotIn(EXPIRED_TOKEN, notice_repr)
        self.assertNotIn(BODY_CANARY, notice_repr)

    def test_expired_attempt_is_never_replayed_and_refreshed_token_recovers(self):
        self.assertEqual(self.decision(self.key, "1", action="approve")[0], "ok")
        self.http.status = 401
        self.assertEqual(self.decision(self.key, "2", action="correct",
                                       reason="blocked", mark=2)[0],
                         "classroom_verification_unavailable")
        # Repeating the decision while still expired fails closed again with
        # a FULL fresh re-verification: the failed attempt stored no
        # authorization that could be replayed.
        calls_before = len(self.http.calls)
        self.assertEqual(self.decision(self.key, "2", action="correct",
                                       reason="blocked again", mark=2)[0],
                         "classroom_verification_unavailable")
        self.assertEqual(len(self.http.calls), calls_before + 1)
        self.assertNotIn("%s::q2" % self.key,
                         self.session.get("question_review_decisions", {}))

        # The OIDC layer refreshes the token: the next decision runs against
        # the renewed credential and succeeds on its own fresh verification.
        # Nothing from the expired attempts is repaired or reused.
        self.http.status = 200
        self.token["value"] = FRESH_TOKEN
        self.assertEqual(self.decision(self.key, "2", action="correct",
                                       reason="scheme B1 missed a mark",
                                       mark=2)[0], "ok")
        record = self.session["question_review_decisions"]["%s::q2" % self.key]
        self.assertEqual(record["status"], "corrected")
        self.assertEqual(record["audit_reference"]["sequence"], 2)
        self.assertEqual(self.session["teacher_overrides"][self.key]["2"], 2)
        _, events = self.stream(self.row)
        self.assertEqual([event["sequence"] for event in events], [1, 2])
        # The refreshed decision re-verified with the NEW token only.
        self.assertEqual(self.http.calls[-1]["headers"]["Authorization"],
                         "Bearer " + FRESH_TOKEN)

    def test_provider_401_error_text_is_status_code_only(self):
        # Direct boundary proof of the privacy rule the fail-closed notices
        # rely on: classroom_request raises with ONLY the status code — the
        # bearer token, the listing URL parameters, the raw provider body
        # and its canary are never echoed (they could leak credentials or
        # student content into logs or the UI).
        self.http.status = 401
        with self.assertRaises(RuntimeError) as caught:
            self.f["classroom_request"](
                "GET", "https://classroomexamples/v1/courses",
                EXPIRED_TOKEN, params={"teacherId": "me"})
        self.assertEqual(str(caught.exception), "Google API error 401.")
        message = str(caught.exception)
        for secret in (EXPIRED_TOKEN, BODY_CANARY, "teacherId"):
            self.assertNotIn(secret, message)


if __name__ == "__main__":
    unittest.main()
