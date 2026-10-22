"""Offline regression tests for Phase C Stage 5 scheme review.

Investigation-only. No network, no provider calls, no student data and no
production imports. Every behavioural test asserts exact C5 rule ids (or an
exact result state); a clean fixture must produce zero findings.

The official mark scheme remains authoritative throughout: a finding here is
a statement about the proposed *review record*, never a verdict about Pearson
content.
"""
import copy
import inspect
import json
import unittest

from investigation import scheme_review as review
from investigation.pdf_question_map import map_question_pages
from investigation.scheme_model import SchemeModelError, parse_scheme
from investigation.verification_engine import verify_scheme_document

SHA = "a" * 64
OTHER_SHA = "b" * 64
PROVENANCE = {"author": "synthetic C5 fixture", "verified": False}


def _document(role="mark_scheme", pages=None, sha=SHA, statuses=None,
              document_id=None, extraction_status="extracted"):
    pages = list(pages or [
        "Q1 (a) M1: correct method\nA1: answer 4\n(b) B1: state units"])
    statuses = list(statuses or ["extracted"] * len(pages))
    return {
        "schema": "investigation.pdf_evidence/1",
        "document_id": document_id or (role + "-doc"),
        "document_role": role,
        "sha256": sha,
        "page_count": len(pages),
        "extraction_status": extraction_status,
        "warnings": [],
        "pages": [
            {
                "page": index,
                "text": text,
                "status": status,
                "warnings": [],
                "objects": None,
            }
            for index, (text, status) in enumerate(
                zip(pages, statuses), start=1)
        ],
    }


def _claim(part, mark_type, page, excerpt, marks=1, confidence="high",
           **overrides):
    payload = {
        "part": part,
        "mark_type": mark_type,
        "marks": marks,
        "extraction_confidence": confidence,
        "source": {
            "document_role": "mark_scheme",
            "page": page,
            "excerpt": excerpt,
        },
    }
    payload.update(overrides)
    return payload


def _base_questions():
    return [{
        "id": "Q1",
        "total": 3,
        "criteria": [
            {"id": "Q1-c1", "code": "M1", "weight": 1},
            {"id": "Q1-c2", "code": "A1", "weight": 1,
             "dependencies": ["Q1-c1"]},
            {"id": "Q1-c3", "code": "B1", "weight": 1},
        ],
    }]


def _base_claims():
    return {
        "Q1-c1": _claim("(a)", "M", 1, "M1: correct method"),
        "Q1-c2": _claim("(a)", "A", 1, "A1: answer 4"),
        "Q1-c3": _claim("(b)", "B", 1, "B1: state units"),
    }


def _payload(questions=None, claims=None, sources=None, reviewer="teacher:1",
             decisions=None, mapping=None, assignment=None,
             specification="synthetic 4MA1 fixture"):
    assignment_record = {
        "assignment_id": "assignment-1",
        "paper_id": "4MA1-1H",
        "session": "synthetic",
        "revision": 1,
        "scheme_sha256": SHA,
    }
    if assignment:
        assignment_record.update(assignment)
    return {
        "schema": "investigation.scheme_review/1",
        "specification": specification,
        "assignment": assignment_record,
        "reviewer_id": reviewer,
        "proposed_scheme": {
            "provenance": dict(PROVENANCE),
            "questions": copy.deepcopy(
                _base_questions() if questions is None else questions),
        },
        "criterion_claims": copy.deepcopy(
            _base_claims() if claims is None else claims),
        "source_documents": (
            {"mark_scheme": _document()} if sources is None
            else copy.deepcopy(sources)),
        "question_mapping": copy.deepcopy(mapping),
        "teacher_decisions": copy.deepcopy(decisions or []),
    }


def _decision(**overrides):
    payload = {
        "decision_id": "decision-1",
        "owner_id": "teacher:1",
        "decision": "approve",
        "target": {"kind": "scheme", "id": None},
        "assignment_id": "assignment-1",
        "revision": 1,
        "scheme_sha256": SHA,
        "acknowledged_finding_ids": [],
        "reason": "teacher checked the proposal",
    }
    payload.update(overrides)
    return payload


def _c5_ids(result):
    return sorted(finding["rule_id"]
                  for finding in result["verified"]["c5_findings"])


def _c2_ids(result):
    return sorted(finding["rule_id"]
                  for finding in result["verified"]["c2_findings"])


def _finding(result, rule_id):
    matching = [finding for finding in result["verified"]["c5_findings"]
                if finding["rule_id"] == rule_id]
    if len(matching) != 1:
        raise AssertionError(
            f"expected exactly one {rule_id}, got {matching}")
    return matching[0]


def _provider_recorder():
    calls = []

    def provider(packet):
        calls.append(packet)
        return {"findings": []}

    return provider, calls


