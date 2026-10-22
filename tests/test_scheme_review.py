"""Phase C5 mark-scheme reviewer tests: offline, deterministic, mocked.

Covers the data contract, deterministic validation rules, M/A/B structure
handling (synthetic examples only), follow-through and alternative-method
handling, teacher approval semantics, and the mocked Gemini-assisted review
response handling (no live provider calls anywhere).
"""
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scheme_review as sr


def criterion(criterion_id, code="M1", marks=1, dependencies=(),
              follow_through=None, alternative_group=None, source_page=None,
              source_evidence=None, note=None):
    return {"criterion_id": criterion_id, "code": code, "marks": marks,
            "dependencies": list(dependencies),
            "follow_through": follow_through,
            "alternative_group": alternative_group,
            "source_page": source_page, "source_evidence": source_evidence,
            "note": note}


def rule_ids(findings, prefix=None):
    ids = [finding["rule_id"] for finding in findings]
    if prefix:
        ids = [rule for rule in ids if rule.startswith(prefix)]
    return ids


def assisted_response(text, finish="STOP"):
    """Mocked assisted-response envelope with the production shape."""
    return SimpleNamespace(
        text=text,
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(
            value=finish, name=finish))])


class DataContractTests(unittest.TestCase):
    def clean_review(self, payload):
        document = {"page_count": 5, "pages": [
            {"page": 2, "text": "M1 valid method, A1 correct answer, B1 independent mark",
             "status": "extracted", "warnings": [], "objects": []}]}
        mapping = {"matches": [{"question_id": q, "certainty": "explicit",
                                "references": [], "pages": [2]}
                               for q in ("1", "2")],
                   "unresolved": []}
        return sr.build_review(
            payload, specification="Edexcel International GCSE Mathematics A (4MA1)",
            evidence_document=document, question_mapping=mapping)

    def test_clean_m_a_dependency_scheme_is_checked_with_no_findings(self):
        payload = {"questions": [
            {"question_number": "1", "max_marks": 2, "criteria": [
                criterion("Q1-c1", "M1", 1, source_page=2,
                          source_evidence="M1 valid method"),
                criterion("Q1-c2", "A1", 1, dependencies=["Q1-c1"],
                          source_page=2, source_evidence="A1 correct answer"),
            ]},
            {"question_number": "2", "max_marks": 1, "criteria": [
                criterion("Q2-b1", "B1", 1, source_page=2,
                          source_evidence="B1 independent mark"),
            ]},
        ]}
        record = self.clean_review(payload)
        self.assertEqual(record["verified"]["findings"], [])
        self.assertEqual(record["teacher"]["status"], "CHECKED")
        self.assertEqual(record["specification"],
                         "Edexcel International GCSE Mathematics A (4MA1)")
        extracted = record["extracted"]["questions"]
        self.assertEqual([entry["mark_type"] for entry in
                          extracted[0]["criteria"]], ["M", "A"])
        self.assertEqual(extracted[1]["criteria"][0]["mark_type"], "B")

    def test_three_channels_are_kept_separate(self):
        payload = {"questions": [{"question_number": "1", "max_marks": 1,
                                  "criteria": [criterion("x", "X1", 1)]}]}
        record = sr.build_review(payload)
        self.assertNotIn("assisted", record["verified"])
        self.assertIsNone(record["assisted"])
        keys_verified = set(record["verified"])
        teacher_keys = set(record["teacher"])
        self.assertFalse(keys_verified & teacher_keys)
        self.assertEqual(record["teacher"], {"status": "NEEDS_TEACHER_REVIEW",
                                             "approved_by": None,
                                             "approved_revision": None})

    def test_revision_changes_when_payload_changes(self):
        payload = {"questions": [{"question_number": "1", "max_marks": 2,
                                  "criteria": [criterion("a", "M1", 1)]}]}
        other = json.loads(json.dumps(payload))
        other["questions"][0]["max_marks"] = 3
        self.assertNotEqual(sr.build_review(payload)["scheme_revision"],
                            sr.build_review(other)["scheme_revision"])
        self.assertEqual(sr.build_review(payload)["scheme_revision"],
                         sr.build_review(payload)["scheme_revision"])

    def test_unknown_information_is_represented_not_guessed(self):
        payload = {"questions": [{"question_number": "1", "max_marks": "3.5",
                                  "criteria": [criterion("a", None, "2x")]}]}
        record = sr.build_review(payload)
        question = record["extracted"]["questions"][0]
        self.assertIsNone(question["max_marks"])
        self.assertIsNone(question["criteria"][0]["marks"])
        rules = rule_ids(record["verified"]["findings"])
        self.assertIn("C5-MARKS-INVALID", rules)
        self.assertEqual(question["criteria"][0]["mark_type"], "UNKNOWN")


