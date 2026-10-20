"""Mocked provider tests for Phase C Stage 5 scheme review.

Every provider in this module is a local Python callable. There are no
network calls, no Gemini SDK imports and no credentials. The tests assert
that provider output is always a bounded *proposal*: it cannot add, remove or
reweight criteria, cannot change extracted or verified information, cannot
approve anything, and cannot bypass deterministic findings.
"""
import copy
import json
import unittest

from investigation import scheme_review as review
from investigation.test_scheme_review import (
    OTHER_SHA, SHA, _base_claims, _base_questions, _claim, _decision,
    _document, _finding, _payload)

PROVIDER_CATEGORIES = review.PROVIDER_CATEGORIES


def _finding_payload(**overrides):
    payload = {
        "category": "TOTAL_MISMATCH",
        "question_id": "Q1",
        "criterion_ids": [],
        "explanation": "The proposed record does not reconcile with the page.",
        "confidence": "high",
        "suggested_action": "Compare the proposed criteria with the source page.",
        "source": {
            "document_role": "mark_scheme",
            "page": 1,
            "excerpt": "M1: correct method",
        },
    }
    payload.update(overrides)
    return payload


def _provider(findings=None, payload=None, error=None, calls=None):
    calls = calls if calls is not None else []

    def call(packet):
        calls.append(packet)
        if error is not None:
            raise error
        if payload is not None:
            return payload
        return {"findings": copy.deepcopy(findings or [])}

    return call, calls


def _completed(findings):
    result = review.review_scheme(_payload(), provider=_provider(findings)[0])
    return result


