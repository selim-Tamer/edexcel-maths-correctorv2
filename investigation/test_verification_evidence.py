"""Offline regression tests for Stage C3 evidence linking and alignment.

Investigation-only. No network, no AI API calls, no student data.
Synthetic PDFs are built in-memory; findings are produced by the REAL
Stage C2 engine (including its golden benchmark cases).
"""
import copy
import json
import unittest

from investigation import pdf_test_fixtures as fixtures
from investigation.pdf_evidence import extract_pdf_evidence
from investigation.pdf_question_map import map_question_pages
from investigation.verification_evidence import (
    ALIGNMENT_RULES, CERTAINTY_AMBIGUOUS, CERTAINTY_EXPLICIT,
    CERTAINTY_UNAVAILABLE, check_document_alignment,
    link_findings_to_pdf_evidence, validate_c2_finding)
from investigation.verification_engine import (
    INSUFFICIENT, REVIEW_STATES, SUSPECTED, HUMAN_REVIEW, verify_scheme)


def _scheme_document(lines_per_page, **kwargs):
    return extract_pdf_evidence(
        fixtures.build_pdf(lines_per_page, **kwargs), "mark_scheme")


def _paper_document(lines_per_page, **kwargs):
    return extract_pdf_evidence(
        fixtures.build_pdf(lines_per_page, **kwargs), "question_paper")


SCHEME_PAGES = [
    ["Q1 (a) M1: forms 2x + 3 = 11",
     "Q1 (b) A1: x = 4",
     "Q2 (a) total 5 marks row: B1"],
    ["alternative method row for Q2"],
    ["Q3 (c) A1: completes the table"],
]

PAPER_PAGES = [
    ["Question 1 total for this question: 2 marks",
     "Solve 2x + 3 = 11",
     "Question 2 total for this question: 5 marks"],
    ["Question 3 total for this question: 4 marks"],
]


def _findings_with_mismatch():
    """Real C2 findings: weight/total mismatch plus an unsupported mark code
    (the latter carries criterion ids for criterion-level hint tests)."""
    return verify_scheme({
        "provenance": {"author": "synthetic test", "verified": False},
        "questions": [
            {"id": "Q1", "total": 5,
             "criteria": [{"id": "Q1-c1", "code": "M1", "weight": 1},
                          {"id": "Q1-c2", "code": "A1", "weight": 3}]},
            {"id": "Q3", "total": None,
             "criteria": [{"id": "Q3-x", "code": "X1", "weight": 2}]},
        ],
    })