class DeterministicValidationTests(unittest.TestCase):
    def test_non_integer_and_negative_marks_are_flagged(self):
        payload = {"questions": [
            {"question_number": "1", "max_marks": -2, "criteria": []}]}
        findings = sr.build_review(payload)["verified"]["findings"]
        rules = rule_ids(findings)
        self.assertIn("C5-MARKS-NEGATIVE", rules)
        payload = {"questions": [{"question_number": "2", "max_marks": 1,
                                  "criteria": [criterion("a", "M1", -1)]}]}
        findings = sr.build_review(payload)["verified"]["findings"]
        self.assertIn("C5-MARKS-NEGATIVE", rule_ids(findings))

    def test_invalid_question_identifiers_are_flagged(self):
        payload = {"questions": [
            {"question_number": "1a!", "max_marks": 1, "criteria": []},
            {"question_number": None, "max_marks": 1, "criteria": []},
            {"question_number": "3", "max_marks": 1, "criteria": []},
        ]}
        findings = sr.build_review(payload)["verified"]["findings"]
        rules = rule_ids(findings)
        self.assertIn("C5-QID-INVALID", rules)
        self.assertIn("C5-QID-INVALID", rules)

    def test_duplicate_question_numbers_are_flagged(self):
        payload = {"questions": [
            {"question_number": "4", "max_marks": 1, "criteria": [
                criterion("a", "M1", 1)]},
            {"question_number": "4", "max_marks": 1, "criteria": [
                criterion("b", "B1", 1)]},
        ]}
        findings = sr.build_review(payload)["verified"]["findings"]
        self.assertIn("C5-QID-DUPLICATE", rule_ids(findings))
        self.assertIn("SCHEME-DUP-QUESTIONS", rule_ids(findings))

    def test_duplicate_criterion_ids_are_flagged(self):
        payload = {"questions": [{"question_number": "5", "max_marks": 2,
                                  "criteria": [criterion("a", "M1", 1),
                                               criterion("a", "A1", 1)]}]}
        findings = sr.build_review(payload)["verified"]["findings"]
        self.assertIn("C5-DUP-CRITERIA", rule_ids(findings))
        self.assertIn("SCHEME-DUP-CRITERIA", rule_ids(findings))

    def test_missing_criteria_for_declared_maximum(self):
        payload = {"questions": [{"question_number": "7", "max_marks": 3,
                                  "criteria": []}]}
        findings = sr.build_review(payload)["verified"]["findings"]
        missing = [f for f in findings
                   if f["rule_id"] == "C5-CRITERIA-MISSING"]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["category"], "CRITERION_MISSING")

    def test_uncovered_question_numbers_are_reported(self):
        payload = {"questions": [
            {"question_number": "1", "max_marks": 1, "criteria": [
                criterion("a", "M1", 1)]},
        ]}
        record = sr.build_review(payload, question_numbers=["1", "2"])
        rules = rule_ids(record["verified"]["findings"])
        self.assertIn("C5-QUESTION-NOT-INTERPRETED", rules)

    def test_undefined_dependency_conflict(self):
        payload = {"questions": [{"question_number": "1", "max_marks": 2,
                                  "criteria": [
                                      criterion("a", "M1", 1),
                                      criterion("b", "A1", 1,
                                                dependencies=["ghost"])]}]}
        findings = sr.build_review(payload)["verified"]["findings"]
        rules = rule_ids(findings)
        self.assertIn("C5-DEP-UNDEFINED", rules)
        self.assertEqual(
            [f["category"] for f in findings if f["rule_id"] == "C5-DEP-UNDEFINED"],
            ["DEPENDENCY_CONFLICT"])

    def test_cyclic_dependency_conflict(self):
        payload = {"questions": [{"question_number": "1", "max_marks": 2,
                                  "criteria": [
                                      criterion("a", "M1", 1,
                                                dependencies=["b"]),
                                      criterion("b", "A1", 1,
                                                dependencies=["a"])]}]}
        findings = sr.build_review(payload)["verified"]["findings"]
        self.assertIn("C5-DEP-CYCLE", rule_ids(findings))

    def test_unsupported_mark_type_is_flagged_as_unknown(self):
        payload = {"questions": [{"question_number": "1", "max_marks": 1,
                                  "criteria": [criterion("a", "X1", 1)]}]}
        record = sr.build_review(payload)
        rules = rule_ids(record["verified"]["findings"])
        self.assertIn("C5-MARKTYPE-UNSUPPORTED", rules)
        self.assertEqual(
            record["extracted"]["questions"][0]["criteria"][0]["mark_type"],
            "UNKNOWN")

    def test_two_alternative_methods_never_sum_into_the_maximum(self):
        payload = {"questions": [{"question_number": "3", "max_marks": 2,
                                  "criteria": [
                                      criterion("m1", "M1", 2,
                                                alternative_group="alt",
                                                note="Method A"),
                                      criterion("m2", "M1", 2,
                                                alternative_group="alt",
                                                note="Method B")]}]}
        record = sr.build_review(payload)
        alt = [f for f in record["verified"]["findings"]
               if f["category"] == "ALTERNATIVE_METHOD_CONFLICT"]
        self.assertTrue(alt)
        states = [f["state"] for f in alt]
        self.assertIn("human_review_required", states)
        self.assertEqual(record["teacher"]["status"], "NEEDS_TEACHER_REVIEW")

    def test_follow_through_declaration_requires_evidence(self):
        payload = {"questions": [{"question_number": "8", "max_marks": 1,
                                  "criteria": [
                                      criterion("a", "A1ft", 1,
                                                follow_through=None,
                                                note=None)]}]}
        payload["questions"][0]["criteria"][0]["follow_through"] = True
        record = sr.build_review(payload)
        self.assertIn("C5-FT-UNSUPPORTED",
                      rule_ids(record["verified"]["findings"]))
        # ft suffix contradicted by an explicit False flag
        payload = {"questions": [{"question_number": "9", "max_marks": 1,
                                  "criteria": [
                                      criterion("a", "A1ft", 1,
                                                follow_through=False)]}]}
        findings = sr.build_review(payload)["verified"]["findings"]
        self.assertIn("SCHEME-FT-CONTRADICTION",
                      rule_ids(findings))

    def test_follow_through_with_evidence_is_not_flagged_unsupported(self):
        payload = {"questions": [{"question_number": "8", "max_marks": 1,
                                  "criteria": [
                                      criterion("a", "A1ft", 1,
                                                follow_through=True,
                                                source_page=2,
                                                source_evidence="A1ft follows")]}]}
        record = sr.build_review(payload)
        self.assertNotIn("C5-FT-UNSUPPORTED",
                         rule_ids(record["verified"]["findings"]))

    def test_all_missing_source_pages_block_silent_certification(self):
        payload = {"questions": [{"question_number": "1", "max_marks": 2,
                                  "criteria": [
                                      criterion("a", "M1", 1),
                                      criterion("b", "A1", 1, dependencies=["a"])]}]}
        findings = sr.build_review(payload)["verified"]["findings"]
        self.assertIn("C5-SOURCE-PAGE-MISSING", rule_ids(findings))

    def test_examiners_special_guidance_is_review_not_verdict(self):
        payload = {"questions": [{"question_number": "1",
                                  "max_marks": 1, "diagram_dependent": True,
                                  "criteria": [criterion("a", "B1", 1,
                                                         source_page=2,
                                                         source_evidence="B1 shown")]}]}
        document = {"page_count": 3, "pages": [
            {"page": 2, "text": "B1 shown", "status": "extracted",
             "warnings": [], "objects": []}]}
        findings = sr.build_review(
            payload, evidence_document=document)["verified"]["findings"]
        self.assertIn("SCHEME-DIAGRAM-REVIEW", rule_ids(findings))
        diagram = [f for f in findings
                   if f["rule_id"] == "SCHEME-DIAGRAM-REVIEW"]
        self.assertEqual(diagram[0]["category"], "EXAMINER_GUIDANCE_REQUIRED")