class AcceptedFindingTests(unittest.TestCase):

    def test_valid_provider_finding_is_a_nonblocking_proposal(self):
        result = _completed([_finding_payload()])
        self.assertEqual(result["provider_review"]["status"], "completed")
        self.assertEqual(result["provider_review"]["request_count"], 1)
        self.assertEqual(result["provider_review"]["max_requests"], 1)
        self.assertEqual(result["provider_review"]["cache"], "miss")
        self.assertEqual(len(result["provider_review"]["findings"]), 1)
        finding = result["provider_review"]["findings"][0]
        self.assertEqual(finding["origin"], "gemini_proposal")
        self.assertEqual(finding["rule_id"], "C5-PROVIDER-TOTAL_MISMATCH")
        self.assertEqual(finding["state"], "suspected_issue")
        self.assertFalse(finding["blocks_approval"])
        self.assertTrue(finding["acknowledgement_required"])
        self.assertEqual(finding["evidence"]["provider_confidence"], "high")
        self.assertIn("source_reference", finding["evidence"])
        self.assertNotIn(
            finding["finding_id"],
            {item["finding_id"] for item in result["verified"]["c5_findings"]})
        self.assertIn(finding["finding_id"],
                      result["verified"]["required_acknowledgements"])
        self.assertEqual(result["teacher"]["review_status"],
                         "NEEDS_TEACHER_REVIEW")

    def test_every_allowed_category_is_accepted_when_targeted_correctly(self):
        cases = {
            "TOTAL_MISMATCH": ("Q1", []),
            "CRITERION_MISSING": ("Q1", []),
            "CRITERION_UNSUPPORTED": ("Q1", ["Q1-c1"]),
            "MARK_TYPE_UNCERTAIN": ("Q1", ["Q1-c1"]),
            "DEPENDENCY_CONFLICT": ("Q1", ["Q1-c1"]),
            "FOLLOW_THROUGH_UNCERTAIN": ("Q1", ["Q1-c1"]),
            "ALTERNATIVE_METHOD_CONFLICT": ("Q1", ["Q1-c1"]),
            "QUESTION_MAPPING_CONFLICT": ("Q1", []),
            "SOURCE_EVIDENCE_MISSING": (None, []),
        }
        for category, (question_id, criterion_ids) in cases.items():
            with self.subTest(category=category):
                result = _completed([_finding_payload(
                    category=category,
                    question_id=question_id,
                    criterion_ids=criterion_ids)])
                self.assertEqual(result["provider_review"]["status"],
                                 "completed")
                self.assertEqual(
                    result["provider_review"]["findings"][0]
                    ["issue_category"], category)

    def test_provider_can_cite_an_extracted_question_paper_page(self):
        payload = _payload()
        payload["source_documents"]["question_paper"] = _document(
            "question_paper", pages=["Question 1 (a) Solve the equation"])
        payload["proposed_scheme"]["questions"][0]["source_ref"] = {
            "paper": 1}
        provider, _ = _provider([_finding_payload(
            category="QUESTION_MAPPING_CONFLICT",
            question_id="Q1",
            source={
                "document_role": "question_paper",
                "page": 1,
                "excerpt": "Question 1 (a) Solve the equation",
            })])
        result = review.review_scheme(payload, provider=provider)
        finding = result["provider_review"]["findings"][0]
        self.assertEqual(finding["evidence"]["source_reference"]
                         ["document_role"], "question_paper")

    def test_provider_findings_do_not_change_extracted_or_verified_data(self):
        baseline = review.review_scheme(_payload())
        result = _completed([_finding_payload(), _finding_payload(
            category="CRITERION_UNSUPPORTED",
            criterion_ids=["Q1-c1"],
            explanation="The first criterion may not support its claim.")])
        self.assertEqual(result["extracted"], baseline["extracted"])
        for key in (
                "c2_findings", "c5_findings", "c2_approval_accounting",
                "effective_totals", "reconciliation", "evidence_references",
                "question_mapping", "evidence_coverage", "derived_mark_types",
                "hard_blockers", "source_scheme_sha256"):
            self.assertEqual(result["verified"][key],
                             baseline["verified"][key])
        self.assertEqual(result["teacher"]["review_status"],
                         "NEEDS_TEACHER_REVIEW")
        self.assertFalse(result["teacher"]["approval_valid"])
        self.assertEqual(result["provider_review"]["findings"][0]["origin"],
                         "gemini_proposal")

    def test_provider_finding_can_be_acknowledged_without_becoming_official(self):
        payload = _payload()
        preliminary = review.review_scheme(
            payload, provider=_provider([_finding_payload()])[0])
        provider_id = preliminary["provider_review"]["findings"][0]["finding_id"]
        result = review.review_scheme(
            _payload(decisions=[_decision(
                acknowledged_finding_ids=[provider_id])]),
            provider=_provider([_finding_payload()])[0])
        self.assertEqual(result["teacher"]["review_status"],
                         "TEACHER_APPROVED")
        self.assertTrue(result["teacher"]["approval_valid"])
        finding = result["provider_review"]["findings"][0]
        self.assertEqual(finding["origin"], "gemini_proposal")
        self.assertFalse(finding["blocks_approval"])
        self.assertEqual(result["extracted"]["origin"],
                         "proposed_model_extraction")

    def test_provider_requests_are_bounded_to_one(self):
        provider, calls = _provider([_finding_payload()])
        result = review.review_scheme(_payload(), provider=provider)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["provider_review"]["request_count"], 1)
        self.assertLessEqual(result["provider_review"]["request_count"],
                             review.MAX_PROVIDER_REQUESTS)

    def test_zero_findings_keeps_checked_state(self):
        result = _completed([])
        self.assertEqual(result["provider_review"]["status"], "completed")
        self.assertEqual(result["provider_review"]["findings"], [])
        self.assertEqual(result["teacher"]["review_status"], "CHECKED")
        self.assertEqual(result["verified"]["required_acknowledgements"], [])


