"""Offline regression tests for the Phase C Stage 2 verification engine.

Investigation-only. No network, no AI API calls, no student data, no
production imports. Every behavioural test asserts EXACT expected findings
(rule id, question id, criterion ids) - successful execution alone is never
accepted, and deliberately valid fixtures must produce ZERO findings.
"""
import json
import unittest

from investigation import scheme_model, verification_engine as engine

PROVENANCE = {"author": "synthetic test", "verified": False}


def _scheme(questions, provenance=PROVENANCE):
    payload = {"questions": questions}
    if provenance is not None:
        payload["provenance"] = provenance
    return payload


def _find(payload):
    return engine.verify_scheme(payload)


def _keys(payload):
    return sorted(engine.finding_key(finding) for finding in _find(payload))


def _one(payload, rule_id):
    matching = [f for f in _find(payload) if f["rule_id"] == rule_id]
    if len(matching) != 1:
        raise AssertionError(
            f"expected exactly one {rule_id}, got {len(matching)}: {matching}")
    return matching[0]


class ModelContractTests(unittest.TestCase):

    def test_valid_payload_parses_with_defaults_not_invented(self):
        document = scheme_model.parse_scheme(_scheme([
            {"id": "Q1", "total": 2, "criteria": [
                {"id": "Q1-M1", "code": "M1", "weight": 1},
                {"id": "Q1-A1", "code": "A1", "weight": 1,
                 "dependencies": ["Q1-M1"]}]}]))
        self.assertEqual(document.questions[0].total, 2)
        self.assertEqual(document.provenance, PROVENANCE)
        self.assertEqual(document.questions[0].criteria[1].dependencies, ["Q1-M1"])
        # Unavailable fields stay None; nothing is inferred.
        self.assertIsNone(document.questions[0].diagram_dependent)
        self.assertIsNone(document.questions[0].claimed_solution)

    def test_unknown_fields_are_retained_verbatim_and_ignored(self):
        document = scheme_model.parse_scheme(_scheme([
            {"id": "Q1", "exotic_ai_confidence": 0.9, "criteria": [
                {"id": "Q1-c1", "editor_mood": "neutral",
                 "code": "M1", "weight": 1}]}]))
        self.assertEqual(
            document.questions[0].extras,
            {"exotic_ai_confidence": 0.9})
        self.assertEqual(
            document.questions[0].criteria[0].extras, {"editor_mood": "neutral"})

    def test_malformed_payloads_raise_scheme_model_error(self):
        malformed = [
            [], [],                                       # not an object / not used
            {"questions": "no"},
            _scheme([[1]]),                               # question not an object
            _scheme([{"criteria": []}]),                  # missing question id
            _scheme([{"id": "Q1", "criteria": [{"id": 42}]}]),
            _scheme([{"id": "Q1", "criteria": [{"id": "Q1-c1", "weight": "3"}]}]),
            _scheme([{"id": "Q1", "criteria": [{"id": "Q1-c1", "weight": True}]}]),
            _scheme([{"id": "Q1", "criteria": [{"id": "Q1-c1", "weight": False}]}]),
            _scheme([{"id": "Q1", "criteria": [{"id": "Q1-c1", "weight": 1.5}]}]),
            _scheme([{"id": "Q1", "criteria": [{"id": "Q1-c1", "weight": 1,
                                                 "follow_through": "false"}]}]),
            _scheme([{"id": "Q1", "total": 2, "diagram_dependent": 1, "criteria": []}]),
            _scheme([{"id": "Q1", "criteria": [
                {"id": "Q1-c1", "dependencies": "Q1-c2"}]}]),
            _scheme([{"id": "Q1", "criteria": [
                {"id": "Q1-c1", "dependencies": [""]}]}]),
            _scheme([{"id": "Q1", "claimed_solution": 7, "criteria": []}]),
            _scheme([{"id": "Q1", "total": True, "criteria": []}]),
            None, "scheme", 42, [],
        ]
        del malformed[1]                                  # drop the duplicated stub
        for payload in malformed:
            with self.assertRaises(scheme_model.SchemeModelError):
                scheme_model.parse_scheme(payload)

    def test_missing_optional_and_missing_code_weight_are_not_errors(self):
        document = scheme_model.parse_scheme(_scheme([
            {"id": "Q1", "criteria": [{"id": "Q1-c1"}]}]))
        criterion = document.questions[0].criteria[0]
        self.assertIsNone(criterion.code)
        self.assertIsNone(criterion.weight)
        self.assertEqual(criterion.dependencies, [])

    def test_oversized_records_raise(self):
        oversized_note = "x" * (scheme_model.MAX_NOTE_CHAR_LIMIT + 1)
        with self.assertRaises(scheme_model.SchemeModelError):
            scheme_model.parse_scheme(_scheme([
                {"id": "Q1", "criteria": [
                    {"id": "Q1-c1", "note": oversized_note}]}]))
        with self.assertRaises(scheme_model.SchemeModelError):
            scheme_model.parse_scheme({"questions": [
                {"id": f"Q{i}", "criteria": []}
                for i in range(scheme_model.MAX_QUESTIONS + 1)]})
        with self.assertRaises(scheme_model.SchemeModelError):
            too_many_criteria = [
                {"id": f"Q1-c{i}", "code": "B1", "weight": 1}
                for i in range(scheme_model.MAX_CRITERIA_PER_QUESTION + 1)]
            scheme_model.parse_scheme(_scheme([
                {"id": "Q1", "criteria": too_many_criteria}]))
        with self.assertRaises(scheme_model.SchemeModelError):
            too_many_deps = [f"Q1-c{i}" for i in range(
                scheme_model.MAX_DEPENDENCIES_PER_CRITERION + 1)]
            scheme_model.parse_scheme(_scheme([
                {"id": "Q1", "criteria": [{"id": "Q1-root", "dependencies": too_many_deps}]}]))

    def test_mark_code_grammar(self):
        supported = ["M1", "A1", "B2", "dM1", "A1ft", "M2ft", "B1ft", "dM2"]
        unsupported = ["X1", "m1", "M0", "dA1", "C1", 10, None, "A1 FT", "", "-", "dM"]
        for code in supported:
            self.assertTrue(scheme_model.code_is_supported(code), code)
        for code in unsupported:
            self.assertFalse(scheme_model.code_is_supported(code), code)
        self.assertTrue(scheme_model.code_has_follow_through_suffix("A1ft"))
        self.assertFalse(scheme_model.code_has_follow_through_suffix("A1"))