class ContractTests(unittest.TestCase):

    def test_clean_review_is_checked_without_findings(self):
        result = review.review_scheme(_payload())
        self.assertEqual(result["schema"], review.RESULT_SCHEMA)
        self.assertEqual(result["teacher"]["review_status"], "CHECKED")
        self.assertFalse(result["teacher"]["approval_valid"])
        self.assertEqual(result["verified"]["c2_findings"], [])
        self.assertEqual(result["verified"]["c5_findings"], [])
        self.assertEqual(result["verified"]["hard_blockers"], [])
        self.assertEqual(result["verified"]["required_acknowledgements"], [])
        self.assertEqual(
            result["verified"]["evidence_coverage"]["coverage"], 1.0)
        self.assertEqual(result["provider_review"]["status"], "not_requested")
        self.assertEqual(result["provider_review"]["request_count"], 0)
        self.assertEqual(result["safeguards"], review.SAFEGUARDS)
        self.assertFalse(result["safeguards"]["publishes_grades"])
        self.assertFalse(result["safeguards"]["affects_student_marks"])
        self.assertTrue(
            result["safeguards"]["official_scheme_authoritative"])
        self.assertTrue(
            result["safeguards"]["provider_findings_are_proposals"])
        self.assertFalse(result["safeguards"]["live_provider_calls"])

    def test_unknown_schema_is_rejected(self):
        payload = _payload()
        payload["schema"] = "investigation.scheme_review/2"
        with self.assertRaises(review.SchemeReviewError):
            review.review_scheme(payload)

    def test_unexpected_top_level_and_assignment_keys_are_rejected(self):
        payload = _payload()
        payload["unexpected"] = True
        with self.assertRaises(review.SchemeReviewError):
            review.review_scheme(payload)
        payload = _payload()
        payload["assignment"]["unexpected"] = True
        with self.assertRaises(review.SchemeReviewError):
            review.review_scheme(payload)

    def test_malformed_scheme_error_never_echoes_prose(self):
        payload = _payload()
        payload["proposed_scheme"] = {
            "questions": [{"id": "Q1", "criteria": "SECRET PROSE"}],
        }
        with self.assertRaises(review.SchemeReviewError) as context:
            review.review_scheme(payload)
        self.assertNotIn("SECRET PROSE", str(context.exception))

    def test_assignment_validation(self):
        for assignment in (
                {"assignment_id": ""},
                {"paper_id": ""},
                {"revision": 0},
                {"revision": True},
                {"scheme_sha256": "A" * 64},
                {"scheme_sha256": "a" * 63}):
            payload = _payload(assignment=assignment)
            with self.assertRaises(review.SchemeReviewError):
                review.review_scheme(payload)

    def test_claim_validation_is_strict(self):
        invalid_claims = [
            {"part": 1},
            {"part": "a" * 41},
            {"mark_type": "X"},
            {"marks": True},
            {"marks": 1.5},
            {"marks": -1},
            {"follow_through": "true"},
            {"alternative_option": ""},
            {"extraction_confidence": "certain"},
            {"source": {"document_role": "unknown", "page": 1}},
            {"source": {"document_role": "mark_scheme", "page": 0}},
            {"source": {"document_role": "mark_scheme", "page": 1,
                        "excerpt": ""}},
            {"source": {"document_role": "mark_scheme", "page": 1,
                        "excerpt": "x" * (review.MAX_EXCERPT_CHARS + 1)}},
            {"unknown_field": True},
        ]
        for claim_overrides in invalid_claims:
            claims = _base_claims()
            claims["Q1-c1"] = {**claims["Q1-c1"], **claim_overrides}
            with self.assertRaises(review.SchemeReviewError):
                review.review_scheme(_payload(claims=claims))

    def test_claim_key_length_is_bounded(self):
        claims = _base_claims()
        claims["x" * 201] = _claim("(a)", "M", 1, "M1: correct method")
        with self.assertRaises(review.SchemeReviewError):
            review.review_scheme(_payload(claims=claims))

    def test_source_document_validation(self):
        for sources in (
                {"mark_scheme": {"schema": "wrong"}},
                {"question_paper": _document("mark_scheme")},
                {"unknown_role": _document()}):
            with self.assertRaises(review.SchemeReviewError):
                review.review_scheme(_payload(sources=sources))

    def test_question_mapping_validation(self):
        with self.assertRaises(review.SchemeReviewError):
            review.review_scheme(_payload(mapping={"schema": "wrong"}))

    def test_decision_validation(self):
        invalid = [
            _decision(decision="maybe"),
            _decision(target={"kind": "scheme", "id": "Q1"}),
            _decision(target={"kind": "criterion", "id": ""}),
            _decision(acknowledged_finding_ids=["a", "a"]),
            _decision(owner_id=""),
            _decision(revision=0),
            _decision(scheme_sha256="not-a-digest"),
            _decision(reason=1),
            {**_decision(), "unexpected": True},
        ]
        for decision in invalid:
            with self.assertRaises(review.SchemeReviewError):
                review.review_scheme(_payload(decisions=[decision]))

    def test_duplicate_decision_ids_are_rejected(self):
        with self.assertRaises(review.SchemeReviewError):
            review.review_scheme(_payload(decisions=[
                _decision(), _decision()]))

    def test_oversized_decision_and_claim_collections_are_rejected(self):
        too_many_decisions = [
            _decision(decision_id=f"d-{index}")
            for index in range(review.MAX_DECISIONS + 1)]
        with self.assertRaises(review.SchemeReviewError):
            review.review_scheme(_payload(decisions=too_many_decisions))
        claims = _base_claims()
        for index in range(review.MAX_CLAIMS + 1):
            claims[f"extra-{index}"] = _claim("(a)", "M", 1, "M1: correct method")
        with self.assertRaises(review.SchemeReviewError):
            review.review_scheme(_payload(claims=claims))

    def test_oversized_question_count_is_rejected(self):
        questions = [
            {"id": f"Q{index}", "criteria": []}
            for index in range(review.MAX_QUESTIONS + 1)]
        with self.assertRaises(review.SchemeReviewError):
            review.review_scheme(_payload(questions=questions))