class RejectedPayloadTests(unittest.TestCase):

    def _status(self, payload):
        result = review.review_scheme(
            _payload(), provider=_provider(payload=payload)[0])
        return result["provider_review"]["status"]

    def test_invalid_json_string(self):
        result = review.review_scheme(
            _payload(), provider=_provider(payload="{not json")[0])
        self.assertEqual(result["provider_review"]["status"], "invalid_json")
        self.assertEqual(result["provider_review"]["request_count"], 1)
        self.assertEqual(result["provider_review"]["findings"], [])
        unavailable = _finding(result, "C5-PROVIDER-REVIEW-UNAVAILABLE")
        self.assertEqual(unavailable["evidence"]["provider_status"],
                         "invalid_json")
        self.assertEqual(unavailable["state"], "evidence_insufficient")
        self.assertFalse(unavailable["blocks_approval"])
        self.assertTrue(unavailable["acknowledgement_required"])
        self.assertEqual(result["verified"]["c2_findings"], [])

    def test_json_string_is_accepted(self):
        encoded = (
            '{"findings": [{"category": "TOTAL_MISMATCH", '
            '"question_id": "Q1", "criterion_ids": [], '
            '"explanation": "bounded", "confidence": "low", '
            '"suggested_action": "review"}]}')
        result = review.review_scheme(
            _payload(), provider=_provider(payload=encoded)[0])
        self.assertEqual(result["provider_review"]["status"], "completed")
        self.assertEqual(result["provider_review"]["findings"][0]
                         ["severity"], "low")

    def test_schema_invalid_shapes(self):
        payloads = [
            [],
            "not a dict",
            42,
            {},
            {"findings": "no"},
            {"findings": [], "extra": True},
            {"findings": [42]},
            {"findings": [{}]},
            {"findings": [{"category": "TOTAL_MISMATCH"}]},
            {"findings": [_finding_payload()] * (review.MAX_PROVIDER_FINDINGS + 1)},
        ]
        for payload in payloads:
            with self.subTest(payload=str(payload)[:40]):
                result = review.review_scheme(
                    _payload(), provider=_provider(payload=payload)[0])
                self.assertIn(result["provider_review"]["status"],
                              ("schema_invalid", "invalid_json"))
                self.assertEqual(
                    result["provider_review"]["findings"], [])

    def test_finding_validation_rejections(self):
        invalid = [
            _finding_payload(category="UNKNOWN_CATEGORY"),
            _finding_payload(category="MARK_TYPE_UNCERTAIN",
                             question_id="Q99"),
            _finding_payload(category="MARK_TYPE_UNCERTAIN",
                             criterion_ids=["Q1-ghost"]),
            _finding_payload(category="MARK_TYPE_UNCERTAIN",
                             criterion_ids=["Q1-c1", "Q1-c1"]),
            _finding_payload(category="MARK_TYPE_UNCERTAIN",
                             question_id="Q2", criterion_ids=["Q1-c1"]),
            _finding_payload(category="CRITERION_MISSING",
                             criterion_ids=["Q1-c1"]),
            _finding_payload(category="CRITERION_UNSUPPORTED",
                             criterion_ids=[]),
            _finding_payload(category="MARK_TYPE_UNCERTAIN",
                             question_id=None, criterion_ids=["Q1-c1"]),
            _finding_payload(explanation=""),
            _finding_payload(explanation="x" * (review.MAX_EXPLANATION_CHARS + 1)),
            _finding_payload(confidence="certain"),
            _finding_payload(suggested_action=""),
            _finding_payload(suggested_action="x" * (review.MAX_ACTION_CHARS + 1)),
            _finding_payload(source={"document_role": "unknown", "page": 1}),
            _finding_payload(source={"document_role": "mark_scheme", "page": 0}),
            _finding_payload(source={"document_role": "mark_scheme", "page": 2}),
            _finding_payload(source={
                "document_role": "mark_scheme", "page": 1,
                "excerpt": "fabricated excerpt"}),
            _finding_payload(source={
                "document_role": "mark_scheme", "page": 1,
                "excerpt": "x" * (review.MAX_EXCERPT_CHARS + 1)}),
        ]
        for index, finding in enumerate(invalid):
            with self.subTest(index=index):
                result = review.review_scheme(
                    _payload(), provider=_provider([finding])[0])
                self.assertEqual(result["provider_review"]["findings"], [])
                self.assertEqual(len(
                    result["provider_review"]["rejected_findings"]), 1)
                self.assertEqual(result["provider_review"]["status"],
                                 "schema_invalid")

    def test_unexpected_and_missing_finding_fields_are_rejected(self):
        extra = _finding_payload()
        extra["unexpected"] = True
        missing = _finding_payload()
        del missing["suggested_action"]
        for finding in (extra, missing):
            result = review.review_scheme(
                _payload(), provider=_provider([finding])[0])
            self.assertEqual(result["provider_review"]["findings"], [])
            self.assertEqual(result["provider_review"]["status"],
                             "schema_invalid")

    def test_mixed_findings_keep_only_valid_ones(self):
        valid = _finding_payload()
        invalid = _finding_payload(category="MARK_TYPE_UNCERTAIN",
                                   criterion_ids=["Q1-ghost"])
        result = review.review_scheme(
            _payload(), provider=_provider([valid, invalid])[0])
        self.assertEqual(result["provider_review"]["status"],
                         "completed_with_rejections")
        self.assertEqual(len(result["provider_review"]["findings"]), 1)
        self.assertEqual(len(
            result["provider_review"]["rejected_findings"]), 1)
        self.assertEqual(result["provider_review"]["error_category"], None)

    def test_hallucinated_criterion_is_not_attached_to_a_finding(self):
        result = review.review_scheme(
            _payload(),
            provider=_provider([_finding_payload(
                category="CRITERION_UNSUPPORTED",
                criterion_ids=["Q1-ghost"])])[0])
        self.assertEqual(result["provider_review"]["findings"], [])
        self.assertEqual(result["provider_review"]["status"], "schema_invalid")