class EvidenceMappingTests(unittest.TestCase):
    DOCUMENT = {"schema": "investigation.pdf_evidence/1", "document_id": "d1",
                "document_role": "mark_scheme", "sha256": "abc", "page_count": 4,
                "extraction_status": "complete", "warnings": [], "pages": [
        {"page": 1, "text": "no criteria here", "status": "extracted",
         "warnings": [], "objects": []},
        {"page": 2, "text": "The official M1 line for a valid method",
         "status": "extracted", "warnings": [], "objects": []},
        {"page": 3, "text": "", "status": "empty_text_layer",
         "warnings": [], "objects": []},
    ]}

    mapping_record = {
        "schema": "investigation.pdf_question_map/1", "document_id": "d1",
        "document_role": "mark_scheme", "sha256": "abc", "page_count": 4,
        "question_ids": ["1"], "matches": [
            {"question_id": "1", "certainty": "explicit", "references": [],
             "pages": [2]}],
        "unresolved": [], "unmatched_headings": [], "hint_pages": {},
        "unheadable_ids": [], "warnings": [],
    }

    def test_verbatim_evidence_conflict_is_contradiction(self):
        payload = {"questions": [{"question_number": "1", "max_marks": 1,
                                  "criteria": [criterion("a", "M1", 1,
                                                         source_page=2,
                                                         source_evidence="text that never appears")]}]}
        record = sr.build_review(payload, evidence_document=self.DOCUMENT,
                                 question_mapping=self.mapping_record)
        conflicts = [f for f in record["verified"]["findings"]
                     if f["rule_id"] == "C5-EVIDENCE-CONFLICT"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["state"],
                         "verified_structural_inconsistency")
        self.assertEqual(conflicts[0]["category"], "SOURCE_EVIDENCE_CONFLICT")

    def test_conflicting_question_mapping_is_flagged(self):
        payload = {"questions": [{"question_number": "5", "max_marks": 1,
                                  "criteria": [criterion("a", "B1", 1,
                                                         source_page=2,
                                                         source_evidence="valid")]}]}
        # Question 5 is not in the mapping, and not present in matches.
        mapping = json.loads(json.dumps(self.mapping_record))
        record = sr.build_review(payload, question_mapping=mapping)
        self.assertIn("C5-MAPPING-CONFLICT",
                      rule_ids(record["verified"]["findings"]))

    def test_unresolvable_mapping_is_uncertain_not_confirmed(self):
        payload = {"questions": [{"question_number": "1", "max_marks": 1,
                                  "criteria": [criterion("a", "B1", 1,
                                                         source_page=2,
                                                         source_evidence="valid")]}]}
        mapping = {"matches": [], "unresolved": [
            {"question_id": "1", "reasons": ["all_pages_lack_text_layer"]}]}
        record = sr.build_review(payload, question_mapping=mapping)
        rules = rule_ids(record["verified"]["findings"])
        self.assertIn("C5-MAPPING-CONFLICT", rules)

    def test_out_of_range_page_is_verified_inconsistency(self):
        payload = {"questions": [{"question_number": "1", "max_marks": 1,
                                  "criteria": [criterion("a", "M1", 1,
                                                         source_page=99,
                                                         source_evidence="anything")]}]}
        record = sr.build_review(payload, evidence_document=self.DOCUMENT,
                                 question_mapping=self.mapping_record)
        self.assertIn("C5-SOURCE-PAGE-OUT-OF-RANGE",
                      rule_ids(record["verified"]["findings"]))

    def test_missing_text_layer_is_unavailable_not_contradictory(self):
        payload = {"questions": [{"question_number": "1", "max_marks": 1,
                                  "criteria": [criterion("a", "M1", 1,
                                                         source_page=3,
                                                         source_evidence="unseen line")]}]}
        record = sr.build_review(payload, evidence_document=self.DOCUMENT,
                                 question_mapping=self.mapping_record)
        unavailable = [f for f in record["verified"]["findings"]
                       if f["rule_id"] == "C5-EVIDENCE-UNAVAILABLE"]
        self.assertEqual(len(unavailable), 1)
        self.assertNotIn("C5-EVIDENCE-CONFLICT",
                         rule_ids(record["verified"]["findings"]))
        self.assertEqual(unavailable[0]["evidence"].get("page_status"),
                         "empty_text_layer")