class DeterministicCheckTests(unittest.TestCase):
    """Each test asserts the EXACT finding set of its fixture."""

    def test_1_valid_complete_scheme_produces_no_findings(self):
        payload = _scheme([
            {"id": "Q1", "total": 3, "source_ref": {"page": 4}, "criteria": [
                {"id": "Q1-M1", "code": "M1", "weight": 1},
                {"id": "Q1-A1", "code": "A1", "weight": 1,
                 "dependencies": ["Q1-M1"]},
                {"id": "Q1-A1ft", "code": "A1ft", "weight": 1,
                 "dependencies": ["Q1-M1"], "follow_through": True}]},
            {"id": "Q2", "total": 1, "criteria": [
                {"id": "Q2-B1", "code": "B1", "weight": 1}]}])
        self.assertEqual(_keys(payload), [])

    def test_2_duplicate_criterion_ids(self):
        payload = _scheme([
            {"id": "Q1", "total": 2, "criteria": [
                {"id": "Q1-A1", "code": "A1", "weight": 1},
                {"id": "Q1-A1", "code": "A1", "weight": 1}]}])
        self.assertEqual(_keys(payload), [
            ("SCHEME-DUP-CRITERIA", None, ("Q1-A1",))])
        finding = _one(payload, "SCHEME-DUP-CRITERIA")
        self.assertEqual(finding["state"], engine.VERIFIED)
        self.assertEqual(finding["evidence"]["occurrences"], 2)

    def test_3_unsupported_mark_code_syntax(self):
        payload = _scheme([
            {"id": "Q1", "total": 4, "criteria": [
                {"id": "Q1-X2", "code": "X2", "weight": 1},
                {"id": "Q1-M0", "code": "M0", "weight": 1},
                {"id": "Q1-dA1", "code": "dA1", "weight": 1},
                {"id": "Q1-c3", "code": "m1", "weight": 1}]}])
        self.assertEqual(_keys(payload), sorted([
            ("SCHEME-CODE-UNSUPPORTED", "Q1", ("Q1-X2",)),
            ("SCHEME-CODE-UNSUPPORTED", "Q1", ("Q1-M0",)),
            ("SCHEME-CODE-UNSUPPORTED", "Q1", ("Q1-dA1",)),
            ("SCHEME-CODE-UNSUPPORTED", "Q1", ("Q1-c3",))]))
        x2 = [f for f in _find(payload) if f["criterion_ids"] == ["Q1-X2"]][0]
        self.assertEqual(x2["evidence"]["code"], "X2")
        for code in ("X2", "M0", "dA1", "m1"):
            finding = [f for f in _find(payload)
                       if f["evidence"]["code"] == code][0]
            self.assertEqual(finding["rule_id"], "SCHEME-CODE-UNSUPPORTED")
            self.assertEqual(finding["state"], engine.VERIFIED)
            self.assertEqual(finding["evidence"]["supported_bases"],
                             ["M", "A", "B", "dM"])

    def test_4_missing_weight_is_a_finding_not_an_inferred_value(self):
        payload = _scheme([
            {"id": "Q1", "total": 2, "criteria": [
                {"id": "Q1-c1", "code": "M1", "weight": 1},
                {"id": "Q1-c3", "code": "A1"}]}])
        self.assertEqual(_keys(payload), [
            ("SCHEME-FIELD-MISSING", "Q1", ("Q1-c3",)),
            ("SCHEME-TOTAL-UNRECONCILABLE", "Q1", ("Q1-c3",))])
        finding = _one(payload, "SCHEME-FIELD-MISSING")
        self.assertEqual(finding["severity"], "high")
        self.assertEqual(finding["evidence"]["missing_fields"], ["weight"])
        self.assertIn("never filled by inference", finding["explanation"])

    def test_5_zero_and_negative_weights(self):
        payload = _scheme([
            {"id": "Q1", "total": 2, "criteria": [
                {"id": "Q1-M1", "code": "M1", "weight": 0},
                {"id": "Q1-A1", "code": "A1", "weight": -1}]}])
        self.assertEqual(_keys(payload), sorted([
            ("SCHEME-WEIGHT-NONPOSITIVE", "Q1", ("Q1-A1",)),
            ("SCHEME-WEIGHT-NONPOSITIVE", "Q1", ("Q1-M1",)),
            ("SCHEME-TOTAL-UNRECONCILABLE", "Q1", ("Q1-A1", "Q1-M1"))]))

    def test_6_correct_total_reconciliation_produces_no_findings(self):
        payload = _scheme([
            {"id": "Q1", "total": 4, "criteria": [
                {"id": "Q1-M1", "code": "M1", "weight": 2},
                {"id": "Q1-A1", "code": "A1", "weight": 1},
                {"id": "Q1-B1", "code": "B1", "weight": 1}]}])
        self.assertEqual(_keys(payload), [])

    def test_7_incorrect_total_reconciliation(self):
        payload = _scheme([
            {"id": "Q1", "total": 5, "criteria": [
                {"id": "Q1-M1", "code": "M1", "weight": 1},
                {"id": "Q1-A1", "code": "A1", "weight": 1},
                {"id": "Q1-B1", "code": "B1", "weight": 2}]}])
        self.assertEqual(_keys(payload), [
            ("SCHEME-TOTAL-MISMATCH", "Q1", ())])
        finding = _one(payload, "SCHEME-TOTAL-MISMATCH")
        self.assertEqual(finding["state"], engine.VERIFIED)
        self.assertEqual(finding["severity"], "high")
        self.assertEqual(finding["evidence"]["explicit_total"], 5)
        self.assertEqual(finding["evidence"]["criterion_weight_sum"], 4)

    def test_8_undefined_dependency_reference(self):
        payload = _scheme([
            {"id": "Q1", "total": 2, "criteria": [
                {"id": "Q1-M1", "code": "M1", "weight": 1},
                {"id": "Q1-A1", "code": "A1", "weight": 1,
                 "dependencies": ["Q1-M2"]}]}])
        finding = _one(payload, "SCHEME-DEP-UNDEFINED")
        self.assertEqual(
            (finding["question_id"], tuple(finding["criterion_ids"]),
             finding["evidence"]["dependency_id"]),
            ("Q1", ("Q1-A1",), "Q1-M2"))
        self.assertEqual(finding["state"], engine.VERIFIED)

    def test_9_self_dependency(self):
        payload = _scheme([
            {"id": "Q1", "total": 1, "criteria": [
                {"id": "Q1-M1", "code": "M1", "weight": 1,
                 "dependencies": ["Q1-M1"]}]}])
        self.assertEqual(_keys(payload), [
            ("SCHEME-DEP-SELF", "Q1", ("Q1-M1",))])

    def test_10_dependency_cycle(self):
        payload = _scheme([
            {"id": "Q1", "total": 3, "criteria": [
                {"id": "Q1-M1", "code": "M1", "weight": 1},
                {"id": "Q1-A1", "code": "A1", "weight": 1,
                 "dependencies": ["Q1-B1"]},
                {"id": "Q1-B1", "code": "B1", "weight": 1,
                 "dependencies": ["Q1-A1", "Q1-M1"]}]}])
        self.assertEqual(_keys(payload), [
            ("SCHEME-DEP-CYCLE", None, ("Q1-A1", "Q1-B1"))])
        finding = _one(payload, "SCHEME-DEP-CYCLE")
        self.assertEqual(finding["evidence"]["cycle_members"], ["Q1-A1", "Q1-B1"])

    def test_11_valid_dependent_method_mark_produces_no_findings(self):
        payload = _scheme([
            {"id": "Q1", "total": 3, "criteria": [
                {"id": "Q1-M1", "code": "M1", "weight": 1},
                {"id": "Q1-dM1", "code": "dM1", "weight": 1,
                 "dependencies": ["Q1-M1"]},
                {"id": "Q1-A1", "code": "A1", "weight": 1,
                 "dependencies": ["Q1-M1", "Q1-dM1"]}]}])
        self.assertEqual(_keys(payload), [])

    def test_12_valid_follow_through_mark_produces_no_findings(self):
        payload = _scheme([
            {"id": "Q1", "total": 2, "criteria": [
                {"id": "Q1-M1", "code": "M1", "weight": 1},
                {"id": "Q1-A1ft", "code": "A1ft", "weight": 1,
                 "dependencies": ["Q1-M1"], "follow_through": True}]}])
        self.assertEqual(_keys(payload), [])

    def test_13_valid_alternative_group_produces_no_findings(self):
        payload = _scheme([
            {"id": "Q1", "total": 2, "criteria": [
                {"id": "Q1-A1", "code": "A1", "weight": 1,
                 "alternative_group": "METHOD-A"},
                {"id": "Q1-A2", "code": "A1", "weight": 1,
                 "alternative_group": "METHOD-A"}]}])
        self.assertEqual(_keys(payload), [])

    def test_13b_singleton_alternative_group_is_invalid(self):
        payload = _scheme([
            {"id": "Q1", "total": 1, "criteria": [
                {"id": "Q1-A3", "code": "A1", "weight": 1,
                 "alternative_group": "ORPHAN-C"}]}])
        self.assertEqual(_keys(payload), [
            ("SCHEME-ALT-GROUP-INVALID", "Q1", ("Q1-A3",))])
        finding = _one(payload, "SCHEME-ALT-GROUP-INVALID")
        self.assertEqual(finding["evidence"]["alternative_group"], "ORPHAN-C")

    def test_14_ambiguous_evidence_reports_gaps_and_states(self):
        payload = _scheme([
            {"id": "Q1", "criteria": [
                {"id": "Q1-c1", "note": "The scanned fraction could read 1/2 or 1/3.",
                 "source_ref": {"page": 6, "line": None}}]}],
            provenance={"author": "test", "extraction_state": "ambiguous"})
        self.assertEqual(_keys(payload), sorted([
            ("SCHEME-FIELD-MISSING", "Q1", ("Q1-c1",)),
            ("SCHEME-TOTAL-ABSENT", "Q1", ())]))
        missing = _one(payload, "SCHEME-FIELD-MISSING")
        absent = _one(payload, "SCHEME-TOTAL-ABSENT")
        self.assertEqual(missing["state"], engine.VERIFIED)
        self.assertEqual(absent["state"], engine.INSUFFICIENT)
        self.assertIn("No total is inferred", absent["explanation"])

    def test_15_missing_document_references(self):
        payload = {"questions": [
            {"id": "Q1", "total": 1, "criteria": [
                {"id": "Q1-B1", "code": "B1", "weight": 1}]}]}
        self.assertEqual(_keys(payload), [
            ("SCHEME-PROVENANCE-ABSENT", None, ())])
        finding = _one(payload, "SCHEME-PROVENANCE-ABSENT")
        self.assertEqual(finding["state"], engine.INSUFFICIENT)
        self.assertEqual(finding["severity"], "low")

    def test_15b_provenance_present_produces_no_findings(self):
        payload = _scheme([
            {"id": "Q1", "total": 1, "criteria": [
                {"id": "Q1-B1", "code": "B1", "weight": 1}]}])
        self.assertEqual(_keys(payload), [])

    def test_16_unsupported_mathematical_claim(self):
        payload = _scheme([
            {"id": "Q1", "total": 2, "claimed_solution": "3x = 15, so x = 5.",
             "criteria": [
                 {"id": "Q1-M1", "code": "M1", "weight": 1},
                 {"id": "Q1-A1", "code": "A1", "weight": 1,
                  "dependencies": ["Q1-M1"]}]}])
        finding = _one(payload, "SCHEME-MATH-UNSUPPORTED")
        self.assertEqual(finding["state"], engine.INSUFFICIENT)
        self.assertTrue(finding["review_required"])
        self.assertIn("no arithmetic or algebraic verification",
                      finding["explanation"])
        self.assertEqual(finding["criterion_ids"], ["Q1-A1", "Q1-M1"])

    def test_17_malicious_and_malformed_input_rejected_before_any_check(self):
        for payload in (None, [], "scheme", 42):
            with self.assertRaises(scheme_model.SchemeModelError):
                engine.verify_scheme(payload)

    def test_17b_duplicated_dependency_declaration(self):
        payload = _scheme([
            {"id": "Q1", "total": 2, "criteria": [
                {"id": "Q1-M1", "code": "M1", "weight": 1},
                {"id": "Q1-A1", "code": "A1", "weight": 1,
                 "dependencies": ["Q1-M1", "Q1-M1"]}]}])
        finding = _one(payload, "SCHEME-DEP-DUPLICATED")
        self.assertEqual(
            (finding["question_id"], tuple(finding["criterion_ids"]),
             finding["evidence"]["dependency_id"]),
            ("Q1", ("Q1-A1",), "Q1-M1"))

    def test_18_no_false_positives_on_deliberate_valid_contract(self):
        # valid official-style exception + valid diagram-free scheme with
        # full provenance: zero findings
        payload = _scheme([
            {"id": "Q1", "total": 1, "criteria": [
                {"id": "Q1-B1", "code": "B1", "weight": 1,
                 "note": "Correct answer without working earns B1; method is "
                         "implied and no working is commanded."}]}])
        self.assertEqual(_find(payload), [])