class ProviderFailureTests(unittest.TestCase):

    def _failure(self, error):
        return review.review_scheme(
            _payload(), provider=_provider(error=error)[0])

    def test_timeout(self):
        for error in (TimeoutError(), type("TimeoutError", (Exception,), {})()):
            result = self._failure(error)
            self.assertEqual(result["provider_review"]["status"], "timeout")
            self.assertEqual(result["provider_review"]["request_count"], 1)
            self.assertEqual(result["provider_review"]["findings"], [])
            self.assertEqual(result["provider_review"]["error_category"],
                             "timeout")

    def test_quota_limited(self):
        class ResourceExhaustedError(Exception):
            status_code = 429

        result = self._failure(ResourceExhaustedError())
        self.assertEqual(result["provider_review"]["status"],
                         "quota_limited")
        self.assertEqual(result["verified"]["c5_findings"][0]
                         ["evidence"]["provider_status"], "quota_limited")

    def test_blocked(self):
        class BlockedError(Exception):
            pass

        result = self._failure(BlockedError())
        self.assertEqual(result["provider_review"]["status"], "blocked")

    def test_generic_failure_is_unavailable(self):
        result = self._failure(RuntimeError("secret provider detail"))
        self.assertEqual(result["provider_review"]["status"], "unavailable")
        self.assertNotIn("secret provider detail",
                         str(result["provider_review"]))

    def test_empty_response(self):
        result = review.review_scheme(
            _payload(), provider=lambda packet: None)
        self.assertEqual(result["provider_review"]["status"],
                         "empty_response")
        result = review.review_scheme(
            _payload(), provider=_provider(payload="")[0])
        self.assertEqual(result["provider_review"]["status"],
                         "empty_response")

    def test_not_callable_provider(self):
        result = review.review_scheme(_payload(), provider="not-callable")
        self.assertEqual(result["provider_review"]["status"], "unavailable")
        self.assertEqual(result["provider_review"]["request_count"], 0)

    def test_anonymous_review_refuses_provider(self):
        provider, calls = _provider([_finding_payload()])
        result = review.review_scheme(_payload(reviewer=None), provider=provider)
        self.assertEqual(result["provider_review"]["status"],
                         "anonymous_refused")
        self.assertEqual(result["provider_review"]["request_count"], 0)
        self.assertEqual(calls, [])
        self.assertEqual(result["teacher"]["review_status"], "UNREVIEWED")
        self.assertIn("C5-PROVIDER-REVIEW-UNAVAILABLE",
                      [finding["rule_id"]
                       for finding in result["verified"]["c5_findings"]])

    def test_missing_mark_scheme_refuses_provider(self):
        provider, calls = _provider([_finding_payload()])
        result = review.review_scheme(_payload(sources={}), provider=provider)
        self.assertEqual(result["provider_review"]["status"],
                         "unavailable_evidence")
        self.assertEqual(result["provider_review"]["request_count"], 0)
        self.assertEqual(calls, [])

    def test_provider_none_is_never_called(self):
        result = review.review_scheme(_payload(), provider=None)
        self.assertEqual(result["provider_review"]["status"], "not_requested")
        self.assertEqual(result["provider_review"]["request_count"], 0)
        self.assertEqual(result["verified"]["required_acknowledgements"], [])

    def test_failure_finding_is_deterministic_and_ack_required(self):
        result = self._failure(TimeoutError())
        unavailable = _finding(result, "C5-PROVIDER-REVIEW-UNAVAILABLE")
        self.assertEqual(unavailable["origin"], "deterministic")
        self.assertFalse(unavailable["blocks_approval"])
        self.assertTrue(unavailable["acknowledgement_required"])
        self.assertIn(unavailable["finding_id"],
                      result["verified"]["required_acknowledgements"])
        self.assertEqual(result["teacher"]["review_status"],
                         "NEEDS_TEACHER_REVIEW")