class ApprovalStateTests(unittest.TestCase):
    def setUp(self):
        from scheme_authorization import ApprovalAuthority, ServerIdentity
        self.authority = ApprovalAuthority(lambda assignment: ServerIdentity("teacher-a", "login-a"))
        self.authorization = self.authority.authorize(None)

    def context(self, record):
        if "approval_binding" not in record:
            sr.bind_scheme_review(record, self.authorization,
                                  review_id="review-" + str(len(self.authority._reviews)),
                                  document_digest="a" * 64)
        return self.authorization

    def clean_record(self):
        payload = {"questions": [{"question_number": "1", "max_marks": 1,
                                  "criteria": [
                                      criterion("a", "B1", 1, source_page=2,
                                                source_evidence="B1 shown")]}]}
        document = {"page_count": 3, "pages": [
            {"page": 2, "text": "B1 shown", "status": "extracted",
             "warnings": [], "objects": []}]}
        mapping = {"matches": [{"question_id": "1", "certainty": "explicit",
                                "references": [], "pages": [2]}],
                   "unresolved": []}
        return sr.build_review(payload, evidence_document=document,
                               question_mapping=mapping)

    def test_clean_scheme_can_be_teacher_approved_once(self):
        record = self.clean_record()
        sr.approve_scheme_review(record, self.context(record), record["scheme_revision"])
        self.assertEqual(record["teacher"]["status"], "TEACHER_APPROVED")
        state, reason = sr.approval_state(record, self.authorization,
                                          record["scheme_revision"])
        self.assertEqual(state, "approved")
        self.assertEqual(record["teacher"]["approved_by"], "teacher-a")

    def test_different_teacher_cannot_inherit_approval(self):
        record = self.clean_record()
        sr.approve_scheme_review(record, self.context(record), record["scheme_revision"])
        state, reason = sr.approval_state(record, "teacher-b",
                                          record["scheme_revision"])
        self.assertEqual(state, "invalid")

    def test_stale_revision_invalidates_approval(self):
        record = self.clean_record()
        revision = record["scheme_revision"]
        sr.approve_scheme_review(record, self.context(record), revision)
        changed = json.loads(json.dumps(record))
        changed["scheme_revision"] = "new-revision"
        state, reason = sr.approval_state(changed, self.authorization, revision)
        self.assertEqual(state, "invalid")
        self.assertEqual(reason, "review_content_changed")

    def test_approval_blocked_on_unresolved_findings(self):
        inputs = self.clean_record()["approval_inputs"]
        inputs["interpretation"]["questions"][0]["max_marks"] = 5
        record = sr.build_review(**inputs)
        context = self.context(record)
        with self.assertRaises(sr.SchemeReviewApprovalError) as error:
            sr.approve_scheme_review(record, context,
                                     record["scheme_revision"])
        self.assertIn("unresolved_finding", str(error.exception))

    def test_missing_evidence_cannot_be_silently_certified(self):
        payload = {"questions": [{"question_number": "1", "max_marks": 1,
                                  "criteria": [criterion("a", "M1", 1,
                                                         source_page=99,
                                                         source_evidence="x")]}]}
        document = {"page_count": 3, "pages": []}
        mapping = {"matches": [], "unresolved": []}
        record = sr.build_review(payload, evidence_document=document,
                                 question_mapping=mapping)
        with self.assertRaises(sr.SchemeReviewApprovalError):
            sr.approve_scheme_review(record, self.context(record),
                                     record["scheme_revision"])
        # Source faults require correction and revalidation, never a waiver.
        context = self.context(record)
        with self.assertRaises(sr.SchemeReviewApprovalError):
            sr.resolve_findings(record, [f["finding_id"] for f in
                                         sr.unresolved_findings(record)], context)
        self.assertNotEqual(record["teacher"]["status"], "TEACHER_APPROVED")

    def test_new_findings_after_approval_reopen_review(self):
        record = self.clean_record()
        sr.approve_scheme_review(record, self.context(record), record["scheme_revision"])
        extra = sr._finding("TOTAL_MISMATCH", "C5-total", question_number="9",
                            severity="high", state=sr.C2_VERIFIED)
        record["verified"]["findings"].append(extra)
        state, reason = sr.approval_state(record, self.authorization,
                                          record["scheme_revision"])
        self.assertEqual(state, "invalid")
        self.assertEqual(reason, "review_content_changed")

    def test_anonymous_identifiers_cannot_approve(self):
        record = self.clean_record()
        with self.assertRaises(sr.SchemeReviewApprovalError):
            sr.approve_scheme_review(record, "", record["scheme_revision"])
        self.assertEqual(record["teacher"]["status"], "CHECKED")

    def test_genuine_confinders_cannot_be_waived_without_teacher(self):
        payload = {"questions": [{"question_number": "1", "max_marks": 2,
                                  "criteria": [
                                      criterion("a", "M1", 1, source_page=2,
                                                source_evidence="B1 shown"),
                                      criterion("b", "A1", 1,
                                                dependencies=["nothere"], source_page=2,
                                                source_evidence="B1 shown")]}]}
        inputs = self.clean_record()["approval_inputs"]
        inputs["interpretation"] = payload
        record = sr.build_review(**inputs)
        context = self.context(record)
        with self.assertRaisesRegex(sr.SchemeReviewApprovalError, "unresolved_finding"):
            sr.approve_scheme_review(record, context,
                                     record["scheme_revision"])

    def test_assisted_findings_block_certification(self):
        record = self.clean_record()
        findings, outcome = sr.parse_assisted_response(
            assisted_response(json.dumps({"findings": [{
                    "category": "CRITERION_MISSING",
                    "explanation": "synthetic mocked claim",
                    "question_number": "1", "criterion_id": "a"}]})))
        self.assertEqual(outcome, "completed")
        sr.apply_assisted_findings(record, findings, outcome)
        with self.assertRaises(sr.SchemeReviewApprovalError) as error:
            sr.approve_scheme_review(record, self.context(record),
                                     record["scheme_revision"])
        self.assertIn("assisted_review_unresolved", str(error.exception))

    def test_assisted_failure_blocks_certification(self):
        record = self.clean_record()
        sr.apply_assisted_outcome(record, "timeout")
        with self.assertRaises(sr.SchemeReviewApprovalError):
            sr.approve_scheme_review(record, self.context(record),
                                     record["scheme_revision"])
        # A clean completed no-findings assisted pass never blocks.
        other = self.clean_record()
        findings, outcome = sr.parse_assisted_response(
            assisted_response(
                json.dumps({"findings": []})))
        self.assertEqual(outcome, "completed_no_findings")
        sr.apply_assisted_findings(other, findings, outcome)
        sr.approve_scheme_review(other, self.context(other), other["scheme_revision"])
        self.assertEqual(other["teacher"]["status"], "TEACHER_APPROVED")