class FollowThroughDeclarationTests(unittest.TestCase):

    def test_code_suffix_with_false_flag_is_contradictory(self):
        payload = _scheme([
            {"id": "Q1", "total": 1, "criteria": [
                {"id": "Q1-A1ft", "code": "A1ft", "weight": 1,
                 "follow_through": False}]}])
        finding = _one(payload, "SCHEME-FT-CONTRADICTION")
        self.assertEqual(finding["state"], engine.VERIFIED)
        self.assertEqual(finding["evidence"],
                         {"code": "A1ft", "follow_through": False})

    def test_flag_true_without_suffix_is_suspected_not_contradictory(self):
        payload = _scheme([
            {"id": "Q1", "total": 1, "criteria": [
                {"id": "Q1-Q", "code": "A1", "weight": 1,
                 "follow_through": True}]}])
        finding = _one(payload, "SCHEME-FT-MISMATCH")
        self.assertEqual(finding["state"], engine.SUSPECTED)
        self.assertEqual(finding["severity"], "low")

    def test_absent_flag_produces_no_false_positive(self):
        payload = _scheme([
            {"id": "Q1", "total": 1, "criteria": [
                {"id": "Q1-A1", "code": "A1", "weight": 1}]}])
        self.assertEqual(_keys(payload), [])