class CacheTests(unittest.TestCase):

    def test_cache_hit_avoids_a_second_request(self):
        cache = review.ReviewCache()
        provider, calls = _provider([_finding_payload()])
        first = review.review_scheme(_payload(), provider=provider, cache=cache)
        second = review.review_scheme(_payload(), provider=provider, cache=cache)
        self.assertEqual(len(calls), 1)
        self.assertEqual(first["provider_review"]["cache"], "miss")
        self.assertEqual(second["provider_review"]["cache"], "hit")
        self.assertEqual(first["provider_review"]["findings"],
                         second["provider_review"]["findings"])
        self.assertEqual(cache.size("teacher:1"), 1)

    def test_cache_is_owner_scoped(self):
        cache = review.ReviewCache()
        provider, calls = _provider([_finding_payload()])
        review.review_scheme(_payload(), provider=provider, cache=cache)
        other = review.review_scheme(
            _payload(reviewer="teacher:2"), provider=provider, cache=cache)
        self.assertEqual(len(calls), 2)
        self.assertEqual(other["provider_review"]["cache"], "miss")
        self.assertEqual(cache.size("teacher:1"), 1)
        self.assertEqual(cache.size("teacher:2"), 1)

    def test_cache_is_revision_and_scheme_scoped(self):
        cache = review.ReviewCache()
        provider, calls = _provider([_finding_payload()])
        review.review_scheme(_payload(), provider=provider, cache=cache)
        review.review_scheme(
            _payload(assignment={"revision": 2}),
            provider=provider, cache=cache)
        review.review_scheme(
            _payload(assignment={"scheme_sha256": OTHER_SHA}),
            provider=provider, cache=cache)
        self.assertEqual(len(calls), 3)
        self.assertEqual(cache.size("teacher:1"), 3)

    def test_cache_is_packet_scoped(self):
        cache = review.ReviewCache()
        provider, calls = _provider([_finding_payload()])
        review.review_scheme(_payload(), provider=provider, cache=cache)
        claims = _base_claims()
        claims["Q1-c1"]["part"] = "(b)"
        review.review_scheme(
            _payload(claims=claims), provider=provider, cache=cache)
        self.assertEqual(len(calls), 2)

    def test_failed_requests_are_not_cached(self):
        cache = review.ReviewCache()
        provider, calls = _provider(error=TimeoutError())
        review.review_scheme(_payload(), provider=provider, cache=cache)
        review.review_scheme(_payload(), provider=provider, cache=cache)
        self.assertEqual(len(calls), 2)
        self.assertEqual(cache.size(), 0)

    def test_cache_returns_a_defensive_copy(self):
        cache = review.ReviewCache()
        provider, _ = _provider([_finding_payload()])
        first = review.review_scheme(_payload(), provider=provider, cache=cache)
        first["provider_review"]["findings"][0]["explanation"] = "mutated"
        second = review.review_scheme(_payload(), provider=provider, cache=cache)
        self.assertNotEqual(
            second["provider_review"]["findings"][0]["explanation"],
            "mutated")

    def test_cache_owner_clear_and_global_clear(self):
        cache = review.ReviewCache()
        provider, _ = _provider([_finding_payload()])
        review.review_scheme(_payload(), provider=provider, cache=cache)
        review.review_scheme(
            _payload(reviewer="teacher:2"), provider=provider, cache=cache)
        self.assertEqual(cache.size(), 2)
        cache.clear_owner("teacher:1")
        self.assertEqual(cache.size("teacher:1"), 0)
        self.assertEqual(cache.size("teacher:2"), 1)
        cache.clear()
        self.assertEqual(cache.size(), 0)

    def test_cache_key_includes_all_binding_components(self):
        key = review.ReviewCache.key(
            "teacher:1", "assignment-1", 1, SHA, "digest")
        self.assertEqual(key, (
            "teacher:1", "assignment-1", 1, SHA, "digest"))
        self.assertEqual(
            review.ReviewCache.key(None, "a", 1, None, "d"),
            ("", "a", 1, "", "d"))