class ProductionExtractionReviewTests(unittest.TestCase):
    def test_production_maxima_review_is_clean_when_consistent(self):
        record = sr.review_extracted_maxima({"1": 3, "2": 4},
                                            {"1": "a", "2": "b"},
                                            question_numbers=["1", "2"],
                                            specification="Edexcel 4MA1")
        self.assertEqual(record["verified"]["findings"], [])
        self.assertEqual(record["teacher"]["status"], "CHECKED")
        self.assertEqual(record["extracted"]["maxima"], {"1": 3, "2": 4})

    def test_invalid_maxima_identifiers_are_flagged(self):
        record = sr.review_extracted_maxima(
            {" 1a ": 3, "10": 4})
        rules = rule_ids(record["verified"]["findings"])
        self.assertIn("C5-QID-INVALID", rules)

    def test_question_coveraged_against_declared_numbers(self):
        record = sr.review_extracted_maxima({"1": 3}, None,
                                            question_numbers=["1", "2", "7"])
        rules = rule_ids(record["verified"]["findings"])
        self.assertIn("C5-QUESTION-NOT-INTERPRETED", rules)

    def test_empty_extraction_cannot_be_certified(self):
        record = sr.review_extracted_maxima({})
        rules = rule_ids(record["verified"]["findings"])
        self.assertIn("C5-EXTRACTION-EMPTY", rules)
        self.assertEqual(record["teacher"]["status"], "NEEDS_TEACHER_REVIEW")
        with self.assertRaises(sr.SchemeReviewApprovalError):
            sr.approve_scheme_review(record, "teacher-a",
                                     record["scheme_revision"])

    def test_answer_summaries_stay_bounded(self):
        big = "x" * 10000
        record = sr.review_extracted_maxima({"1": 2}, {"1": big})
        self.assertEqual(len(record["extracted"]["answers"]["1"]), 500)


