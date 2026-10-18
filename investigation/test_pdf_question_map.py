"""Offline regression tests for Stage C3 question/page mapping.

Investigation-only. No network, no OCR, no student data. All fixtures are
synthetic PDFs built in-memory; none represents real Pearson material.
"""
import unittest
from unittest import mock

from investigation import pdf_question_map as mapping
from investigation.pdf_evidence import extract_pdf_evidence
from investigation.pdf_question_map import (
    CONTINUATION_POLICIES, METHOD_CONTINUATION, METHOD_HEADING,
    map_question_pages, validate_question_mapping, validate_question_ids)
from investigation.pdf_test_fixtures import build_pdf


def _scheme(lines_per_page, **kwargs):
    return extract_pdf_evidence(build_pdf(lines_per_page, **kwargs),
                                "mark_scheme")


class HeadingRecognitionTests(unittest.TestCase):

    def test_prefix_headings_map_explicitly(self):
        document = _scheme([["Q1 (a) M1: forms equation",
                             "Q1 (b) A1: x = 4"],
                            ["Q2 (a) B1: total 5 marks"]])
        result = map_question_pages(document, ["Q1", "Q2"])
        self.assertEqual(result["matches"][0]["certainty"], "explicit")
        self.assertEqual(result["matches"][0]["pages"], [1])
        self.assertEqual(result["matches"][1]["certainty"], "explicit")
        self.assertEqual(result["matches"][1]["pages"], [2])
        self.assertEqual(result["unresolved"], [])
        for match in result["matches"]:
            for reference in match["references"]:
                self.assertEqual(reference["method"], METHOD_HEADING)
                self.assertEqual(reference["data_origin"], "pdf_extracted")
                self.assertTrue(reference["excerpt"])
        validate_question_mapping(result)

    def test_realistic_paper_headings_map_two_questions_on_one_page(self):
        document = extract_pdf_evidence(build_pdf([
            ["Question 1 total for this question: 2 marks",
             "Solve 2x + 3 = 11",
             "Question 2 total for this question: 5 marks"]
        ]), "question_paper")
        result = map_question_pages(document, ["Q1", "Q2"])
        self.assertEqual(result["matches"][0]["pages"], [1])
        self.assertEqual(result["matches"][1]["pages"], [1])
        self.assertEqual(result["matches"][1]["certainty"], "explicit")

    def test_running_prose_is_not_a_heading(self):
        document = _scheme([["Question 3 asks to verify the claim",
                             "so we review question 4 rows below"]])
        result = map_question_pages(document, ["Q3", "Q4"])
        self.assertEqual(result["matches"][0]["certainty"], "unresolved")
        self.assertEqual(result["matches"][1]["certainty"], "unresolved")
        self.assertEqual(result["unmatched_headings"], [])

    def test_numbers_in_working_are_never_headings(self):
        document = _scheme([
            ["x2 = 8", "(67 + 1) / 2 = 34", "2. Find the mean.",
             "1/2 of 10 = 5", "Question 3 (a) real heading"]])
        result = map_question_pages(document, ["Q3"])
        self.assertEqual(result["matches"][0]["pages"], [1])
        self.assertEqual(result["matches"][0]["references"][0]["excerpt"],
                         "Question 3 (a) real heading")

    def test_bare_numbers_off_by_default(self):
        document = _scheme([["1.", "2)"]])
        result = map_question_pages(
            document, ["Q1"], allow_bare_numbers=False)
        self.assertEqual(result["matches"][0]["certainty"], "unresolved")
        result2 = map_question_pages(
            document, ["Q1"], allow_bare_numbers=True)
        self.assertEqual(result2["matches"][0]["certainty"], "explicit")
        # Strictly isolated number-separator lines only; numbers inside
        # sentences or lists are still not headings.
        prose = _scheme([["1. Some numbered list item"]])
        prose_result = map_question_pages(prose, ["Q1"],
                                          allow_bare_numbers=True)
        self.assertEqual(prose_result["matches"][0]["certainty"],
                         "unresolved")

    def test_fraction_and_superscript_ambiguity_never_creates_mappings(self):
        document = _scheme([["x2 1/2 (67+1)/2 x^2 10-3 table rows"]])
        result = map_question_pages(document, ["Q1"])
        self.assertEqual(result["matches"][0]["certainty"], "unresolved")
        self.assertEqual(result["matches"][0]["references"], [])
        self.assertFalse(result["unmatched_headings"])