class DuplicateQuestionIdTests(unittest.TestCase):

    def test_duplicate_question_ids_merge_colliding_scope_findings(self):
        payload = _scheme([
            {"id": "Q7", "criteria": [{"id": "Q7-M1", "code": "M1", "weight": 1}]},
            {"id": "Q7", "criteria": [{"id": "Q7-B1", "code": "B1", "weight": 1}]}])
        self.assertEqual(_keys(payload), sorted([
            ("SCHEME-DUP-QUESTIONS", "Q7", ()),
            ("SCHEME-TOTAL-ABSENT", "Q7", ())]))
        absent = _one(payload, "SCHEME-TOTAL-ABSENT")
        self.assertEqual(absent["evidence"]["colliding_occurrences"], 2)


class DiagramDependenceTests(unittest.TestCase):

    def test_explicit_diagram_dependence_requires_human_review(self):
        payload = _scheme([
            {"id": "Q9", "total": 1, "diagram_dependent": True, "criteria": [
                {"id": "Q9-B1", "code": "B1", "weight": 1}]}])
        finding = _one(payload, "SCHEME-DIAGRAM-REVIEW")
        self.assertEqual(finding["state"], engine.HUMAN_REVIEW)
        self.assertEqual(finding["criterion_ids"], ["Q9-B1"])

    def test_diagram_dependent_absent_or_false_is_silent(self):
        payload = _scheme([
            {"id": "Q9", "total": 1, "criteria": [
                {"id": "Q9-B1", "code": "B1", "weight": 1}]}])
        self.assertEqual(_keys(payload), [])