class LinkingTests(unittest.TestCase):

    def test_real_c2_findings_link_to_scheme_pages(self):
        findings = _findings_with_mismatch()
        self.assertTrue(findings)          # real engine output
        for finding in findings:
            validate_c2_finding(finding)
        scheme_document = _scheme_document(SCHEME_PAGES)
        scheme_mapping = map_question_pages(scheme_document, ["Q1", "Q2"])
        enriched = link_findings_to_pdf_evidence(
            findings, mark_scheme_document=scheme_document,
            mark_scheme_mapping=scheme_mapping)
        mismatch = next(f for f in enriched
                        if f["rule_id"] == "SCHEME-TOTAL-MISMATCH")
        self.assertEqual(mismatch["pdf_evidence"]["certainty"],
                         CERTAINTY_EXPLICIT)
        section = mismatch["pdf_evidence"]["mark_scheme"]
        self.assertEqual(section["page_count"],
                         scheme_document["page_count"])
        self.assertTrue(section["links"])
        self.assertTrue(all(reference["question_id"] == "Q1"
                            for reference in section["links"]))
        # original finding data byte-identical apart from pdf_evidence:
        original = next(f for f in findings
                        if f["finding_id"] == mismatch["finding_id"])
        preserved = {key: value for key, value in mismatch.items()
                     if key != "pdf_evidence"}
        self.assertEqual(preserved, original)

    def test_original_findings_never_mutated(self):
        findings = _findings_with_mismatch()
        snapshot = copy.deepcopy(findings)
        scheme_document = _scheme_document(SCHEME_PAGES)
        mapping = map_question_pages(scheme_document, ["Q1", "Q2"])
        link_findings_to_pdf_evidence(findings, mark_scheme_document=scheme_document,
                                      mark_scheme_mapping=mapping)
        self.assertEqual(findings, snapshot)

    def test_finding_without_mapping_has_no_invented_evidence(self):
        findings = _findings_with_mismatch()
        enriched = link_findings_to_pdf_evidence(findings)
        for finding in enriched:
            self.assertEqual(finding["pdf_evidence"]["certainty"],
                             CERTAINTY_UNAVAILABLE)
            self.assertIsNone(finding["pdf_evidence"]["question_paper"])
            self.assertIsNone(finding["pdf_evidence"]["mark_scheme"])
            self.assertFalse(finding["pdf_evidence"]["evidence_available"])
            self.assertIn("No page-level mapping",
                          finding["pdf_evidence"]["note"])
            self.assertEqual(
                {key: value for key, value in finding.items()
                 if key != "pdf_evidence"},
                next(original for original in findings
                     if original["finding_id"] == finding["finding_id"]))

    def test_ambiguous_mapping_is_never_promoted_to_verified(self):
        findings = _findings_with_mismatch()
        scheme_document = _scheme_document([
            ["Q1 (a) M1: rows"], ["filler"], ["Q1 (b) duplicate heading"]])
        mapping = map_question_pages(scheme_document, ["Q1"])
        self.assertEqual(mapping["matches"][0]["certainty"], "ambiguous")
        enriched = link_findings_to_pdf_evidence(
            findings, mark_scheme_document=scheme_document,
            mark_scheme_mapping=mapping)
        mismatch = next(f for f in enriched
                        if f["rule_id"] == "SCHEME-TOTAL-MISMATCH")
        self.assertEqual(mismatch["pdf_evidence"]["certainty"],
                         CERTAINTY_AMBIGUOUS)
        self.assertEqual(mismatch["state"],
                         next(f for f in findings
                              if f["finding_id"] == mismatch["finding_id"])
                         ["state"])
        # all candidates kept visible
        self.assertEqual(
            len(mismatch["pdf_evidence"]["mark_scheme"]["links"]), 2)

    def test_heuristic_continuation_stays_labelled(self):
        findings = _findings_with_mismatch()
        scheme_document = _scheme_document(SCHEME_PAGES)
        mapping = map_question_pages(scheme_document, ["Q1", "Q2", "Q3"],
                                     continuation_policy="heuristic_adjacent")
        q2_match = next(m for m in mapping["matches"]
                        if m["question_id"] == "Q2")
        self.assertEqual(q2_match["certainty"], "explicit")
        self.assertIn(2, q2_match["pages"])
        enriched = link_findings_to_pdf_evidence(
            findings, mark_scheme_document=scheme_document,
            mark_scheme_mapping=mapping)
        mismatch = next(f for f in enriched
                        if f["rule_id"] == "SCHEME-TOTAL-MISMATCH")
        origins = {reference["data_origin"]
                   for reference in mismatch["pdf_evidence"]["mark_scheme"]
                   ["links"]}
        self.assertIn("pdf_extracted", origins)

    def test_criterion_hints_attach_only_when_explicit(self):
        findings = _findings_with_mismatch()
        coded = next(f for f in findings
                     if f["rule_id"] == "SCHEME-CODE-UNSUPPORTED")
        self.assertEqual(coded["criterion_ids"], ["Q3-x"])
        scheme_document = _scheme_document(SCHEME_PAGES)
        mapping = map_question_pages(scheme_document, ["Q1", "Q2", "Q3"])
        enriched = link_findings_to_pdf_evidence(
            [coded], mark_scheme_document=scheme_document,
            mark_scheme_mapping=mapping,
            criterion_page_hints={"mark_scheme": {"Q3-x": [1]}})
        mismatch = enriched[0]
        hints = mismatch["pdf_evidence"]["criterion_page_hints"]
        self.assertEqual([hint["criterion_id"] for hint in hints],
                         ["Q3-x"])
        self.assertEqual(hints[0]["pages"], [1])
        self.assertEqual(hints[0]["document_role"], "mark_scheme")
        out_of_finding_hints = link_findings_to_pdf_evidence(
            [coded], mark_scheme_document=scheme_document,
            mark_scheme_mapping=mapping,
            criterion_page_hints={"mark_scheme": {"Q9-c9": [1]}})
        mismatch = out_of_finding_hints[0]
        self.assertEqual(mismatch["pdf_evidence"]["criterion_page_hints"], [])

    def test_invalid_criterion_hints_rejected(self):
        findings = _findings_with_mismatch()
        scheme_document = _scheme_document(SCHEME_PAGES)
        mapping = map_question_pages(scheme_document, ["Q1", "Q2"])
        with self.assertRaises(ValueError):
            link_findings_to_pdf_evidence(
                findings, mark_scheme_document=scheme_document,
                mark_scheme_mapping=mapping,
                criterion_page_hints={"unknown_role": {"Q1-c1": [1]}})
        with self.assertRaises(ValueError):
            link_findings_to_pdf_evidence(
                findings, mark_scheme_document=scheme_document,
                mark_scheme_mapping=mapping,
                criterion_page_hints={"mark_scheme": {"Q1-c1": []}})
        with self.assertRaises(ValueError):
            link_findings_to_pdf_evidence(
                findings, mark_scheme_document=scheme_document,
                mark_scheme_mapping=mapping,
                criterion_page_hints={"mark_scheme": {"Q1-c1": [99]}})

    def test_mapping_document_digest_agreement_enforced(self):
        findings = _findings_with_mismatch()
        scheme_document = _scheme_document(SCHEME_PAGES)
        mapping = map_question_pages(scheme_document, ["Q1", "Q2"])
        other_document = _scheme_document(
            [["Q1 (a) different digest scheme text"]])
        with self.assertRaises(ValueError):
            link_findings_to_pdf_evidence(
                findings, mark_scheme_document=other_document,
                mark_scheme_mapping=mapping)

    def test_non_finding_inputs_rejected(self):
        scheme_document = _scheme_document(SCHEME_PAGES)
        mapping = map_question_pages(scheme_document, ["Q1", "Q2"])
        with self.assertRaises(ValueError):
            link_findings_to_pdf_evidence(
                [{"not": "a finding"}],
                mark_scheme_document=scheme_document,
                mark_scheme_mapping=mapping)

    def test_output_deterministic_and_json_serializable(self):
        findings = _findings_with_mismatch()
        scheme_document = _scheme_document(SCHEME_PAGES)
        paper_document = _paper_document(PAPER_PAGES)
        scheme_mapping = map_question_pages(scheme_document, ["Q1", "Q2"])
        paper_mapping = map_question_pages(paper_document, ["Q1", "Q2", "Q3"])
        args = dict(mark_scheme_document=scheme_document,
                    mark_scheme_mapping=scheme_mapping,
                    question_paper_document=paper_document,
                    question_paper_mapping=paper_mapping)
        first = link_findings_to_pdf_evidence(findings, **args)
        second = link_findings_to_pdf_evidence(findings, **args)
        self.assertEqual(first, second)
        json.dumps(first)

    def test_extraction_warnings_surface_on_linked_findings(self):
        findings = _findings_with_mismatch()
        scheme_document = _scheme_document(
            [["Q1 (a) M1 row"], None], path_only_pages=(1,))
        mapping = map_question_pages(scheme_document, ["Q1"])
        enriched = link_findings_to_pdf_evidence(
            findings, mark_scheme_document=scheme_document,
            mark_scheme_mapping=mapping)
        warnings = enriched[0]["pdf_evidence"]["extraction_warnings"]
        self.assertTrue(
            any("empty page" in warning or "text layer" in warning
                for warning in warnings),
            warnings)