class SpanningAndContinuationTests(unittest.TestCase):

    def test_explicit_only_policy_never_guesses_spanning_pages(self):
        document = _scheme([["Question 1 total 2 marks", "part one"],
                            ["continuation working without heading"],
                            ["Question 2 total 3 marks", "more"]])
        result = map_question_pages(document, ["Q1", "Q2"],
                                    continuation_policy="explicit_only")
        q1 = result["matches"][0]
        self.assertEqual(q1["pages"], [1])
        self.assertEqual(q1["certainty"], "explicit")
        self.assertTrue(all(ref["method"] == METHOD_HEADING
                            for ref in q1["references"]))

    def test_heuristic_continuation_is_opt_in_and_labelled(self):
        document = _scheme([["Question 1 total 2 marks", "part one"],
                            ["continuation working without heading"],
                            ["Question 2 total 3 marks", "more"]])
        result = map_question_pages(document, ["Q1", "Q2"],
                                    continuation_policy="heuristic_adjacent")
        q1 = result["matches"][0]
        self.assertEqual(q1["pages"], [1, 2])
        continuations = [ref for ref in q1["references"]
                         if ref["method"] == METHOD_CONTINUATION]
        self.assertEqual([ref["page"] for ref in continuations], [2])
        self.assertTrue(all(ref["data_origin"] == "heuristic_inference"
                            for ref in continuations))
        self.assertEqual(q1["certainty"], "explicit")

    def test_blank_text_pages_are_not_claimed_as_continuation(self):
        document = _scheme([["Question 1 total 2 marks"],
                            None,
                            ["Question 2 total 3 marks"]],
                           path_only_pages=(1,))
        result = map_question_pages(document, ["Q1", "Q2"],
                                    continuation_policy="heuristic_adjacent")
        q1 = result["matches"][0]
        self.assertEqual(q1["pages"], [1])

    def test_policy_values_validate(self):
        document = _scheme([["Question 1 total 2 marks"]])
        for policy in CONTINUATION_POLICIES:
            with self.subTest(policy=policy):
                map_question_pages(document, ["Q1"], continuation_policy=policy)
        with self.assertRaises(ValueError):
            map_question_pages(document, ["Q1"], continuation_policy="greedy")


class AmbiguityAndUnresolvedTests(unittest.TestCase):

    def test_duplicate_headings_on_distinct_pages_are_ambiguous(self):
        document = _scheme([["Q2: first heading"],
                            ["filler scheme text"],
                            ["Q2: second heading"]])
        result = map_question_pages(document, ["Q2"])
        self.assertEqual(result["matches"][0]["certainty"], "ambiguous")
        self.assertEqual(result["matches"][0]["pages"], [1, 3])
        self.assertTrue(result["warnings"])

    def test_duplicate_heading_on_one_page_is_not_ambiguous(self):
        document = _scheme([["Q1 (a) part one", "Q1 (b) part two"]])
        result = map_question_pages(document, ["Q1"])
        self.assertEqual(result["matches"][0]["certainty"], "explicit")
        self.assertEqual(len(result["matches"][0]["references"]), 2)

    def test_missing_question_id_is_unresolved_with_reason(self):
        document = _scheme([["Q1 (a) only question present"]])
        result = map_question_pages(document, ["Q1", "Q7"])
        unresolved = {entry["question_id"]: entry["reasons"]
                      for entry in result["unresolved"]}
        self.assertEqual(unresolved["Q7"], ["no_heading_found"])
        self.assertNotIn("Q1", unresolved)

    def test_all_empty_text_layer_pages_are_reported_honestly(self):
        document = _scheme([[], None], path_only_pages=(0, 1))
        result = map_question_pages(document, ["Q1", "Q2"])
        unresolved = {entry["question_id"]: entry["reasons"]
                      for entry in result["unresolved"]}
        self.assertEqual(unresolved["Q1"], ["all_pages_lack_text_layer"])
        self.assertEqual(unresolved["Q2"], ["all_pages_lack_text_layer"])
        self.assertEqual(result["unmatched_headings"], [])

    def test_unmatched_headings_are_recorded_not_invented(self):
        document = _scheme([["Q1 (a) requested",
                             "Q9 (z) not requested"]])
        result = map_question_pages(document, ["Q1"])
        self.assertEqual(result["matches"][0]["certainty"], "explicit")
        self.assertEqual(len(result["unmatched_headings"]), 1)
        self.assertEqual(result["unmatched_headings"][0]["heading_number"],
                         "9")
        self.assertEqual(result["unmatched_headings"][0]["page"], 1)

    def test_unmatchable_id_shapes_require_hints(self):
        document = _scheme([["Q1 (a) heading exists"]])
        result = map_question_pages(document, ["Q1(a)"])
        self.assertEqual(result["unheadable_ids"], ["Q1(a)"])
        entry = result["unresolved"][0]
        self.assertIn("id_shape_not_matchable", entry["reasons"])
        self.assertEqual(entry["question_id"], "Q1(a)")