class BoundedTraversalTests(unittest.TestCase):

    def setUp(self):
        # 49-criterion dependency chain in one question (at the cap boundary).
        chain = []
        for index in range(1, 50):
            dependencies = [f"Q1-c{index - 1}"] if index > 1 else []
            chain.append({"id": f"Q1-c{index}", "code": "M1", "weight": 1,
                          "dependencies": dependencies})
        self.payload = _scheme([{"id": "Q1", "total": 49, "criteria": chain}])

    def test_long_chain_completes_without_recursion_and_no_findings(self):
        self.assertEqual(_keys(self.payload), [])


class FindingContractTests(unittest.TestCase):

    def test_every_finding_has_complete_required_fields(self):
        payload = _scheme([
            {"id": "Q1", "total": 3, "criteria": [
                {"id": "Q1-M1", "code": "X2", "weight": 0},
                {"id": "Q1-A1", "weight": 1, "dependencies": ["Q1-Z9"]},
                {"id": "Q1-A1ft", "code": "A1ft", "weight": 1,
                 "follow_through": False}]}],
            provenance=None)
        for finding in _find(payload):
            self.assertEqual(
                set(finding), {
                    "finding_id", "rule_id", "issue_category", "state",
                    "severity", "question_id", "criterion_ids", "evidence",
                    "explanation", "review_required"})
            self.assertIn(finding["state"], engine.REVIEW_STATES)
            self.assertTrue(finding["review_required"])
            self.assertIn(finding["severity"], ("high", "medium", "low"))
            self.assertIn(finding["rule_id"], engine.SUPPORTED_RULES)
            self.assertIs(finding["review_required"], True)
            self.assertNotIn("confidence", finding)
            self.assertNotIn("confidence", finding["evidence"])

    def test_verification_is_deterministic_across_invocations(self):
        payload = _scheme([
            {"id": "Q1", "total": 2, "criteria": [
                {"id": "Q1-M1", "code": "M1", "weight": 0},
                {"id": "Q1-A1", "code": "A1", "weight": 1,
                 "dependencies": ["Q1-Z9"]}]}])
        first = json.dumps(_find(payload), sort_keys=True)
        second = json.dumps(_find(payload), sort_keys=True)
        self.assertEqual(first, second)