class AlignmentTests(unittest.TestCase):

    def alignment_case(self, scheme_pages, paper_pages, scheme_ids=None,
                       paper_ids=None, **kwargs):
        scheme_document = _scheme_document(scheme_pages, **kwargs)
        paper_document = _paper_document(paper_pages, **kwargs)
        scheme_mapping = map_question_pages(
            scheme_document, scheme_ids or ["Q1", "Q2", "Q3"],
            **{k: v for k, v in kwargs.items() if k == "continuation_policy"})
        paper_mapping = map_question_pages(
            paper_document, paper_ids or ["Q1", "Q2", "Q3"])
        findings = check_document_alignment(
            question_paper_document=paper_document,
            question_paper_mapping=paper_mapping,
            mark_scheme_document=scheme_document,
            mark_scheme_mapping=scheme_mapping)
        return findings, scheme_document, paper_document

    def test_fully_aligned_documents_have_no_alignment_findings(self):
        findings, _, _ = self.alignment_case(
            [p for p in PAPER_PAGES], [p for p in PAPER_PAGES])
        self.assertEqual(findings, [])

    def test_question_in_paper_but_not_in_scheme_is_suspected(self):
        # The paper maps question 3; the supplied scheme pages contain no
        # question 3 heading at all (and the caller ids ask for it).
        findings, _, _ = self.alignment_case(
            SCHEME_PAGES[:2], PAPER_PAGES,
            scheme_ids=["Q1", "Q2", "Q3"], paper_ids=["Q1", "Q2", "Q3"])
        absent = [f for f in findings
                  if f["rule_id"] == "PDF-ALIGN-UNMATCHED-IN-SCHEME"]
        self.assertEqual([f["question_id"] for f in absent], ["Q3"])
        self.assertEqual(absent[0]["state"], SUSPECTED)
        self.assertTrue(absent[0]["review_required"])

    def test_question_in_scheme_but_not_in_paper_is_suspected(self):
        paper_pages = [
            ["Question 1 total for this question: 2 marks",
             "Solve 2x + 3 = 11",
             "Question 2 total for this question: 5 marks"],
            ["part (b) content row without a question heading"],
        ]
        findings, _, _ = self.alignment_case(
            SCHEME_PAGES, paper_pages,
            scheme_ids=["Q1", "Q2", "Q3"], paper_ids=["Q1", "Q2", "Q3"])
        absent = [f for f in findings
                  if f["rule_id"] == "PDF-ALIGN-UNMATCHED-IN-PAPER"]
        self.assertEqual([f["question_id"] for f in absent], ["Q3"])
        self.assertEqual(absent[0]["state"], SUSPECTED)

    def test_missing_text_layer_makes_absence_insufficient(self):
        findings, _, _ = self.alignment_case(
            SCHEME_PAGES, [[], None, None], scheme_ids=["Q1", "Q2", "Q3"],
            paper_ids=["Q1", "Q2", "Q3"], path_only_pages=(1, 2))
        absent = [f for f in findings
                  if f["rule_id"] == "PDF-ALIGN-UNMATCHED-IN-PAPER"]
        self.assertEqual(absent[0]["state"], INSUFFICIENT)
        layers = [f for f in findings
                  if f["rule_id"] == "PDF-ALIGN-TEXT-LAYER-MISSING"]
        self.assertTrue(layers)
        self.assertEqual(layers[0]["state"], INSUFFICIENT)
        self.assertTrue(any("never asserts" in finding["explanation"]
                            or "extracted-text absence never proves" in
                            finding["explanation"] for finding in layers))

    def test_multiple_candidate_pages_require_human_review(self):
        scheme_pages = [SCHEME_PAGES[0], SCHEME_PAGES[1],
                        SCHEME_PAGES[2], SCHEME_PAGES[0]]
        scheme_document = _scheme_document(scheme_pages)
        paper_document = _paper_document(PAPER_PAGES)
        scheme_mapping = map_question_pages(
            scheme_document, ["Q1", "Q2", "Q3"])
        paper_mapping = map_question_pages(paper_document,
                                           ["Q1", "Q2", "Q3"])
        findings = check_document_alignment(
            question_paper_document=paper_document,
            question_paper_mapping=paper_mapping,
            mark_scheme_document=scheme_document,
            mark_scheme_mapping=scheme_mapping)
        ambiguous = [f for f in findings
                     if f["rule_id"] == "PDF-ALIGN-AMBIGUOUS-MAPPING"]
        self.assertEqual(ambiguous[0]["question_id"], "Q1")
        self.assertEqual(ambiguous[0]["state"], HUMAN_REVIEW)
        self.assertEqual(ambiguous[0]["evidence"]["candidate_pages"], [1, 4])

    def test_unmatched_headings_outside_requested_ids(self):
        scheme_document = _scheme_document([
            ["Q1 (a) requested", "Q9 (z) extra heading"]])
        paper_document = _paper_document(PAPER_PAGES)
        scheme_mapping = map_question_pages(scheme_document, ["Q1"])
        paper_mapping = map_question_pages(paper_document, ["Q1", "Q2", "Q3"])
        findings = check_document_alignment(
            question_paper_document=paper_document,
            question_paper_mapping=paper_mapping,
            mark_scheme_document=scheme_document,
            mark_scheme_mapping=scheme_mapping)
        unmatched = [f for f in findings
                     if f["rule_id"] == "PDF-ALIGN-UNMATCHED-HEADINGS"]
        self.assertEqual([f["evidence"]["heading_number"] for f in unmatched],
                         ["9"])

    def test_incomplete_extraction_is_reported(self):
        # A document whose every page genuinely fails (synthetic pdfium
        # fault), reported as extraction_failed then as an alignment gap.
        import io
        import pypdfium2 as pdfium_module
        from pypdf import PdfReader
        from unittest import mock

        class FakeTextPage:
            def count_chars(self):
                raise RuntimeError("synthetic pdfium failure")

            def get_text_range(self, start, count):
                raise RuntimeError("synthetic pdfium failure")

            def close(self):
                pass

        class FakePage:
            def get_objects(self):
                return iter(())

            def get_textpage(self):
                return FakeTextPage()

            def close(self):
                pass

        class FakeDocument:
            def __init__(self, data):
                self.pages = [FakePage(), FakePage(), FakePage()]

            def __len__(self):
                return 3

            def __getitem__(self, index):
                return self.pages[index]

            def close(self):
                pass

        data = fixtures.build_pdf(
            [["Q1 (a) M1 row"], ["Q2 row"], ["Q3 row"]])
        self.assertEqual(len(PdfReader(io.BytesIO(data)).pages), 3)
        with mock.patch.object(pdfium_module, "PdfDocument", FakeDocument):
            document = extract_pdf_evidence(data, "mark_scheme")
        for page in document["pages"]:
            self.assertEqual(page["status"], "extraction_failed")
        self.assertEqual(document["extraction_status"], "extraction_failed")
        findings = check_document_alignment(
            question_paper_document=None,
            question_paper_mapping=None,
            mark_scheme_document=document, mark_scheme_mapping=None)
        self.assertEqual([f["rule_id"] for f in findings],
                         ["PDF-ALIGN-EXTRACTION-INCOMPLETE"])
        self.assertEqual(findings[0]["state"], INSUFFICIENT)
        self.assertEqual(findings[0]["evidence"]["failed_pages"], [1, 2, 3])

    def test_never_reports_verified_claims_about_documents(self):
        # Q3 is mapped in the scheme while the paper text shows no heading
        # for it: the strongest "source documents disagree" case that heuristics
        # can produce — which still must never be reported as a verified claim.
        findings, _, _ = self.alignment_case(
            SCHEME_PAGES, [PAPER_PAGES[0]],
            scheme_ids=["Q1", "Q2", "Q3"], paper_ids=["Q1", "Q2"])
        self.assertTrue(findings)
        for finding in findings:
            self.assertNotEqual(finding["state"],
                                "verified_structural_inconsistency")
            self.assertEqual(finding["review_required"], True)
            self.assertIn(finding["rule_id"], ALIGNMENT_RULES)
            self.assertIn("schema", finding)

    def test_alignment_findings_deterministic_and_serializable(self):
        findings, _, _ = self.alignment_case(
            SCHEME_PAGES, PAPER_PAGES, scheme_ids=["Q1", "Q2", "Q3", "Q4"],
            paper_ids=["Q1", "Q2", "Q3", "Q4"])
        json.dumps(findings)
        again, _, _ = self.alignment_case(
            SCHEME_PAGES, PAPER_PAGES, scheme_ids=["Q1", "Q2", "Q3", "Q4"],
            paper_ids=["Q1", "Q2", "Q3", "Q4"])
        self.assertEqual(findings, again)

    def test_digest_mismatch_rejected(self):
        scheme_document = _scheme_document(SCHEME_PAGES)
        other = _scheme_document([["something else"]])
        mapping = map_question_pages(scheme_document, ["Q1", "Q2"])
        for document in (other,):    # mapping digest disagrees with doc
            with self.assertRaises(ValueError):
                check_document_alignment(
                    question_paper_document=document,
                    question_paper_mapping=mapping)