class QuestionAndClaimTests(unittest.TestCase):

    def test_empty_scheme_is_blocking(self):
        result = review.review_scheme(_payload(questions=[], claims={}))
        self.assertEqual(_c5_ids(result), ["C5-SCHEME-EMPTY"])
        self.assertEqual(result["teacher"]["review_status"],
                         "NEEDS_TEACHER_REVIEW")
        self.assertIn(
            "C5::C5-SCHEME-EMPTY::-::",
            result["verified"]["hard_blockers"])

    def test_invalid_question_identifier_is_blocking(self):
        questions = _base_questions()
        questions[0]["id"] = "Q1(a)"
        result = review.review_scheme(_payload(questions=questions))
        finding = _finding(result, "C5-QUESTION-ID-INVALID")
        self.assertEqual(finding["question_id"], "Q1(a)")
        self.assertTrue(finding["blocks_approval"])
        self.assertEqual(finding["state"], "verified_structural_inconsistency")

    def test_question_without_criteria_is_insufficient(self):
        questions = [{"id": "Q1", "total": 3, "criteria": []}]
        result = review.review_scheme(_payload(questions=questions, claims={}))
        finding = _finding(result, "C5-QUESTION-NO-CRITERIA")
        self.assertEqual(finding["state"], "evidence_insufficient")
        self.assertTrue(finding["blocks_approval"])

    def test_claim_for_unknown_criterion_is_not_attached(self):
        claims = _base_claims()
        claims["Q1-ghost"] = _claim("(a)", "M", 1, "M1: correct method")
        result = review.review_scheme(_payload(claims=claims))
        finding = _finding(result, "C5-CLAIM-UNKNOWN-CRITERION")
        self.assertEqual(finding["criterion_ids"], ["Q1-ghost"])
        self.assertTrue(finding["blocks_approval"])

    def test_missing_claim_is_never_inferred(self):
        claims = _base_claims()
        del claims["Q1-c2"]
        result = review.review_scheme(_payload(claims=claims))
        finding = _finding(result, "C5-EXTRACTION-CLAIM-MISSING")
        self.assertEqual(finding["criterion_ids"], ["Q1-c2"])
        self.assertEqual(finding["state"], "evidence_insufficient")

    def test_part_notation_checks(self):
        claims = _base_claims()
        claims["Q1-c1"]["part"] = "part one"
        result = review.review_scheme(_payload(claims=claims))
        self.assertEqual(_finding(result, "C5-PART-ID-INVALID")["state"],
                         "verified_structural_inconsistency")
        claims = _base_claims()
        claims["Q1-c1"]["part"] = "(A)"
        result = review.review_scheme(_payload(claims=claims))
        finding = _finding(result, "C5-PART-ID-NONCANONICAL")
        self.assertFalse(finding["blocks_approval"])
        self.assertTrue(finding["acknowledgement_required"])

    def test_mark_type_declaration_checks(self):
        claims = _base_claims()
        claims["Q1-c1"].pop("mark_type")
        result = review.review_scheme(_payload(claims=claims))
        self.assertEqual(_finding(result, "C5-MARK-TYPE-UNDECLARED")["state"],
                         "evidence_insufficient")
        claims = _base_claims()
        claims["Q1-c1"]["mark_type"] = "UNKNOWN"
        result = review.review_scheme(_payload(claims=claims))
        self.assertEqual(_finding(result, "C5-MARK-TYPE-UNCERTAIN")["state"],
                         "suspected_issue")
        claims = _base_claims()
        claims["Q1-c1"]["mark_type"] = "A"
        result = review.review_scheme(_payload(claims=claims))
        finding = _finding(result, "C5-MARK-TYPE-CONFLICT")
        self.assertEqual(finding["evidence"]["declared"], "A")
        self.assertEqual(finding["evidence"]["derived_from_code"], "M")
        self.assertTrue(finding["blocks_approval"])

    def test_mark_allocation_conflict(self):
        claims = _base_claims()
        claims["Q1-c1"]["marks"] = 2
        result = review.review_scheme(_payload(claims=claims))
        finding = _finding(result, "C5-MARK-ALLOCATION-CONFLICT")
        self.assertEqual(finding["evidence"]["claim_marks"], 2)
        self.assertEqual(finding["evidence"]["criterion_weight"], 1)

    def test_follow_through_conflict(self):
        questions = _base_questions()
        questions[0]["criteria"][0]["follow_through"] = True
        claims = _base_claims()
        claims["Q1-c1"]["follow_through"] = False
        result = review.review_scheme(
            _payload(questions=questions, claims=claims))
        self.assertTrue(any(
            finding["rule_id"] == "C5-FOLLOW-THROUGH-CONFLICT"
            for finding in result["verified"]["c5_findings"]))

    def test_orphan_alternative_option(self):
        claims = _base_claims()
        claims["Q1-c1"]["alternative_option"] = "method-one"
        result = review.review_scheme(_payload(claims=claims))
        finding = _finding(result, "C5-ALTERNATIVE-OPTION-ORPHAN")
        self.assertEqual(finding["evidence"]["alternative_option"],
                         "method-one")
        self.assertFalse(finding["blocks_approval"])

    def test_extraction_confidence_warnings(self):
        claims = _base_claims()
        claims["Q1-c1"]["extraction_confidence"] = "unknown"
        claims["Q1-c2"]["extraction_confidence"] = "low"
        result = review.review_scheme(_payload(claims=claims))
        self.assertEqual(
            _finding(result, "C5-EXTRACTION-CONFIDENCE-UNKNOWN")["state"],
            "evidence_insufficient")
        self.assertEqual(
            _finding(result, "C5-LOW-EXTRACTION-CONFIDENCE")["state"],
            "suspected_issue")