class ModuleHygieneTests(unittest.TestCase):
    """The engine must stay offline and free of code-execution hazards."""

    FORBIDDEN_IMPORTS = (
        "import google", "from google", "import streamlit", "from streamlit",
        "import marking_requests", "from marking_requests",
        "import app5", "from app5", "import urllib", "from urllib",
        "import socket", "import requests", "from requests",
        "eval(", "exec(", "import subprocess", "from subprocess")

    def test_modules_import_nothing_forbidden(self):
        import inspect
        for module in (scheme_model, engine):
            source = inspect.getsource(module)
            for pattern in self.FORBIDDEN_IMPORTS:
                self.assertNotIn(pattern, source,
                                 f"{module.__name__} contains {pattern!r}")

    def test_rules_documented_constant_matches_engine_checks(self):
        self.assertEqual(len(engine.SUPPORTED_RULES), 18)


class BenchmarkTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.report = engine.run_benchmark()
        cls.data = engine.load_benchmark()

    def test_whole_benchmark_passes(self):
        self.assertTrue(self.report["passed"])
        failed = [r["id"] for r in self.report["results"] if r["status"] != "pass"]
        self.assertEqual(failed, [])
        self.assertGreaterEqual(len(self.report["results"]), 15)

    def test_required_category_coverage(self):
        expected_cases = {
            "correct-complete-scheme", "stated-total-mismatch",
            "dependency-missing-criterion", "duplicate-criterion-ids",
            "duplicate-question-ids", "self-dependency", "dependency-cycle",
            "unsupported-mark-codes", "nonpositive-weights",
            "missing-required-fields", "ambiguous-extraction-record",
            "missing-source-information", "diagram-dependent-question",
            "official-style-exception-valid", "claimed-solution-unverifiable",
            "duplicated-dependency-declaration", "contradictory-follow-through",
            "alternative-groups-valid-and-invalid"}
        ids = {case["id"] for case in self.data["cases"]}
        self.assertEqual(expected_cases - ids, set())

    def test_negative_controls_expect_zero_findings(self):
        for case_id in ("correct-complete-scheme", "official-style-exception-valid"):
            case = next(c for c in self.data["cases"] if c["id"] == case_id)
            self.assertEqual(case["expected_findings"], [], case_id)
            self.assertEqual(engine.verify_scheme(case["scheme"]), [], case_id)

    def test_out_of_stage_cases_never_get_fabricated_math_verdicts(self):
        # Categories that WOULD claim a mathematical verdict about the scheme
        # content must never be reported by the Stage C2 engine at all.
        math_verdict_categories = {"question_total_mismatch"}
        verdict_rules = {"SCHEME-TOTAL-MISMATCH"}
        for case in self.data["cases"]:
            if case.get("unsupported_in_stage"):
                for finding in engine.verify_scheme(case["scheme"]):
                    self.assertNotIn(finding["rule_id"], verdict_rules,
                                     case["id"])
                    self.assertNotIn(finding["issue_category"],
                                     math_verdict_categories, case["id"])

    def test_specific_out_of_stage_cases_have_gap_or_review_states(self):
        expectations = {
            "diagram-dependent-question": engine.HUMAN_REVIEW,
            "claimed-solution-unverifiable": engine.INSUFFICIENT,
            "ambiguous-extraction-record": engine.INSUFFICIENT,
        }
        for case_id, expected_state in expectations.items():
            case = next(c for c in self.data["cases"] if c["id"] == case_id)
            states = {finding["state"]
                      for finding in engine.verify_scheme(case["scheme"])}
            self.assertIn(expected_state, states, case_id)

    def test_benchmark_provenance_is_synthetic_and_not_official(self):
        self.assertIn("NOT official Pearson", self.data["provenance"])

    def test_malformed_benchmark_is_rejected(self):
        import pathlib
        import tempfile
        original = (pathlib.Path(engine.__file__).parent
                    / engine.BENCHMARK_FILENAME)
        with tempfile.TemporaryDirectory() as tmp:
            content = json.loads(original.read_text(encoding="utf-8"))
            content["cases"][0]["id"] = content["cases"][1]["id"]
            target = pathlib.Path(tmp) / "bad.json"
            target.write_text(json.dumps(content), encoding="utf-8")
            with self.assertRaises(ValueError):
                engine.load_benchmark(target)

    def test_finding_ids_are_unique_per_document(self):
        for case in self.data["cases"]:
            findings = engine.verify_scheme(case["scheme"])
            ids = [finding["finding_id"] for finding in findings]
            self.assertEqual(len(ids), len(set(ids)), case["id"])


if __name__ == "__main__":
    unittest.main()