class AssistedReviewTests(unittest.TestCase):
    def test_no_live_calls_exist_in_the_module(self):
        source = (Path(sr.__file__).with_name("scheme_review.py")).read_text(
            encoding="utf-8")
        self.assertFalse(sr.ASSISTED_REVIEW_ACTIVATED)
        self.assertNotIn("generate_content", source)
        self.assertNotIn("from google", source)
        self.assertNotIn("client.", source)
        self.assertNotIn("requests.", source)

    def test_mocked_completion_no_findings_marks_suggestions_only(self):
        record = sr.review_extracted_maxima({"1": 2})
        findings, outcome = sr.parse_assisted_response(
            assisted_response(json.dumps({"findings": []})))
        self.assertEqual(outcome, "completed_no_findings")
        before = json.dumps(record["verified"]["findings"])
        sr.apply_assisted_findings(record, findings, outcome)
        self.assertEqual(before, json.dumps(record["verified"]["findings"]))
        self.assertTrue(record["assisted"]["suggestions"])
        self.assertEqual(record["teacher"]["status"], "CHECKED")

    def test_mocked_findings_never_become_verified_information(self):
        record = sr.review_extracted_maxima({"1": 2})
        payload = {"findings": [{
            "category": "CRITERION_MISSING", "question_number": "1",
            "criterion_id": None, "explanation": "Method mark missing",
            "confidence": "medium", "evidence_reference": "page 2",
            "suggested_action": "Teacher check"}]}
        payload["findings"][0]["criterion_id"] = None
        findings, outcome = sr.parse_assisted_response(
            assisted_response(json.dumps(payload)))
        self.assertEqual(outcome, "completed")
        sr.apply_assisted_findings(record, findings, outcome)
        self.assertEqual(record["verified"]["findings"], [])
        self.assertEqual(len(record["assisted"]["findings"]), 1)
        self.assertEqual(record["assisted"]["findings"][0]["origin"],
                         "assisted")
        self.assertEqual(record["teacher"]["status"], "NEEDS_TEACHER_REVIEW")

    def test_invalid_json_outcome(self):
        findings, outcome = sr.parse_assisted_response(
            assisted_response("this is not json"))
        self.assertIsNone(findings)
        self.assertEqual(outcome, "invalid_json")

    def test_missing_schema_fields_outcome(self):
        findings, outcome = sr.parse_assisted_response(
            assisted_response(json.dumps({"other": []})))
        self.assertEqual(outcome, "invalid_response_shape")

    def test_unknown_category_or_bad_confidence_is_rejected(self):
        bad = json.dumps({"findings": [{"category": "MADE_UP",
                                        "explanation": "x"}]})
        findings, outcome = sr.parse_assisted_response(
            assisted_response(bad))
        self.assertEqual(outcome, "invalid_response_shape")
        bad_conf = json.dumps({"findings": [
            {"category": "CRITERION_MISSING", "explanation": "x",
             "confidence": "certainty"}]})
        findings, outcome = sr.parse_assisted_response(
            assisted_response(bad_conf))
        self.assertEqual(outcome, "invalid_response_shape")

    def test_hallucinated_criterion_cannot_be_certified(self):
        record = sr.review_extracted_maxima({"1": 2})
        payload = {"findings": [{
            "category": "CRITERION_UNSUPPORTED",
            "question_number": "55", "explanation": "Q55 B1 fabricated",
            "confidence": "high"}]}
        findings, outcome = sr.parse_assisted_response(
            assisted_response(json.dumps(payload)))
        sr.apply_assisted_findings(record, findings, outcome)
        self.assertEqual(record["verified"]["findings"], [])
        self.assertEqual(record["extracted"]["maxima"], {"1": 2})
        self.assertNotIn("B1", json.dumps(record["extracted"]))

    def test_unsupported_assisted_findings_list_shape(self):
        findings, outcome = sr.parse_assisted_response(
            assisted_response(json.dumps({"findings": ["not dict"]})))
        self.assertEqual(outcome, "invalid_response_shape")

    def test_blocked_incomplete_and_empty_and_large_outcomes(self):
        findings, outcome = sr.parse_assisted_response(
            assisted_response(json.dumps({"findings": []}),
                                   finish="MAX_TOKENS"))
        self.assertEqual(outcome, "blocked_or_incomplete_response")
        findings, outcome = sr.parse_assisted_response(
            assisted_response("   "))
        self.assertEqual(outcome, "empty_response")
        findings, outcome = sr.parse_assisted_response(
            assisted_response("x" * 200_000))
        self.assertEqual(outcome, "response_too_large")

    def test_out_of_range_page_cited_by_assisted_findings(self):
        record = sr.review_extracted_maxima({"1": 2})
        payload = {"findings": [{
            "category": "SOURCE_EVIDENCE_MISSING", "question_number": "1",
            "explanation": "page reference invalid", "confidence": "low"}]}
        findings, outcome = sr.parse_assisted_response(
            assisted_response(json.dumps(payload)))
        sr.apply_assisted_findings(record, findings, outcome)
        self.assertEqual(record["teacher"]["status"], "NEEDS_TEACHER_REVIEW")

    def test_timeout_quota_and_model_failures_are_content_free(self):
        self.assertEqual(sr.classify_assisted_failure(TimeoutError("t")),
                         "timeout")
        self.assertEqual(sr.classify_assisted_failure(
            RuntimeError("gemini quota exceeded")), "quota_exhausted")
        self.assertEqual(sr.classify_assisted_failure(
            RuntimeError("pipeline down")), "model_error")
        for outcome in ("timeout", "quota_exhausted", "model_error"):
            record = sr.review_extracted_maxima({"1": 2})
            sr.apply_assisted_outcome(record, outcome)
            self.assertEqual(record["assisted"]["outcome"], outcome)
            self.assertIn("failure_note", record["assisted"])
            self.assertEqual(record["teacher"]["status"],
                             "NEEDS_TEACHER_REVIEW")

    def test_assisted_outcome_never_produces_any_content(self):
        record = sr.review_extracted_maxima({"1": 2}, {"1": "SECRET-VALUE"})
        sr.apply_assisted_outcome(record, "timeout")
        self.assertNotIn("SECRET-VALUE", json.dumps(record["assisted"]))


class BoundsAndSafetyTests(unittest.TestCase):
    def test_oversized_information_is_finding_not_exception(self):
        payload = {"questions": [
            {"question_number": str(n), "max_marks": 1, "criteria": []}
            for n in range(1, 400)]}
        record = sr.build_review(payload)
        self.assertLessEqual(len(record["extracted"]["questions"]), 100)


if __name__ == "__main__":
    unittest.main()