class SourceEvidenceTests(unittest.TestCase):

    def test_missing_page_reference_blocks_approval(self):
        claims = _base_claims()
        claims["Q1-c1"].pop("source")
        result = review.review_scheme(_payload(claims=claims))
        finding = _finding(result, "C5-SOURCE-PAGE-MISSING")
        self.assertTrue(finding["blocks_approval"])
        self.assertEqual(finding["state"], "evidence_insufficient")

    def test_question_paper_reference_cannot_support_a_criterion(self):
        claims = _base_claims()
        claims["Q1-c1"]["source"]["document_role"] = "question_paper"
        result = review.review_scheme(_payload(claims=claims))
        self.assertEqual(_finding(result, "C5-SOURCE-ROLE-INVALID")["state"],
                         "verified_structural_inconsistency")

    def test_page_out_of_range_is_structural(self):
        claims = _base_claims()
        claims["Q1-c1"]["source"]["page"] = 2
        result = review.review_scheme(_payload(claims=claims))
        finding = _finding(result, "C5-SOURCE-PAGE-OUT-OF-RANGE")
        self.assertEqual(finding["evidence"]["page_count"], 1)

    def test_empty_text_layer_is_a_limitation_not_a_verdict(self):
        claims = _base_claims()
        claims["Q1-c1"]["source"]["excerpt"] = "not extracted"
        sources = {"mark_scheme": _document(
            pages=[""], statuses=["empty_text_layer"],
            extraction_status="partial_extraction")}
        result = review.review_scheme(_payload(claims=claims, sources=sources))
        findings = [finding for finding in result["verified"]["c5_findings"]
                    if finding["rule_id"] == "C5-SOURCE-EVIDENCE-UNAVAILABLE"]
        self.assertEqual(len(findings), 3)
        for finding in findings:
            self.assertEqual(finding["state"], "evidence_insufficient")
            self.assertTrue(finding["blocks_approval"])
            self.assertNotIn("official document is wrong",
                             finding["explanation"])

    def test_missing_excerpt_is_not_evidence(self):
        claims = _base_claims()
        claims["Q1-c1"]["source"].pop("excerpt")
        result = review.review_scheme(_payload(claims=claims))
        finding = _finding(result, "C5-SOURCE-EVIDENCE-MISSING")
        self.assertTrue(finding["blocks_approval"])

    def test_fabricated_excerpt_is_rejected(self):
        claims = _base_claims()
        claims["Q1-c1"]["source"]["excerpt"] = "M1: invented method"
        result = review.review_scheme(_payload(claims=claims))
        finding = _finding(result, "C5-SOURCE-EVIDENCE-MISMATCH")
        self.assertEqual(finding["state"], "verified_structural_inconsistency")
        self.assertIn("not a claim", finding["explanation"])
        self.assertNotIn("The official document is wrong",
                         finding["explanation"])

    def test_missing_mark_scheme_document(self):
        result = review.review_scheme(_payload(sources={}))
        self.assertEqual(_c5_ids(result), ["C5-SOURCE-DOCUMENT-MISSING"])
        self.assertIn(
            "C5::C5-SOURCE-DOCUMENT-MISSING::-::",
            result["verified"]["hard_blockers"])

    def test_mapping_for_a_different_document_is_rejected(self):
        other = _document(sha=OTHER_SHA, document_id="other-doc")
        mapping = map_question_pages(other, ["Q1"])
        result = review.review_scheme(_payload(mapping=mapping))
        self.assertIn("C5-MAPPING-DOCUMENT-MISMATCH", _c5_ids(result))
        self.assertIn("C5-QUESTION-MAPPING-MISSING", _c5_ids(result))

    def test_unresolved_mapping_is_not_guessed(self):
        sources = {"mark_scheme": _document(
            pages=["M1: correct method\nA1: answer 4\nB1: state units"])}
        result = review.review_scheme(_payload(sources=sources))
        self.assertIn("C5-QUESTION-MAPPING-UNRESOLVED", _c5_ids(result))
        self.assertNotIn("C5-QUESTION-MAPPING-MISSING", _c5_ids(result))

    def test_ambiguous_mapping_requires_human_review(self):
        sources = {"mark_scheme": _document(pages=[
            "Q1 (a) M1: correct method\nA1: answer 4\n(b) B1: state units",
            "Q1 (a) duplicate heading on another page",
        ])}
        result = review.review_scheme(_payload(sources=sources))
        findings = [finding for finding in result["verified"]["c5_findings"]
                    if finding["rule_id"] == "C5-QUESTION-MAPPING-AMBIGUOUS"]
        self.assertEqual(len(findings), 3)
        for finding in findings:
            self.assertEqual(finding["state"], "human_review_required")
            self.assertTrue(finding["blocks_approval"])
            self.assertEqual(finding["evidence"]["pages"], [1, 2])

    def test_evidence_outside_mapped_pages_is_preserved(self):
        sources = {"mark_scheme": _document(pages=[
            "Q1 (a) M1: correct method\nA1: answer 4\n(b) B1: state units",
            "Q2 (a) M1: other question",
        ])}
        claims = _base_claims()
        claims["Q1-c1"]["source"] = {
            "document_role": "mark_scheme",
            "page": 2,
            "excerpt": "Q2 (a) M1: other question",
        }
        result = review.review_scheme(_payload(claims=claims, sources=sources))
        finding = _finding(result, "C5-EVIDENCE-OUTSIDE-QUESTION-MAPPING")
        self.assertEqual(finding["evidence"]["mapped_pages"], [1])
        self.assertFalse(finding["blocks_approval"])
        self.assertTrue(finding["acknowledgement_required"])

    def test_heuristic_continuation_page_requires_confirmation(self):
        sources = {"mark_scheme": _document(pages=[
            "Q1 (a) M1: first line",
            "continued working for the same question",
            "Q2 (a) B1: second question",
        ])}
        questions = [
            {"id": "Q1", "total": 1, "criteria": [
                {"id": "Q1-c1", "code": "M1", "weight": 1}]},
            {"id": "Q2", "total": 1, "criteria": [
                {"id": "Q2-c1", "code": "B1", "weight": 1}]},
        ]
        claims = {
            "Q1-c1": _claim("(a)", "M", 2, "continued working for the same question"),
            "Q2-c1": _claim("(a)", "B", 3, "Q2 (a) B1: second question"),
        }
        result = review.review_scheme(_payload(
            questions=questions, claims=claims, sources=sources))
        finding = _finding(result, "C5-MULTIPAGE-MAPPING-HEURISTIC")
        self.assertFalse(finding["blocks_approval"])
        self.assertTrue(finding["acknowledgement_required"])
        self.assertEqual(
            result["verified"]["question_mapping"]["matches"][0]["pages"],
            [1, 2])