class DeterminismTests(unittest.TestCase):

    def test_provider_findings_are_sorted_and_repeatable(self):
        findings = [
            _finding_payload(category="TOTAL_MISMATCH"),
            _finding_payload(category="CRITERION_UNSUPPORTED",
                             criterion_ids=["Q1-c1"]),
        ]
        first = review.review_scheme(
            _payload(), provider=_provider(findings)[0])
        second = review.review_scheme(
            _payload(), provider=_provider(list(reversed(findings)))[0])
        self.assertEqual(first["provider_review"]["findings"],
                         second["provider_review"]["findings"])
        ids = [finding["finding_id"]
               for finding in first["provider_review"]["findings"]]
        self.assertEqual(ids, sorted(ids))

    def test_provider_packet_is_deterministic_and_bounded(self):
        provider, calls = _provider([])
        review.review_scheme(_payload(), provider=provider)
        review.review_scheme(_payload(), provider=provider)
        self.assertEqual(calls[0], calls[1])
        self.assertLessEqual(
            len(json.dumps(calls[0], sort_keys=True)),
            review.MAX_PACKET_CHARS)

    def test_provider_payload_cannot_mutate_the_packet(self):
        captured = []

        def provider(packet):
            captured.append(packet)
            packet["questions"][0]["criteria"][0]["weight"] = 99
            return {"findings": []}

        result = review.review_scheme(_payload(), provider=provider)
        self.assertEqual(result["extracted"]["proposed_scheme"]
                         ["questions"][0]["criteria"][0]["weight"], 1)
        self.assertEqual(result["verified"]["effective_totals"]["Q1"]
                         ["effective_total"], 3)


if __name__ == "__main__":
    unittest.main()