class HintTests(unittest.TestCase):

    def test_valid_hints_are_recorded_as_caller_supplied(self):
        document = _scheme([["no heading text at all"]])
        result = map_question_pages(document, ["Q1"], hints={"Q1": [1]})
        self.assertEqual(result["matches"][0]["certainty"], "hint_only")
        self.assertEqual(result["hint_pages"]["Q1"], [1])
        heading_references = [reference
                              for reference in result["matches"][0]["references"]
                              if reference["method"] == METHOD_HEADING]
        self.assertEqual(heading_references, [])

    def test_invalid_hints_rejected(self):
        document = _scheme([["page one"]])
        with self.assertRaises(ValueError):
            map_question_pages(document, ["Q1"], hints={"Q7": [1]})
        with self.assertRaises(ValueError):
            map_question_pages(document, ["Q1"], hints={"Q1": [7]})
        with self.assertRaises(ValueError):
            map_question_pages(document, ["Q1"], hints={"Q1": []})
        with self.assertRaises(ValueError):
            map_question_pages(document, ["Q1"], hints={"Q1": [1, 1]})


class InputValidationTests(unittest.TestCase):

    def _document(self):
        return _scheme([["Q1 (a)"]])

    def test_question_id_list_validation(self):
        document = self._document()
        for bad in ([], None, "Q1", [""], [" "], [1],
                    ["Q1", "Q1"], ["Q" + "x" * 300]):
            with self.subTest(bad=bad):
                with self.assertRaises((ValueError, TypeError)):
                    map_question_pages(document,
                                       bad if bad is not None else [])
        with self.assertRaises(ValueError):
            map_question_pages(
                self._document(),
                [f"Q{i}" for i in range(mapping.MAX_QUESTION_IDS + 1)])
        result = map_question_pages(self._document(), ["Q1"])
        self.assertEqual(result["question_ids"], ["Q1"])
        validate_question_ids(["Q1", "Q2"])

    def test_document_validation_is_strict(self):
        document = self._document()
        bad_document = dict(document)
        bad_document["sha256"] = "not-hex"
        with self.assertRaises(ValueError):
            map_question_pages(bad_document, ["Q1"])
        result = map_question_pages(document, ["Q1"])
        validate_question_mapping(result)
        tampered = dict(result)
        tampered["schema"] = "investigation.pdf_question_map/0"
        with self.assertRaises(ValueError):
            validate_question_mapping(tampered)
        broken = dict(result)
        broken["matches"] = []          # count must equal question ids
        with self.assertRaises(ValueError):
            validate_question_mapping(broken)
        misreferenced = dict(result)
        entry = misreferenced["matches"][0]
        misreferenced["matches"] = [
            dict(entry, question_id="Q2")]   # id not in question_ids list
        with self.assertRaises(ValueError):
            validate_question_mapping(misreferenced)

    def test_roles_are_preserved_in_output(self):
        scheme_result = map_question_pages(self._document(), ["Q1"])
        self.assertEqual(scheme_result["document_role"], "mark_scheme")
        paper = extract_pdf_evidence(build_pdf([["Question 1 total"]]),
                                     "question_paper")
        paper_result = map_question_pages(paper, ["Q1"])
        self.assertEqual(paper_result["document_role"], "question_paper")

    def test_mapping_output_is_json_serializable_and_deterministic(self):
        import json
        document = _scheme([["Q1 (a) M1"], ["Q2 (a) A1"]])
        first = map_question_pages(document, ["Q1", "Q2"],
                                   continuation_policy="heuristic_adjacent")
        second = map_question_pages(document, ["Q1", "Q2"],
                                    continuation_policy="heuristic_adjacent")
        self.assertEqual(first, second)
        json.dumps(first)


if __name__ == "__main__":
    unittest.main()