class TotalsAndAlternativesTests(unittest.TestCase):

    def test_missing_criterion_shortfall_is_not_filled(self):
        questions = [{"id": "Q1", "total": 3, "criteria": [
            {"id": "Q1-c1", "code": "M1", "weight": 1},
            {"id": "Q1-c2", "code": "A1", "weight": 1},
        ]}]
        claims = {
            "Q1-c1": _claim("(a)", "M", 1, "M1: correct method"),
            "Q1-c2": _claim("(a)", "A", 1, "A1: answer 4"),
        }
        result = review.review_scheme(
            _payload(questions=questions, claims=claims))
        finding = _finding(result, "C5-CRITERION-MISSING")
        self.assertEqual(finding["evidence"]["shortfall"], 1)
        self.assertEqual(finding["evidence"]["effective_total"], 2)
        self.assertEqual(result["verified"]["effective_totals"]["Q1"]
                         ["effective_total"], 2)

    def test_total_overcount_is_structural(self):
        questions = [{"id": "Q1", "total": 2, "criteria": [
            {"id": "Q1-c1", "code": "M1", "weight": 1},
            {"id": "Q1-c2", "code": "A1", "weight": 2},
        ]}]
        claims = {
            "Q1-c1": _claim("(a)", "M", 1, "M1: correct method"),
            "Q1-c2": _claim("(a)", "A", 1, "A1: answer 4", marks=2),
        }
        result = review.review_scheme(
            _payload(questions=questions, claims=claims))
        finding = _finding(result, "C5-TOTAL-OVERCOUNT")
        self.assertEqual(finding["evidence"]["excess"], 1)
        self.assertEqual(finding["state"], "verified_structural_inconsistency")

    def test_absent_total_is_left_unreconciled(self):
        questions = [{"id": "Q1", "criteria": [
            {"id": "Q1-c1", "code": "M1", "weight": 1},
        ]}]
        claims = {"Q1-c1": _claim("(a)", "M", 1, "M1: correct method")}
        result = review.review_scheme(
            _payload(questions=questions, claims=claims))
        self.assertIn("SCHEME-TOTAL-ABSENT", _c2_ids(result))
        self.assertNotIn("C5-CRITERION-MISSING", _c5_ids(result))
        self.assertNotIn("C5-TOTAL-OVERCOUNT", _c5_ids(result))

    def _alternative_payload(self, weights=(2, 2), total=3, option_two="two"):
        questions = [{"id": "Q1", "total": total, "criteria": [
            {"id": "Q1-a1", "code": "M1", "weight": weights[0],
             "alternative_group": "method"},
            {"id": "Q1-a2", "code": "M1", "weight": weights[1],
             "alternative_group": "method"},
            {"id": "Q1-a3", "code": "A1", "weight": 1},
        ]}]
        claims = {
            "Q1-a1": _claim(
                "(a)", "M", 1, "M1: method one", marks=weights[0],
                alternative_option="one"),
            "Q1-a2": _claim(
                "(a)", "M", 1, "M1: method two", marks=weights[1],
                alternative_option=option_two),
            "Q1-a3": _claim("(a)", "A", 1, "A1: answer"),
        }
        sources = {"mark_scheme": _document(pages=[
            "Q1 (a) M1: method one\nM1: method two\nA1: answer"])}
        return _payload(questions=questions, claims=claims, sources=sources)

    def test_alternatives_are_counted_once_not_summed(self):
        result = review.review_scheme(self._alternative_payload())
        self.assertNotIn("C5-CRITERION-MISSING", _c5_ids(result))
        self.assertNotIn("C5-TOTAL-OVERCOUNT", _c5_ids(result))
        totals = result["verified"]["effective_totals"]["Q1"]
        self.assertEqual(totals["raw_criterion_sum"], 5)
        self.assertEqual(totals["effective_total"], 3)
        self.assertTrue(totals["alternative_structure_resolved"])
        self.assertEqual(totals["alternative_groups"][0]["contribution"], 2)
        self.assertEqual(len(result["verified"]["reconciliation"]), 1)
        explained = result["verified"]["c2_approval_accounting"][
            "SCHEME-TOTAL-MISMATCH::Q1::"]
        self.assertTrue(explained["explained_by_alternatives"])
        self.assertFalse(explained["blocks_approval"])
        approved = review.review_scheme(_payload(
            questions=self._alternative_payload()["proposed_scheme"]
            ["questions"],
            claims=self._alternative_payload()["criterion_claims"],
            sources=self._alternative_payload()["source_documents"],
            decisions=[_decision()]))
        self.assertEqual(approved["teacher"]["review_status"],
                         "TEACHER_APPROVED")
        self.assertTrue(approved["teacher"]["approval_valid"])

    def test_unresolved_alternative_structure_skips_total_comparison(self):
        result = review.review_scheme(
            self._alternative_payload(option_two=None))
        self.assertIn("C5-ALTERNATIVE-STRUCTURE-UNKNOWN", _c5_ids(result))
        self.assertNotIn("C5-CRITERION-MISSING", _c5_ids(result))
        self.assertNotIn("C5-TOTAL-OVERCOUNT", _c5_ids(result))
        totals = result["verified"]["effective_totals"]["Q1"]
        self.assertFalse(totals["alternative_structure_resolved"])
        self.assertIsNone(totals["effective_total"])

    def test_alternative_branch_maxima_mismatch_requires_confirmation(self):
        result = review.review_scheme(
            self._alternative_payload(weights=(2, 3), total=3))
        finding = _finding(result, "C5-ALTERNATIVE-MAX-MISMATCH")
        self.assertEqual(finding["evidence"]["option_totals"],
                         {"one": 2, "two": 3})
        self.assertTrue(finding["acknowledgement_required"])
        self.assertFalse(finding["blocks_approval"])