class VocabularyTests(unittest.TestCase):

    def test_c2_states_are_reused_without_extension(self):
        # state vocabulary is exactly the C2 set; nothing invented here
        self.assertEqual(set(REVIEW_STATES),
                         {"verified_structural_inconsistency",
                          "suspected_issue", "evidence_insufficient",
                          "human_review_required"})

    def test_alignment_rules_are_documented_and_stable(self):
        self.assertEqual(ALIGNMENT_RULES, (
            "PDF-ALIGN-UNMATCHED-IN-PAPER",
            "PDF-ALIGN-UNMATCHED-IN-SCHEME",
            "PDF-ALIGN-AMBIGUOUS-MAPPING",
            "PDF-ALIGN-UNMATCHED-HEADINGS",
            "PDF-ALIGN-TEXT-LAYER-MISSING",
            "PDF-ALIGN-EXTRACTION-INCOMPLETE",
        ))

    def test_validate_c2_finding_rejects_unknown_states(self):
        base = verify_scheme({
            "provenance": {"author": "synthetic", "verified": False},
            "questions": [{"id": "Q1", "total": 2, "criteria": [
                {"id": "Q1-c1", "code": "M1", "weight": 1}]}],
        })[0]
        validate_c2_finding(base)
        forged = copy.deepcopy(base)
        forged["state"] = "model_says_fine"
        with self.assertRaises(ValueError):
            validate_c2_finding(forged)
        forged = copy.deepcopy(base)
        forged["not_a_key"] = 1
        with self.assertRaises(ValueError):
            validate_c2_finding(forged)


if __name__ == "__main__":
    unittest.main()