class DependencyAndFollowThroughTests(unittest.TestCase):

    def test_cross_question_dependency_requires_confirmation(self):
        questions = [
            {"id": "Q1", "total": 1, "criteria": [
                {"id": "Q1-c1", "code": "M1", "weight": 1,
                 "dependencies": ["Q2-c1"]}]},
            {"id": "Q2", "total": 1, "criteria": [
                {"id": "Q2-c1", "code": "A1", "weight": 1}]},
        ]
        claims = {
            "Q1-c1": _claim("(a)", "M", 1, "Q1 (a) M1: first"),
            "Q2-c1": _claim("(a)", "A", 2, "Q2 (a) A1: second"),
        }
        sources = {"mark_scheme": _document(pages=[
            "Q1 (a) M1: first", "Q2 (a) A1: second"])}
        result = review.review_scheme(
            _payload(questions=questions, claims=claims, sources=sources))
        finding = _finding(result, "C5-DEPENDENCY-CROSS-QUESTION")
        self.assertEqual(finding["evidence"]["target_question_id"], "Q2")
        self.assertTrue(finding["acknowledgement_required"])

    def test_forward_dependency_requires_confirmation(self):
        questions = [{"id": "Q1", "total": 2, "criteria": [
            {"id": "Q1-c1", "code": "M1", "weight": 1,
             "dependencies": ["Q1-c2"]},
            {"id": "Q1-c2", "code": "A1", "weight": 1},
        ]}]
        claims = {
            "Q1-c1": _claim("(a)", "M", 1, "M1: correct method"),
            "Q1-c2": _claim("(a)", "A", 1, "A1: answer 4"),
        }
        result = review.review_scheme(
            _payload(questions=questions, claims=claims))
        self.assertIn("C5-DEPENDENCY-FORWARD", _c5_ids(result))

    def test_dependent_method_without_dependency(self):
        questions = [{"id": "Q1", "total": 2, "criteria": [
            {"id": "Q1-c1", "code": "M1", "weight": 1},
            {"id": "Q1-c2", "code": "dM1", "weight": 1},
        ]}]
        claims = {
            "Q1-c1": _claim("(a)", "M", 1, "M1: correct method"),
            "Q1-c2": _claim("(a)", "M", 1, "A1: answer 4"),
        }
        result = review.review_scheme(
            _payload(questions=questions, claims=claims))
        self.assertIn("C5-DEPENDENT-METHOD-NO-DEPENDENCY", _c5_ids(result))

    def test_alternative_branch_dependency_conflict(self):
        questions = [{"id": "Q1", "total": 2, "criteria": [
            {"id": "Q1-a1", "code": "M1", "weight": 1,
             "alternative_group": "method", "dependencies": ["Q1-a2"]},
            {"id": "Q1-a2", "code": "M1", "weight": 1,
             "alternative_group": "method"},
            {"id": "Q1-a3", "code": "A1", "weight": 1},
        ]}]
        claims = {
            "Q1-a1": _claim("(a)", "M", 1, "M1: one",
                            alternative_option="one"),
            "Q1-a2": _claim("(a)", "M", 1, "M1: two",
                            alternative_option="two"),
            "Q1-a3": _claim("(a)", "A", 1, "A1: answer"),
        }
        sources = {"mark_scheme": _document(pages=[
            "Q1 (a) M1: one\nM1: two\nA1: answer"])}
        result = review.review_scheme(
            _payload(questions=questions, claims=claims, sources=sources))
        self.assertIn("C5-ALTERNATIVE-DEPENDENCY-CONFLICT", _c5_ids(result))
        self.assertIn("C5-DEPENDENCY-FORWARD", _c5_ids(result))

    def test_follow_through_without_earlier_criterion(self):
        questions = [{"id": "Q1", "total": 1, "criteria": [
            {"id": "Q1-c1", "code": "A1ft", "weight": 1,
             "follow_through": True},
        ]}]
        claims = {"Q1-c1": _claim("(a)", "A", 1, "A1: answer 4")}
        result = review.review_scheme(
            _payload(questions=questions, claims=claims))
        self.assertIn("C5-FOLLOW-THROUGH-UNSUPPORTED", _c5_ids(result))

    def test_follow_through_source_unspecified(self):
        questions = [{"id": "Q1", "total": 2, "criteria": [
            {"id": "Q1-c1", "code": "M1", "weight": 1},
            {"id": "Q1-c2", "code": "A1ft", "weight": 1,
             "follow_through": True},
        ]}]
        claims = {
            "Q1-c1": _claim("(a)", "M", 1, "M1: correct method"),
            "Q1-c2": _claim("(a)", "A", 1, "A1: answer 4"),
        }
        result = review.review_scheme(
            _payload(questions=questions, claims=claims))
        self.assertIn("C5-FOLLOW-THROUGH-SOURCE-UNSPECIFIED",
                      _c5_ids(result))

    def test_follow_through_dependency_only_on_later_criterion(self):
        questions = [{"id": "Q1", "total": 3, "criteria": [
            {"id": "Q1-c1", "code": "M1", "weight": 1},
            {"id": "Q1-c2", "code": "A1ft", "weight": 1,
             "follow_through": True, "dependencies": ["Q1-c3"]},
            {"id": "Q1-c3", "code": "B1", "weight": 1},
        ]}]
        claims = {
            "Q1-c1": _claim("(a)", "M", 1, "M1: correct method"),
            "Q1-c2": _claim("(a)", "A", 1, "A1: answer 4"),
            "Q1-c3": _claim("(b)", "B", 1, "B1: state units"),
        }
        result = review.review_scheme(
            _payload(questions=questions, claims=claims))
        self.assertIn("C5-FOLLOW-THROUGH-UNSUPPORTED", _c5_ids(result))
        self.assertIn("C5-DEPENDENCY-FORWARD", _c5_ids(result))

    def test_dependency_cycle_is_preserved_from_c2(self):
        questions = [{"id": "Q1", "total": 2, "criteria": [
            {"id": "Q1-c1", "code": "M1", "weight": 1,
             "dependencies": ["Q1-c2"]},
            {"id": "Q1-c2", "code": "A1", "weight": 1,
             "dependencies": ["Q1-c1"]},
        ]}]
        claims = {
            "Q1-c1": _claim("(a)", "M", 1, "M1: correct method"),
            "Q1-c2": _claim("(a)", "A", 1, "A1: answer 4"),
        }
        result = review.review_scheme(
            _payload(questions=questions, claims=claims))
        self.assertIn("SCHEME-DEP-CYCLE", _c2_ids(result))
        cycle = [finding for finding in result["verified"]["c2_findings"]
                 if finding["rule_id"] == "SCHEME-DEP-CYCLE"][0]
        self.assertEqual(cycle["criterion_ids"], ["Q1-c1", "Q1-c2"])
        self.assertIn(cycle["finding_id"],
                      result["verified"]["hard_blockers"])


class ApprovalTests(unittest.TestCase):

    def test_reviewer_without_decisions_is_unreviewed(self):
        result = review.review_scheme(_payload())
        self.assertEqual(result["teacher"]["review_status"], "CHECKED")
        result = review.review_scheme(_payload(reviewer=None))
        self.assertEqual(result["teacher"]["review_status"], "UNREVIEWED")
        self.assertFalse(result["teacher"]["approval_valid"])

    def test_valid_scheme_approval(self):
        result = review.review_scheme(_payload(decisions=[_decision()]))
        self.assertEqual(result["teacher"]["review_status"],
                         "TEACHER_APPROVED")
        self.assertTrue(result["teacher"]["approval_valid"])
        self.assertEqual(result["teacher"]["approval_binding"], {
            "reviewer_id": "teacher:1",
            "assignment_id": "assignment-1",
            "revision": 1,
            "scheme_sha256": SHA,
        })
        self.assertEqual(result["teacher"]["scheme_decisions_applied"], 1)

    def test_hard_blocker_cannot_be_approved(self):
        claims = _base_claims()
        claims["Q1-c1"]["source"]["excerpt"] = "fabricated excerpt"
        result = review.review_scheme(
            _payload(claims=claims, decisions=[_decision()]))
        self.assertEqual(result["teacher"]["review_status"],
                         "NEEDS_TEACHER_REVIEW")
        self.assertFalse(result["teacher"]["approval_valid"])
        self.assertIn("C5-APPROVAL-BLOCKED", _c5_ids(result))
        blocked = _finding(result, "C5-APPROVAL-BLOCKED")
        self.assertEqual(blocked["evidence"]["hard_blocker_count"], 1)

    def test_missing_acknowledgement_invalidates_approval(self):
        claims = _base_claims()
        claims["Q1-c1"]["extraction_confidence"] = "low"
        result = review.review_scheme(
            _payload(claims=claims, decisions=[_decision()]))
        self.assertEqual(result["teacher"]["review_status"],
                         "NEEDS_TEACHER_REVIEW")
        self.assertFalse(result["teacher"]["approval_valid"])
        finding = _finding(result, "C5-APPROVAL-INCOMPLETE")
        self.assertTrue(finding["evidence"]["missing_acknowledgements"])

    def test_acknowledged_warning_allows_approval(self):
        claims = _base_claims()
        claims["Q1-c1"]["extraction_confidence"] = "low"
        preliminary = review.review_scheme(_payload(claims=claims))
        warning = _finding(preliminary, "C5-LOW-EXTRACTION-CONFIDENCE")
        result = review.review_scheme(_payload(
            claims=claims,
            decisions=[_decision(
                acknowledged_finding_ids=[warning["finding_id"]])]))
        self.assertEqual(result["teacher"]["review_status"],
                         "TEACHER_APPROVED")
        self.assertTrue(result["teacher"]["approval_valid"])

    def test_unknown_acknowledgement_is_rejected(self):
        result = review.review_scheme(_payload(decisions=[_decision(
            acknowledged_finding_ids=["C5::does-not-exist::-::"])]))
        self.assertEqual(result["teacher"]["review_status"],
                         "NEEDS_TEACHER_REVIEW")
        self.assertFalse(result["teacher"]["approval_valid"])
        self.assertIn("C5-ACKNOWLEDGEMENT-UNKNOWN", _c5_ids(result))

    def test_scheme_rejection(self):
        result = review.review_scheme(_payload(decisions=[_decision(
            decision="reject")]))
        self.assertEqual(result["teacher"]["review_status"], "REJECTED")
        self.assertFalse(result["teacher"]["approval_valid"])
        self.assertEqual(result["teacher"]["question_status"]["Q1"],
                         "REJECTED")

    def test_criterion_rejection(self):
        result = review.review_scheme(_payload(decisions=[_decision(
            target={"kind": "criterion", "id": "Q1-c2"},
            decision="reject")]))
        self.assertEqual(result["teacher"]["review_status"],
                         "NEEDS_TEACHER_REVIEW")
        self.assertEqual(result["teacher"]["criterion_status"]["Q1-c2"],
                         "REJECTED")

    def test_needs_review_decision(self):
        result = review.review_scheme(_payload(decisions=[_decision(
            decision="needs_review")]))
        self.assertEqual(result["teacher"]["review_status"],
                         "NEEDS_TEACHER_REVIEW")

    def test_criterion_approval_is_scope_limited(self):
        result = review.review_scheme(_payload(decisions=[_decision(
            target={"kind": "criterion", "id": "Q1-c1"})]))
        self.assertEqual(result["teacher"]["criterion_status"]["Q1-c1"],
                         "TEACHER_APPROVED")
        self.assertEqual(result["teacher"]["criterion_status"]["Q1-c2"],
                         "CHECKED")
        self.assertEqual(result["teacher"]["review_status"], "CHECKED")
        self.assertFalse(result["teacher"]["approval_valid"])

    def test_criterion_approval_requires_acknowledgements(self):
        claims = _base_claims()
        claims["Q1-c1"]["extraction_confidence"] = "low"
        result = review.review_scheme(_payload(
            claims=claims,
            decisions=[_decision(
                target={"kind": "criterion", "id": "Q1-c1"})]))
        self.assertEqual(result["teacher"]["criterion_status"]["Q1-c1"],
                         "NEEDS_TEACHER_REVIEW")
        self.assertIn("C5-APPROVAL-INCOMPLETE", _c5_ids(result))

    def test_question_approval_covers_its_criteria(self):
        result = review.review_scheme(_payload(decisions=[_decision(
            target={"kind": "question", "id": "Q1"})]))
        self.assertEqual(result["teacher"]["question_status"]["Q1"],
                         "TEACHER_APPROVED")
        self.assertEqual(result["teacher"]["question_decisions_applied"], 1)

    def test_unknown_decision_target(self):
        result = review.review_scheme(_payload(decisions=[_decision(
            target={"kind": "criterion", "id": "Q1-ghost"})]))
        self.assertIn("C5-DECISION-TARGET-UNKNOWN", _c5_ids(result))
        self.assertEqual(result["teacher"]["review_status"],
                         "NEEDS_TEACHER_REVIEW")

    def test_multiple_scheme_decisions_are_ambiguous(self):
        result = review.review_scheme(_payload(decisions=[
            _decision(), _decision(decision_id="decision-2")]))
        self.assertIn("C5-DECISION-AMBIGUOUS", _c5_ids(result))

    def test_conflicting_scheme_and_criterion_decisions(self):
        result = review.review_scheme(_payload(decisions=[
            _decision(),
            _decision(
                decision_id="decision-2",
                target={"kind": "criterion", "id": "Q1-c1"},
                decision="reject"),
        ]))
        self.assertIn("C5-APPROVAL-CONFLICT", _c5_ids(result))
        self.assertEqual(result["teacher"]["review_status"],
                         "NEEDS_TEACHER_REVIEW")

    def test_conflicting_question_and_criterion_decisions(self):
        result = review.review_scheme(_payload(decisions=[
            _decision(
                decision_id="decision-1",
                target={"kind": "question", "id": "Q1"},
                decision="approve"),
            _decision(
                decision_id="decision-2",
                target={"kind": "criterion", "id": "Q1-c2"},
                decision="reject"),
        ]))
        self.assertIn("C5-APPROVAL-CONFLICT", _c5_ids(result))

    def test_decisions_are_bound_to_owner_assignment_revision_and_digest(self):
        variants = [
            _decision(owner_id="teacher:2"),
            _decision(assignment_id="assignment-2"),
            _decision(revision=2),
            _decision(scheme_sha256=OTHER_SHA),
        ]
        for decision in variants:
            result = review.review_scheme(_payload(decisions=[decision]))
            self.assertIn("C5-APPROVAL-NOT-APPLICABLE", _c5_ids(result))
            self.assertFalse(result["teacher"]["approval_valid"])
            self.assertEqual(result["teacher"]["decisions_applied"], [])
            self.assertEqual(result["teacher"]["decisions_ignored"], 1)

    def test_scheme_revision_mismatch_blocks_approval(self):
        result = review.review_scheme(_payload(
            assignment={"scheme_sha256": OTHER_SHA},
            decisions=[_decision(scheme_sha256=OTHER_SHA)]))
        self.assertIn("C5-SCHEME-REVISION-MISMATCH", _c5_ids(result))
        self.assertEqual(result["teacher"]["review_status"],
                         "NEEDS_TEACHER_REVIEW")
        self.assertFalse(result["teacher"]["approval_valid"])

    def test_stale_decision_cannot_be_mixed_with_a_valid_one(self):
        result = review.review_scheme(_payload(decisions=[
            _decision(decision_id="stale", revision=2),
            _decision(decision_id="current"),
        ]))
        self.assertIn("C5-APPROVAL-NOT-APPLICABLE", _c5_ids(result))
        self.assertFalse(result["teacher"]["approval_valid"])


class SeparationAndHygieneTests(unittest.TestCase):

    def test_c2_findings_are_preserved_exactly(self):
        questions = [{"id": "Q1", "total": 2, "criteria": [
            {"id": "Q1-c1", "code": "M1", "weight": 1},
            {"id": "Q1-c2", "code": "A1", "weight": 2},
        ]}]
        claims = {
            "Q1-c1": _claim("(a)", "M", 1, "M1: correct method"),
            "Q1-c2": _claim("(a)", "A", 1, "A1: answer", marks=2),
        }
        payload = _payload(questions=questions, claims=claims)
        result = review.review_scheme(payload)
        direct = verify_scheme_document(
            parse_scheme(copy.deepcopy(payload["proposed_scheme"])))
        self.assertEqual(result["verified"]["c2_findings"], direct)

    def test_extracted_verified_and_teacher_states_stay_separate(self):
        result = review.review_scheme(_payload(decisions=[_decision()]))
        self.assertEqual(result["extracted"]["origin"],
                         "proposed_model_extraction")
        self.assertNotIn("teacher", result["extracted"])
        self.assertNotIn("teacher", result["verified"])
        self.assertNotIn("provider", result["verified"])
        self.assertEqual(result["teacher"]["review_status"],
                         "TEACHER_APPROVED")
        self.assertNotIn("teacher", result["provider_review"])
        for entry in result["verified"]["derived_mark_types"]:
            self.assertNotIn("origin", entry)
            self.assertIn("derived_mark_type", entry)

    def test_derived_mark_types_do_not_mutate_extracted_claims(self):
        payload = _payload()
        snapshot = copy.deepcopy(payload)
        result = review.review_scheme(payload)
        self.assertEqual(payload, snapshot)
        for claim in result["extracted"]["criterion_claims"].values():
            self.assertNotIn("derived_mark_type", claim)
            self.assertNotIn("dependent_method", claim)
        derived = {
            entry["criterion_id"]: entry["derived_mark_type"]
            for entry in result["verified"]["derived_mark_types"]}
        self.assertEqual(derived, {
            "Q1-c1": "M", "Q1-c2": "A", "Q1-c3": "B"})

    def test_result_is_deterministic_and_json_serializable(self):
        payload = _payload()
        first = review.review_scheme(payload)
        second = review.review_scheme(payload)
        self.assertEqual(first, second)
        encoded = json.dumps(first, sort_keys=True)
        self.assertLessEqual(len(encoded), review.MAX_RESULT_CHARS)
        self.assertEqual(json.loads(encoded), first)

    def test_full_paper_scale_result_is_bounded_but_complete(self):
        questions = []
        claims = {}
        pages = []
        for question_number in range(1, 21):
            criteria = []
            lines = []
            for criterion_number in range(1, 11):
                code = f"M{criterion_number}"
                excerpt = f"{code}: synthetic method {criterion_number}"
                criterion_id = f"Q{question_number}-c{criterion_number}"
                criteria.append({
                    "id": criterion_id, "code": code, "weight": 1})
                claims[criterion_id] = _claim(
                    "(a)", "M", question_number, excerpt)
                lines.append(excerpt)
            questions.append({
                "id": f"Q{question_number}",
                "total": 10,
                "criteria": criteria,
            })
            pages.append(f"Q{question_number} (a) " + "\n".join(lines))
        payload = _payload(
            questions=questions, claims=claims,
            sources={"mark_scheme": _document(pages=pages)})
        result = review.review_scheme(payload)
        self.assertEqual(result["teacher"]["review_status"], "CHECKED")
        self.assertEqual(
            len(result["verified"]["evidence_references"]), 200)
        self.assertLessEqual(
            len(json.dumps(result, sort_keys=True)),
            review.MAX_RESULT_CHARS)

    def test_input_is_not_mutated(self):
        payload = _payload()
        snapshot = copy.deepcopy(payload)
        review.review_scheme(payload)
        self.assertEqual(payload, snapshot)

    def test_findings_are_unique_and_review_required(self):
        claims = _base_claims()
        claims["Q1-c1"]["source"]["excerpt"] = "fabricated"
        result = review.review_scheme(_payload(claims=claims))
        ids = [finding["finding_id"]
               for finding in result["verified"]["c5_findings"]]
        self.assertEqual(len(ids), len(set(ids)))
        for finding in result["verified"]["c5_findings"]:
            self.assertTrue(finding["review_required"])
            self.assertEqual(finding["origin"], "deterministic")
            key = review.finding_key(finding)
            self.assertIsInstance(key, tuple)
            self.assertEqual(key[0], finding["rule_id"])

    def test_no_production_or_network_imports(self):
        source = inspect.getsource(review)
        import_lines = [
            line.strip() for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))]
        forbidden_modules = (
            "app5_1_automated", "marking_requests", "streamlit",
            "google", "genai", "requests", "urllib", "http", "socket",
            "openai", "aiohttp", "httpx")
        for line in import_lines:
            for module in forbidden_modules:
                self.assertNotIn(module, line)
        self.assertIn("This module is investigation-only", review.__doc__)

    def test_provider_packet_excludes_teacher_and_reviewer_data(self):
        provider, calls = _provider_recorder()
        result = review.review_scheme(
            _payload(decisions=[_decision()]), provider=provider)
        self.assertEqual(result["provider_review"]["status"], "completed")
        self.assertEqual(len(calls), 1)
        packet = calls[0]
        encoded = json.dumps(packet, sort_keys=True)
        for forbidden in (
                "teacher_decisions", "reviewer_id", "owner_id",
                "acknowledged_finding_ids", "criterion_status",
                "student_id", "student_name", "awarded_marks", "grade"):
            self.assertNotIn(forbidden, encoded)

        def _all_keys(value):
            if isinstance(value, dict):
                for key, nested in value.items():
                    yield key
                    yield from _all_keys(nested)
            elif isinstance(value, list):
                for nested in value:
                    yield from _all_keys(nested)

        for key in _all_keys(packet):
            self.assertNotIn(key, {
                "teacher_decisions", "reviewer_id", "owner_id",
                "acknowledged_finding_ids", "criterion_status",
                "student_id", "student_name", "awarded_marks", "grade"})
        self.assertEqual(packet["schema"], review.PROVIDER_PACKET_SCHEMA)
        self.assertEqual(packet["source_pages"], {
            1: "Q1 (a) M1: correct method\nA1: answer 4\n"
               "(b) B1: state units"})
        self.assertIn("instructions", packet)
        self.assertLessEqual(
            len(encoded), review.MAX_PACKET_CHARS)


if __name__ == "__main__":
    unittest.main()
