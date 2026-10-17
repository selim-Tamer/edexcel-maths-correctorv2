"""Deterministic regression tests for the Stage 2B reporting layer.

All tests are offline, deterministic and never call the network or Gemini.
Predictions are built in the documented Stage 2A adapter output shape; one
test also drives marking_adapter.predictions_from_responses() to prove the
declared_unknown abstention contract flows through reporting unchanged.
"""
import copy
import json
import statistics
import unittest
from pathlib import Path

import investigation.marking_metrics as metrics
import investigation.marking_adapter as marker_adapter

_CASES_FILE = Path(metrics.__file__).with_name("accuracy_cases.json")
_AMBIG = "synthetic_unambiguous"


def gold_cases():
    return json.loads(_CASES_FILE.read_text(encoding="utf-8"))["cases"]


def by_id(case_id):
    return next(c for c in gold_cases() if c["id"] == case_id)


def _prediction(case, awards=None, abstain=(), uncertain=None, seconds=None):
    """Adapter-shape completed prediction: labels drive awards (TEST ONLY)."""
    awards = dict(awards or {})
    rows = [{"id": c["id"], "awarded": awards.get(c["id"], False if exp is None else exp)}
            for c, exp in zip(case["scheme"], case["expected"])]
    prediction = {"id": case["id"], "awards": rows}
    prediction["uncertain"] = bool(case.get("uncertain")) if uncertain is None else uncertain
    if abstain:
        prediction["declared_unknown"] = list(abstain)
    if seconds is not None:
        prediction["seconds"] = seconds
    return prediction


def _failed_prediction(case_id, reason, seconds=None):
    prediction = {"id": case_id, "failed": True, "uncertain": False,
                  "failure_reason": reason}
    if seconds is not None:
        prediction["seconds"] = seconds
    return prediction


def _custom_case(case_id, criteria, expected, uncertain=False):
    """criteria: [(code, weight), ...]; criteria IDs follow case_id-cN."""
    scheme = [{"id": f"{case_id}-c{n + 1}", "code": code, "weight": weight}
              for n, (code, weight) in enumerate(criteria)]
    return {"id": case_id, "scheme": scheme, "expected": expected,
            "uncertain": uncertain}


def _custom_governance(case_ids):
    return {
        "definitions": {_AMBIG: "defined in tests", "other": "unused alternative"},
        "cases": {cid: {"classification": _AMBIG} for cid in case_ids},
    }


def _bucket(report, category, *, combined=False):
    summary = report["criteria"]
    table = summary["combined_category_accuracy" if combined else "category_accuracy"]
    return table[category]


class ExactAgreementPolicy(unittest.TestCase):
    """Deterministic tests for the definitive/strict exact-score policy:

    a fully-scorable case with any known-reference abstention is
    exact-INeligible for definitive agreement and receives NO success
    credit in strict all-attempt agreement, even when totals match.
    """

    def _case(self, criteria, expected):
        return _custom_case("exact-policy", criteria, expected)

    def _report(self, case, awards, abstain, uncertain=True):
        rows = [{"id": f"{case['id']}-c{n + 1}", "awarded": awards[n]}
                for n in range(len(awards))]
        prediction = {"id": case["id"], "uncertain": uncertain,
                      "awards": rows,
                      "declared_unknown": abstain}
        return metrics.build_report([case], [prediction],
                                    _custom_governance([case["id"]]))

    def test_all_negative_gold_with_complete_abstention_not_a_match(self):
        # The reported bug's reproduction: allreference False, both abstained;
        # emitted totals (0) numerically equal gold (0) but the model made no
        # definitive marking decisions, so no match is granted.
        case = self._case([("M1", 1), ("A1", 1)], [False, False])
        report = self._report(case, awards=[False, False],
                              abstain=["exact-policy-c1", "exact-policy-c2"])
        summary = report["criteria"]
        self.assertEqual(summary["exact_denominators"], {
            "definitive_completions": 0, "abstained_cases": 1,
            "failed_attempts": 0})
        self.assertIsNone(summary["exact_score_agreement"])
        self.assertIsNone(summary["criterion_vector_agreement"])
        self.assertEqual(summary["strict_all_attempt_exact_score_agreement"], 0.0)
        self.assertEqual(
            summary["strict_all_attempt_criterion_vector_agreement"], 0.0)
        self.assertEqual(summary["fully_scorable_cases"], 1)
        self.assertEqual(summary["known_reference_abstentions"], 2)
        result = report["cases"][0]
        self.assertIsNone(result["exact_score_match"])
        self.assertIsNone(result["criterion_vector_match"])
        self.assertEqual(result["exact_score_ineligible_reason"],
                         "known_reference_abstention")
        self.assertEqual(result["gold_total_marks"], 0)
        self.assertEqual(result["predicted_total_marks"], 0)

    def test_partial_abstention_equal_totals_gets_no_credit(self):
        # Abstaining on a criterion the reference withholds keeps the emitted
        # weighted total equal to gold, yet no exact match is claimed.
        case = self._case([("M1", 2), ("A1", 1)], [True, False])
        report = self._report(case, awards=[True, False],
                              abstain=["exact-policy-c2"])
        summary = report["criteria"]
        self.assertEqual(summary["exact_denominators"], {
            "definitive_completions": 0, "abstained_cases": 1,
            "failed_attempts": 0})
        self.assertIsNone(summary["exact_score_agreement"])
        self.assertIsNone(summary["criterion_vector_agreement"])
        self.assertEqual(summary["strict_all_attempt_exact_score_agreement"], 0.0)
        self.assertEqual(report["cases"][0]["gold_total_marks"],
                         report["cases"][0]["predicted_total_marks"])
        self.assertIsNone(report["cases"][0]["exact_score_match"])

    def test_partial_abstention_unequal_totals_no_definitive_match(self):
        case = self._case([("M1", 1), ("A1", 1)], [True, True])
        report = self._report(case, awards=[True, False],
                              abstain=["exact-policy-c2"])
        summary = report["criteria"]
        self.assertEqual(summary["exact_denominators"]["abstained_cases"], 1)
        self.assertEqual(summary["exact_denominators"]["definitive_completions"], 0)
        self.assertIsNone(summary["exact_score_agreement"])
        self.assertEqual(summary["strict_all_attempt_exact_score_agreement"], 0.0)
        self.assertEqual(summary["weighted_totals"],
                         {"gold_marks_earned": 2, "predicted_marks_earned": 1,
                          "difference": -1})

    def test_fully_answered_matching_totals_different_vector(self):
        # Definitive completions keep exact-score semantics; the differing
        # vector breaks only vector agreement. Strict metrics coincide
        # because no abstentions/failures are present.
        case = self._case([("M1", 1), ("A1", 1)], [True, False])
        report = self._report(case, awards=[False, True],
                              abstain=[], uncertain=False)
        summary = report["criteria"]
        self.assertEqual(summary["exact_denominators"], {
            "definitive_completions": 1, "abstained_cases": 0,
            "failed_attempts": 0})
        self.assertTrue(report["cases"][0]["exact_score_match"])
        self.assertFalse(report["cases"][0]["criterion_vector_match"])
        self.assertEqual(summary["exact_score_agreement"], 1.0)
        self.assertEqual(summary["criterion_vector_agreement"], 0.0)
        self.assertEqual(summary["strict_all_attempt_exact_score_agreement"], 1.0)
        self.assertEqual(
            summary["strict_all_attempt_criterion_vector_agreement"], 0.0)

    def test_full_oracle_predictions_definitive_and_strict(self):
        cases = gold_cases()
        predictions = [_prediction(c, seconds=1.0) for c in cases]
        report = metrics.build_report(cases, predictions, metrics.load_governance())
        summary = report["criteria"]
        self.assertEqual(summary["exact_denominators"], {
            "definitive_completions": 28, "abstained_cases": 0,
            "failed_attempts": 0})
        self.assertEqual(summary["fully_scorable_cases"], 28)
        self.assertEqual(summary["strict_all_attempt_exact_score_agreement"], 1.0)
        self.assertEqual(
            summary["strict_all_attempt_criterion_vector_agreement"], 1.0)
        self.assertEqual(summary["exact_score_agreement"], 1.0)
        self.assertEqual(summary["criterion_vector_agreement"], 1.0)

    def test_mixed_successes_abstentions_and_failures(self):
        cases = gold_cases()
        predictions = []
        for case in cases:
            if case["id"] == "algebra-correct":
                # fully-scorable completed case with known abstentions
                predictions.append({
                    "id": case["id"], "uncertain": True,
                    "awards": [{"id": "crit-alg-c-1", "awarded": False},
                               {"id": "crit-alg-c-2", "awarded": False}],
                    "declared_unknown": ["crit-alg-c-1", "crit-alg-c-2"]})
            elif case["id"] == "probability-isw":
                predictions.append(_failed_prediction(case["id"],
                                                      "explicit_failure"))
            elif case["id"] == "trigonometry":
                continue                       # missing prediction
            else:
                predictions.append(_prediction(case))
        report = metrics.build_report(cases, predictions, metrics.load_governance())
        summary = report["criteria"]
        # 28 fully-scorable cases; algebra-correct abstained, isw failed,
        # trigonometry missing, the other 25 completed definitively.
        self.assertEqual(summary["exact_denominators"], {
            "definitive_completions": 25, "abstained_cases": 1,
            "failed_attempts": 2})
        self.assertEqual(summary["fully_scorable_cases"], 28)
        self.assertEqual(summary["exact_score_agreement"], 1.0)
        self.assertEqual(summary["criterion_vector_agreement"], 1.0)
        self.assertEqual(summary["strict_all_attempt_exact_score_agreement"],
                         25 / 28)
        self.assertEqual(
            summary["strict_all_attempt_criterion_vector_agreement"], 25 / 28)
        abstained_result = report["cases"][0]
        self.assertEqual(abstained_result["id"], "algebra-correct")
        self.assertIsNone(abstained_result["exact_score_match"])
        self.assertEqual(report["governance"]["synthetic_unambiguous"]
                         ["exact_ineligible_abstained_cases"], 1)


class DuplicateAbstentionValidation(unittest.TestCase):
    """declared_unknown must be unique, valid, and mirror canonical False."""

    def _completed_prediction(self):
        case = by_id("algebra-correct")
        return case, _prediction(case)

    def test_duplicate_declared_unknown_ids_are_schema_failure(self):
        case, prediction = self._completed_prediction()
        prediction["declared_unknown"] = ["crit-alg-c-1", "crit-alg-c-1"]
        report = metrics.build_report([case], [prediction],
                                      _custom_governance([case["id"]]))
        self.assertEqual(
            report["prediction_status"]["schema_validation_failure"], 1)
        summary = report["criteria"]
        self.assertEqual(summary["known_reference_abstentions"], 0)
        self.assertEqual(summary["answered_criteria"], 0)
        self.assertEqual(summary["coverage"], None)

    def test_declared_unknown_with_claimed_award_is_schema_failure(self):
        case, prediction = self._completed_prediction()
        prediction["awards"][0]["awarded"] = True            # crit-alg-c-1
        prediction["declared_unknown"] = ["crit-alg-c-1"]
        report = metrics.build_report([case], [prediction],
                                      _custom_governance([case["id"]]))
        self.assertEqual(
            report["prediction_status"]["schema_validation_failure"], 1)
        self.assertEqual(report["criteria"]["known_reference_abstentions"], 0)

    def test_canonical_false_abstention_remains_completed(self):
        case, prediction = self._completed_prediction()
        prediction["awards"][0]["awarded"] = False
        prediction["declared_unknown"] = ["crit-alg-c-1"]
        report = metrics.build_report([case], [prediction],
                                      _custom_governance([case["id"]]))
        self.assertEqual(report["prediction_status"]["completed"], 1)
        self.assertEqual(report["cases"][0]["abstentions"],
                         {"known_reference": ["crit-alg-c-1"],
                          "gold_null": []})

    def test_duplicate_award_rows_are_schema_failure(self):
        case, prediction = self._completed_prediction()
        prediction["awards"] = [prediction["awards"][0],
                                dict(prediction["awards"][0])]
        report = metrics.build_report([case], [prediction],
                                      _custom_governance([case["id"]]))
        self.assertEqual(
            report["prediction_status"]["schema_validation_failure"], 1)


class ReportingBasics(unittest.TestCase):
    def _oracle_predictions(self, cases, seconds=True):
        return [_prediction(c, seconds=float(i + 1) if seconds else None)
                for i, c in enumerate(cases)]

    def test_perfect_predictions_full_benchmark(self):
        cases = gold_cases()
        report = metrics.build_report(cases, self._oracle_predictions(cases),
                                      metrics.load_governance())
        summary = report["criteria"]
        self.assertEqual(report["prediction_status"], {
            "completed": 30, "explicit_model_failure": 0,
            "schema_validation_failure": 0, "missing_prediction": 0})
        self.assertEqual(summary["known_reference_criteria"], 67)
        self.assertEqual(summary["gold_null_criteria"], 2)
        self.assertEqual(summary["answered_criteria"], 67)
        self.assertEqual(summary["known_reference_abstentions"], 0)
        self.assertEqual(summary["award_balance"], {
            "correct_awards": 42, "correct_withholds": 25,
            "false_awards": 0, "incorrect_deductions": 0})
        self.assertEqual(summary["selective_accuracy"], 1.0)
        self.assertEqual(summary["conservative_accuracy"], 1.0)
        self.assertEqual(summary["coverage"], 1.0)
        self.assertEqual(summary["false_award_rate"], 0.0)
        self.assertEqual(summary["incorrect_deduction_rate"], 0.0)
        self.assertEqual(summary["fully_scorable_cases"], 28)
        self.assertEqual(summary["exact_score_agreement"], 1.0)
        self.assertEqual(summary["criterion_vector_agreement"], 1.0)
        self.assertEqual(summary["weighted_totals"], {
            "gold_marks_earned": 42, "predicted_marks_earned": 42, "difference": 0})
        self.assertEqual(summary["partial_known_gold_case_ids"],
                         ["unreadable-final", "speed-ambiguous-handwriting"])
        self.assertEqual(report["uncertainty"], {"expected": 2, "detected": 2,
                                                 "unnecessary": 0,
                                                 "uncertain_completions": 2})
        for stage in ("method_ordinary", "independent_b"):
            self.assertEqual(_bucket(report, stage)["selective_accuracy"], 1.0)

    def test_latent_latency_on_perfect_benchmark(self):
        cases = gold_cases()
        report = metrics.build_report(cases, self._oracle_predictions(cases),
                                      metrics.load_governance())
        timings = sorted(float(i + 1) for i in range(30))
        self.assertEqual(report["latency"]["observations"], 30)
        self.assertEqual(report["latency"]["median_seconds"], statistics.median(timings))
        self.assertEqual(report["latency"]["p90_seconds"], timings[26])
        self.assertEqual(report["latency"]["missing_or_invalid_on_completed"], 0)

    def test_entirely_incorrect_predictions(self):
        cases = gold_cases()
        predictions = []
        for case in cases:
            awards = {}
            for c, exp in zip(case["scheme"], case["expected"]):
                awards[c["id"]] = (not exp) if exp is not None else False
            predictions.append(_prediction(case, awards=awards))
        report = metrics.build_report(cases, predictions, metrics.load_governance())
        summary = report["criteria"]
        self.assertEqual(summary["award_balance"], {
            "correct_awards": 0, "correct_withholds": 0,
            "false_awards": 25, "incorrect_deductions": 42})
        self.assertEqual(summary["selective_accuracy"], 0.0)
        self.assertEqual(summary["conservative_accuracy"], 0.0)
        self.assertEqual(summary["false_award_rate"], 25 / 67)
        self.assertEqual(summary["incorrect_deduction_rate"], 42 / 67)
        self.assertEqual(summary["weighted_totals"], {
            "gold_marks_earned": 42, "predicted_marks_earned": 25, "difference": -17})

    def test_same_total_different_criterion_vector(self):
        cases = [by_id("algebra-partial")]          # gold M1=True, A1=False
        case = cases[0]
        prediction = _prediction(case, awards={"crit-alg-p-1": False,
                                               "crit-alg-p-2": True})
        report = metrics.build_report(cases, [prediction], _custom_governance([case["id"]]))
        summary = report["criteria"]
        self.assertEqual(summary["fully_scorable_cases"], 1)
        self.assertEqual(summary["exact_score_agreement"], 1.0)
        self.assertEqual(summary["criterion_vector_agreement"], 0.0)
        self.assertTrue(report["cases"][0]["exact_score_match"])
        self.assertFalse(report["cases"][0]["criterion_vector_match"])
        self.assertEqual(summary["award_balance"], {
            "correct_awards": 0, "correct_withholds": 0,
            "false_awards": 1, "incorrect_deductions": 1})
        self.assertEqual(summary["selective_accuracy"], 0.0)
        self.assertEqual(summary["weighted_totals"]["difference"], 0)

    def test_json_serializable_report(self):
        cases = gold_cases()
        report = metrics.build_report(cases, self._oracle_predictions(cases),
                                      metrics.load_governance())
        self.assertIsInstance(json.dumps(report), str)


class MarkCategories(unittest.TestCase):
    def test_correct_dM1_withhold_counts_as_dependent_method(self):
        case = by_id("quadratics-dependent-method")   # M1/dM1/A1 all gold False
        report = metrics.build_report([case], [_prediction(case)],
                                      _custom_governance([case["id"]]))
        dependent = _bucket(report, "method_dependent")
        self.assertEqual(dependent["answered"], 1)
        self.assertEqual(dependent["correct_withholds"], 1)
        self.assertEqual(dependent["selective_accuracy"], 1.0)
        method_all = _bucket(report, "method_all", combined=True)
        self.assertEqual(method_all["answered"], 2)
        self.assertEqual(method_all["correct_withholds"], 2)
        self.assertEqual(method_all["selective_accuracy"], 1.0)

    def test_incorrect_dM1_award_counts_as_false_award(self):
        case = by_id("quadratics-dependent-method")
        prediction = _prediction(case, awards={"crit-quad-2": True})
        report = metrics.build_report([case], [prediction],
                                      _custom_governance([case["id"]]))
        dependent = _bucket(report, "method_dependent")
        self.assertEqual(dependent["false_awards"], 1)
        self.assertEqual(dependent["selective_accuracy"], 0.0)
        self.assertEqual(dependent["false_award_rate"], 1.0)
        self.assertFalse(report["cases"][0]["criterion_vector_match"])

    def test_correct_A1ft_award_counts_as_follow_through(self):
        case = by_id("follow-through")   # M1 T, A1 F, A1ft T
        report = metrics.build_report([case], [_prediction(case)],
                                      _custom_governance([case["id"]]))
        follow = _bucket(report, "accuracy_follow_through")
        self.assertEqual(follow["correct_awards"], 1)
        self.assertEqual(follow["selective_accuracy"], 1.0)
        accuracy_all = _bucket(report, "accuracy_all", combined=True)
        self.assertEqual(accuracy_all["answered"], 2)
        self.assertEqual(accuracy_all["correct_awards"] + accuracy_all["correct_withholds"], 2)

    def test_incorrect_A1ft_withhold_counts_as_deduction(self):
        case = by_id("follow-through")
        prediction = _prediction(case, awards={"crit-ft-3": False})
        report = metrics.build_report([case], [prediction],
                                      _custom_governance([case["id"]]))
        follow = _bucket(report, "accuracy_follow_through")
        self.assertEqual(follow["incorrect_deductions"], 1)
        self.assertEqual(follow["selective_accuracy"], 0.0)
        self.assertEqual(follow["incorrect_deduction_rate"], 1.0)

    def test_B1_independent_marks(self):
        case = by_id("graphs-gradient")   # M1/A1/B1 all True
        correct = metrics.build_report([case], [_prediction(case)],
                                       _custom_governance([case["id"]]))
        self.assertEqual(_bucket(correct, "independent_b")["correct_awards"], 1)
        case = by_id("fractions-independent-b-marks")   # B1 T, M1 T, A1 F
        prediction = _prediction(case, awards={"crit-frac-1": False})
        wrong = metrics.build_report([case], [prediction],
                                     _custom_governance([case["id"]]))
        b_bucket = _bucket(wrong, "independent_b")
        self.assertEqual(b_bucket["incorrect_deductions"], 1)
        self.assertEqual(b_bucket["selective_accuracy"], 0.0)
        self.assertEqual(_bucket(wrong, "b_or_independent", combined=True),
                         b_bucket)


class AbstentionReporting(unittest.TestCase):
    def test_abstention_on_known_positive_criterion(self):
        case = by_id("algebra-correct")   # M1 T, A1 T
        prediction = _prediction(case, awards={"crit-alg-c-1": False},
                                 abstain=["crit-alg-c-1"])
        report = metrics.build_report([case], [prediction],
                                      _custom_governance([case["id"]]))
        summary = report["criteria"]
        self.assertEqual(summary["known_reference_abstentions"], 1)
        self.assertEqual(summary["answered_criteria"], 1)
        self.assertEqual(summary["award_balance"]["incorrect_deductions"], 0)
        self.assertEqual(summary["selective_accuracy"], 1.0)
        self.assertEqual(summary["coverage"], 0.5)
        self.assertEqual(summary["conservative_accuracy"], 0.5)
        self.assertEqual(summary["weighted_totals"], {
            "gold_marks_earned": 2, "predicted_marks_earned": 1, "difference": -1})

    def test_abstention_on_known_negative_criterion_is_not_credit(self):
        case = by_id("statistics-arithmetic")   # M1 T, A1 F
        prediction = _prediction(case, awards={"crit-stat-a-2": False},
                                 abstain=["crit-stat-a-2"])
        report = metrics.build_report([case], [prediction],
                                      _custom_governance([case["id"]]))
        summary = report["criteria"]
        self.assertEqual(summary["known_reference_abstentions"], 1)
        self.assertEqual(summary["award_balance"]["correct_withholds"], 0)
        self.assertEqual(summary["selective_accuracy"], 1.0)
        self.assertEqual(summary["coverage"], 0.5)
        self.assertEqual(summary["conservative_accuracy"], 0.5)

    def test_abstention_on_gold_null_criterion_reports_separately(self):
        case = by_id("speed-ambiguous-handwriting")   # M1 T, A1 null
        prediction = _prediction(case, awards={"crit-spd-2": False},
                                 abstain=["crit-spd-2"])
        report = metrics.build_report([case], [prediction],
                                      _custom_governance([case["id"]]))
        summary = report["criteria"]
        self.assertEqual(summary["gold_null_abstentions"], 1)
        self.assertEqual(summary["known_reference_abstentions"], 0)
        self.assertEqual(summary["answered_criteria"], 1)
        self.assertEqual(summary["selective_accuracy"], 1.0)
        self.assertEqual(summary["partial_known_gold_case_ids"], [case["id"]])
        self.assertEqual(report["cases"][0]["abstentions"],
                         {"known_reference": [], "gold_null": ["crit-spd-2"]})

    def test_complete_abstention_on_a_case(self):
        case = by_id("algebra-correct")
        prediction = _prediction(case, awards={"crit-alg-c-1": False,
                                               "crit-alg-c-2": False},
                                 abstain=["crit-alg-c-1", "crit-alg-c-2"],
                                 uncertain=True)
        report = metrics.build_report([case], [prediction],
                                      _custom_governance([case["id"]]))
        summary = report["criteria"]
        self.assertEqual(summary["answered_criteria"], 0)
        self.assertEqual(summary["known_reference_abstentions"], 2)
        self.assertIsNone(summary["selective_accuracy"])
        self.assertIsNone(_bucket(report, "method_ordinary")["selective_accuracy"])
        self.assertEqual(summary["coverage"], 0.0)
        self.assertEqual(summary["conservative_accuracy"], 0.0)
        self.assertEqual(summary["weighted_totals"]["difference"], -2)
        self.assertIsNone(report["cases"][0]["criterion_vector_match"])
        self.assertEqual(report["cases"][0]["exact_score_ineligible_reason"],
                         "known_reference_abstention")

    def test_mixed_answered_and_abstained_criteria(self):
        case = _custom_case("mixed-weights", [("M1", 2), ("M1", 1), ("B1", 1)],
                            [True, True, False])
        prediction = {
            "id": case["id"], "uncertain": True,
            "awards": [{"id": f"{case['id']}-c1", "awarded": False},
                       {"id": f"{case['id']}-c2", "awarded": True},
                       {"id": f"{case['id']}-c3", "awarded": False}],
            "declared_unknown": [f"{case['id']}-c1"],
        }
        report = metrics.build_report([case], [prediction],
                                      _custom_governance([case["id"]]))
        summary = report["criteria"]
        self.assertEqual(summary["answered_criteria"], 2)
        self.assertEqual(summary["known_reference_abstentions"], 1)
        self.assertEqual(summary["selective_accuracy"], 1.0)
        self.assertEqual(summary["coverage"], 2 / 3)
        self.assertEqual(summary["conservative_accuracy"], 2 / 3)
        method = _bucket(report, "method_ordinary")
        self.assertEqual(method["answered"], 1)
        self.assertEqual(method["abstentions"], 1)
        self.assertEqual(method["selective_accuracy"], 1.0)
        self.assertEqual(method["conservative_accuracy"], 0.5)
        self.assertEqual(method["coverage"], 0.5)
        self.assertEqual(summary["weighted_totals"]["difference"], -2)

    def test_adapter_abstention_contract_flows_through(self):
        case = by_id("quadratics-dependent-method")
        rows = []
        for c in case["scheme"]:
            row = {"criterion_id": c["id"], "code": c["code"],
                   "weight": c["weight"]}
            if c["id"] == "crit-quad-2":
                row.update(awarded=False, unknown=True)
            else:
                row.update(awarded=False, unknown=False)
            rows.append(row)
        response = {"id": case["id"], "uncertain": True, "seconds": 1.25,
                    "breakdown": rows}
        prediction = marker_adapter.predictions_from_responses([response], [case])[0]
        self.assertEqual(prediction.get("declared_unknown"), ["crit-quad-2"])
        report = metrics.build_report([case], [prediction],
                                      _custom_governance([case["id"]]))
        summary = report["criteria"]
        self.assertEqual(summary["known_reference_abstentions"], 1)
        self.assertEqual(_bucket(report, "method_dependent")["abstentions"], 1)
        self.assertEqual(summary["award_balance"]["correct_withholds"], 2)
        self.assertEqual(summary["selective_accuracy"], 1.0)
        self.assertEqual(summary["coverage"], 2 / 3)
        self.assertEqual(summary["conservative_accuracy"], 2 / 3)


class FailureSemantics(unittest.TestCase):
    def _single_case_report(self, prediction):
        case = by_id("algebra-correct")
        return metrics.build_report([case], [prediction],
                                    _custom_governance([case["id"]]))

    def test_explicit_model_failure_is_never_a_completion(self):
        prediction = _failed_prediction("algebra-correct", "explicit_failure",
                                        seconds=3.0)
        report = self._single_case_report(prediction)
        self.assertEqual(report["prediction_status"]["explicit_model_failure"], 1)
        self.assertEqual(report["prediction_status"]["completed"], 0)
        result = report["cases"][0]
        self.assertEqual(result["status"], "explicit_model_failure")
        self.assertEqual(result["failure_reason"], "explicit_failure")
        summary = report["criteria"]
        self.assertEqual(summary["known_reference_criteria"], 2)
        self.assertEqual(summary["answered_criteria"], 0)
        self.assertIsNone(summary["selective_accuracy"])
        self.assertEqual(report["latency"]["non_completed_with_valid_timing"], 1)
        self.assertEqual(report["latency"]["observations"], 0)

    def test_adapter_schema_failure_maps_to_validation_bucket(self):
        case = by_id("algebra-correct")
        broken = {"id": "algebra-correct", "uncertain": False}   # no breakdown
        prediction = marker_adapter.predictions_from_responses([broken], [case])[0]
        report = metrics.build_report([case], [prediction],
                                      _custom_governance([case["id"]]))
        self.assertEqual(
            report["prediction_status"]["schema_validation_failure"], 1)
        self.assertEqual(report["cases"][0]["failure_reason"], "missing_breakdown")

    def test_missing_prediction_is_visible_and_never_scores(self):
        cases = [by_id("algebra-correct"), by_id("trigonometry")]
        predictions = [_prediction(cases[1])]
        report = metrics.build_report(cases, predictions, metrics.load_governance())
        self.assertEqual(report["prediction_status"]["missing_prediction"], 1)
        self.assertEqual(report["attempted_cases"], 2)
        missing = report["cases"][0]
        self.assertEqual(missing["status"], "missing_prediction")
        self.assertEqual(missing["known_reference_criteria"], 2)
        summary = report["criteria"]
        self.assertEqual(summary["answered_criteria"], 2)   # only trigonometry
        self.assertEqual(summary["known_reference_criteria"], 4)
        # Coverage is a completed-attempt measure: the missing response is
        # never converted into an abstention or an award; it is exposed
        # through prediction_status and the per-case status instead.
        self.assertEqual(summary["coverage"], 1.0)
        self.assertEqual(summary["selective_accuracy"], 1.0)

    def test_uncertain_but_valid_completion_is_counted(self):
        case = by_id("algebra-correct")
        prediction = _prediction(case, uncertain=True)
        report = self._single_case_report(prediction)
        self.assertEqual(report["prediction_status"]["completed"], 1)
        self.assertEqual(report["uncertainty"]["unnecessary"], 1)
        self.assertEqual(report["uncertainty"]["uncertain_completions"], 1)


class IdentifierValidation(unittest.TestCase):
    def _single_case_report(self, prediction):
        case = by_id("algebra-correct")
        return metrics.build_report([case], [prediction],
                                    _custom_governance([case["id"]]))

    def test_unknown_criterion_id_is_schema_failure(self):
        prediction = _prediction(by_id("algebra-correct"))
        prediction["awards"][0]["id"] = "crit-ghost-1"
        report = self._single_case_report(prediction)
        self.assertEqual(
            report["prediction_status"]["schema_validation_failure"], 1)

    def test_duplicate_criterion_ids_are_schema_failure(self):
        prediction = _prediction(by_id("algebra-correct"))
        prediction["awards"] = [prediction["awards"][0],
                                prediction["awards"][0]]
        report = self._single_case_report(prediction)
        self.assertEqual(
            report["prediction_status"]["schema_validation_failure"], 1)

    def test_non_boolean_award_is_schema_failure(self):
        prediction = _prediction(by_id("algebra-correct"))
        prediction["awards"][0]["awarded"] = "yes"
        report = self._single_case_report(prediction)
        self.assertEqual(
            report["prediction_status"]["schema_validation_failure"], 1)

    def test_unknown_gold_case_prediction_raises(self):
        with self.assertRaises(ValueError):
            metrics.build_report([by_id("algebra-correct")],
                                 [_prediction(by_id("trigonometry"))],
                                 _custom_governance(["algebra-correct"]))

    def test_duplicate_predictions_raise(self):
        prediction = _prediction(by_id("algebra-correct"))
        with self.assertRaises(ValueError):
            metrics.build_report([by_id("algebra-correct")],
                                 [prediction, prediction],
                                 _custom_governance(["algebra-correct"]))

    def test_non_dict_prediction_raises(self):
        with self.assertRaises(ValueError):
            metrics.build_report([by_id("algebra-correct")],
                                 ["not-a-prediction"],
                                 _custom_governance(["algebra-correct"]))


class GovernanceStratification(unittest.TestCase):
    def test_every_case_has_exactly_one_classification(self):
        cases = gold_cases()
        manifest = metrics.load_governance()
        classifications = metrics.governance_classifications(manifest, cases)
        self.assertEqual(len(classifications), 30)
        from collections import Counter
        self.assertEqual(Counter(classifications.values()), {
            "synthetic_unambiguous": 18, "synthetic_provisional_rubric": 10,
            "human_review_required": 2})

    def test_manifest_missing_case_raises(self):
        cases = [by_id("algebra-correct")]
        manifest = {"definitions": {"a": "d"}, "cases": {}}
        with self.assertRaises(ValueError):
            metrics.governance_classifications(manifest, cases)

    def test_manifest_undefined_classification_raises(self):
        manifest = {"definitions": {"a": "d"},
                    "cases": {"algebra-correct": {"classification": "ghost"}}}
        with self.assertRaises(ValueError):
            metrics.governance_classifications(manifest, [by_id("algebra-correct")])

    def test_results_grouped_by_classification(self):
        cases = gold_cases()
        predictions = []
        for case in cases:
            if case["id"] == "algebra-correct":
                continue                            # missing prediction
            if case["id"] == "probability-isw":
                predictions.append(_failed_prediction(case["id"],
                                                      "explicit_failure"))
                continue
            predictions.append(_prediction(case))
        report = metrics.build_report(cases, predictions, metrics.load_governance())
        governance = report["governance"]
        self.assertEqual(set(governance),
                         {"synthetic_unambiguous", "synthetic_provisional_rubric",
                          "human_review_required"})
        self.assertEqual(governance["synthetic_unambiguous"]["cases"], 18)
        self.assertEqual(governance["synthetic_unambiguous"]["completed_attempts"], 17)
        self.assertEqual(governance["synthetic_unambiguous"]["failed_attempts"], 1)
        self.assertEqual(governance["synthetic_provisional_rubric"]["cases"], 10)
        self.assertEqual(governance["synthetic_provisional_rubric"]["completed_attempts"], 9)
        self.assertEqual(governance["human_review_required"]["cases"], 2)
        self.assertEqual(governance["human_review_required"]["completed_attempts"], 2)
        self.assertEqual(sorted(governance["human_review_required"]["case_ids"]),
                         ["speed-ambiguous-handwriting", "unreadable-final"])


class DenominatorsAndWeights(unittest.TestCase):
    def test_weighted_aggregation_and_denominators(self):
        # A(w2,M1,T), B(w1,M1,T), C(w1,B1,F), D(w2,M1,T, abstained)
        case = _custom_case("weighted", [("M1", 2), ("M1", 1), ("B1", 1), ("M1", 2)],
                            [True, True, False, True])
        prediction = {
            "id": case["id"], "uncertain": True,
            "awards": [{"id": f"{case['id']}-c1", "awarded": True},
                       {"id": f"{case['id']}-c2", "awarded": False},
                       {"id": f"{case['id']}-c3", "awarded": False},
                       {"id": f"{case['id']}-c4", "awarded": False}],
            "declared_unknown": [f"{case['id']}-c4"],
        }
        report = metrics.build_report([case], [prediction],
                                      _custom_governance([case["id"]]))
        summary = report["criteria"]
        self.assertEqual(summary["answered_criteria"], 3)
        self.assertEqual(summary["known_reference_abstentions"], 1)
        self.assertEqual(summary["coverage"], 3 / 4)
        self.assertEqual(summary["selective_accuracy"], 2 / 3)
        self.assertEqual(summary["conservative_accuracy"], 0.5)
        self.assertEqual(summary["award_balance"], {
            "correct_awards": 1, "correct_withholds": 1,
            "false_awards": 0, "incorrect_deductions": 1})
        self.assertEqual(summary["weighted_totals"], {
            "gold_marks_earned": 5, "predicted_marks_earned": 2, "difference": -3})
        # One abstention on a fully-scorable case: exact-ineligible, no
        # definitive or strict success credit, counted in the abstained
        # denominator only.
        self.assertIsNone(summary["exact_score_agreement"])
        self.assertEqual(summary["exact_denominators"], {
            "definitive_completions": 0, "abstained_cases": 1,
            "failed_attempts": 0})
        self.assertEqual(summary["strict_all_attempt_exact_score_agreement"], 0.0)
        method = _bucket(report, "method_ordinary")
        self.assertEqual(method["answered"], 2)
        self.assertEqual(method["abstentions"], 1)
        self.assertEqual(method["selective_accuracy"], 0.5)
        self.assertEqual(method["conservative_accuracy"], 1 / 3)
        self.assertEqual(method["coverage"], 2 / 3)

    def test_empty_benchmarks_and_denominators(self):
        report = metrics.build_report([], [], _custom_governance([]))
        self.assertEqual(report["attempted_cases"], 0)
        summary = report["criteria"]
        self.assertIsNone(summary["selective_accuracy"])
        self.assertIsNone(summary["conservative_accuracy"])
        self.assertIsNone(summary["coverage"])
        self.assertIsNone(summary["exact_score_agreement"])
        self.assertIsNone(summary["false_award_rate"])
        self.assertIsNone(report["latency"]["median_seconds"])
        self.assertEqual(report["governance"], {})
        self.assertIsInstance(json.dumps(report), str)

    def test_empty_category_denominator_reports_none(self):
        case = _custom_case("only-b", [("B1", 1)], [True])
        report = metrics.build_report(
            [case],
            [{"id": case["id"], "uncertain": False,
              "awards": [{"id": f"{case['id']}-c1", "awarded": True}]}],
            _custom_governance([case["id"]]))
        self.assertIsNone(_bucket(report, "method_ordinary")["selective_accuracy"])
        self.assertEqual(_bucket(report, "independent_b")["selective_accuracy"], 1.0)

    def test_unknown_mark_code_is_tracked_not_guessed(self):
        case = _custom_case("weird-codes", [("X7", 1), ("M1", 1)], [True, False])
        prediction = _prediction(case)
        report = metrics.build_report([case], [prediction],
                                      _custom_governance([case["id"]]))
        unknown = _bucket(report, metrics.UNKNOWN_CATEGORY)
        self.assertEqual(unknown["answered"], 1)
        self.assertEqual(unknown["correct_awards"], 1)
        self.assertEqual(unknown["selective_accuracy"], 1.0)
        self.assertEqual(report["criteria"]["abstention_by_category"],
                         {name: 0 for name in metrics.CATEGORIES})
        self.assertEqual(report["criteria"]["rate_denominator"],
                         "answered known-reference criteria (2)")

    def test_classify_code_grammar(self):
        self.assertEqual(metrics.classify_code("M1"), "method_ordinary")
        self.assertEqual(metrics.classify_code("M2"), "method_ordinary")
        self.assertEqual(metrics.classify_code("dM1"), "method_dependent")
        self.assertEqual(metrics.classify_code("dM2"), "method_dependent")
        self.assertEqual(metrics.classify_code("A1"), "accuracy_ordinary")
        self.assertEqual(metrics.classify_code("A2"), "accuracy_ordinary")
        self.assertEqual(metrics.classify_code("A1ft"), "accuracy_follow_through")
        self.assertEqual(metrics.classify_code("B1"), "independent_b")
        self.assertEqual(metrics.classify_code("B2"), "independent_b")

    def test_classify_code_unknown_codes(self):
        self.assertIsNone(metrics.classify_code("X7"))
        self.assertIsNone(metrics.classify_code("dM1ft"))   # ft on a method code
        self.assertIsNone(metrics.classify_code("Mx"))
        self.assertIsNone(metrics.classify_code(""))
        self.assertIsNone(metrics.classify_code("ft"))
        self.assertIsNone(metrics.classify_code(None))
        self.assertIsNone(metrics.classify_code(3))
        self.assertIsNone(metrics.classify_code("A1 FT"))


class LatencyReporting(unittest.TestCase):
    def _two_cases(self):
        return [by_id("algebra-correct"), by_id("trigonometry")]

    def test_missing_timings_are_counted_not_fabricated(self):
        cases = self._two_cases()
        predictions = [_prediction(cases[0]), _prediction(cases[1])]
        report = metrics.build_report(cases, predictions, metrics.load_governance())
        self.assertEqual(report["latency"]["observations"], 0)
        self.assertIsNone(report["latency"]["median_seconds"])
        self.assertIsNone(report["latency"]["p90_seconds"])
        self.assertEqual(report["latency"]["missing_or_invalid_on_completed"], 2)

    def test_small_sample_p90_is_suppressed(self):
        cases = self._two_cases()
        predictions = [_prediction(cases[0], seconds=3.0),
                       _prediction(cases[1], seconds=1.0)]
        report = metrics.build_report(cases, predictions, metrics.load_governance())
        self.assertEqual(report["latency"]["observations"], 2)
        self.assertEqual(report["latency"]["median_seconds"], 2.0)
        self.assertIsNone(report["latency"]["p90_seconds"])

    def test_p90_reported_when_sample_sufficient(self):
        cases = gold_cases()[:10]
        predictions = [_prediction(c, seconds=float(i + 1))
                       for i, c in enumerate(cases)]
        report = metrics.build_report(cases, predictions, metrics.load_governance())
        self.assertEqual(report["latency"]["observations"], 10)
        timings = sorted(float(i + 1) for i in range(10))
        self.assertEqual(report["latency"]["median_seconds"],
                         statistics.median(timings))
        self.assertEqual(report["latency"]["p90_seconds"], timings[8])


class InvalidLatencyConversions(unittest.TestCase):
    def _verify(self, prediction):
        case = by_id("algebra-correct")
        report = metrics.build_report([case], [prediction],
                                      _custom_governance([case["id"]]))
        self.assertEqual(
            report["prediction_status"]["schema_validation_failure"], 1)
        self.assertEqual(report["latency"]["observations"], 0)

    def test_string_seconds_is_schema_failure(self):
        prediction = _prediction(by_id("algebra-correct"))
        prediction["seconds"] = "fast"
        self._verify(prediction)

    def test_negative_seconds_is_schema_failure(self):
        prediction = _prediction(by_id("algebra-correct"))
        prediction["seconds"] = -1.0
        self._verify(prediction)


class LabelLeakage(unittest.TestCase):
    def _walk(self, node):
        if isinstance(node, dict):
            for key, value in node.items():
                yield key
                yield from self._walk(value)
        elif isinstance(node, list):
            for item in node:
                yield from self._walk(item)
        elif isinstance(node, str):
            yield node

    def test_report_never_contains_gold_derivations_or_prompts(self):
        cases = gold_cases()
        predictions = [_prediction(c) for c in cases]
        report = metrics.build_report(cases, predictions, metrics.load_governance())
        labels = [c.get("derivation") for c in cases]
        self.assertTrue(any(labels))     # the dataset really has derivations
        collected = list(self._walk(report))
        for label in labels:
            if label:
                self.assertNotIn(label, collected)
        for text in collected:
            self.assertNotIn("You are", text)
            self.assertNotIn("derivation", text)

    def test_gold_cases_are_not_mutated(self):
        cases = gold_cases()
        snapshot = copy.deepcopy(cases)
        metrics.build_report(cases, [_prediction(c) for c in cases],
                             metrics.load_governance())
        self.assertEqual(cases, snapshot)

    def test_module_has_no_network_or_sdk_imports(self):
        source = Path(metrics.__file__).read_text(encoding="utf-8")
        for forbidden in ("requests", "urllib", "socket", "http.client",
                          "generativeai", "google.generativeai", "httpx",
                          "websocket", "sqlite3", "subprocess"):
            self.assertNotIn(forbidden, source)


class BenchmarkPreservation(unittest.TestCase):
    def test_original_30_cases_preserved(self):
        cases = gold_cases()
        self.assertEqual([c["id"] for c in cases], [
            "algebra-correct", "algebra-partial", "algebra-invalid-method",
            "algebra-alternative", "geometry-area", "geometry-wrong",
            "trigonometry", "graphs-gradient", "statistics-grouping",
            "statistics-arithmetic", "rounding", "follow-through",
            "unreadable-final", "simultaneous-equations-ecf",
            "cosine-rule-premature-rounding", "cylinder-volume-exact-form",
            "probability-isw", "mean-via-median-method",
            "inequalities-conceptual-error", "trigonometry-alternative-method",
            "quadratics-dependent-method", "expanding-partial-multi-mark",
            "probability-tree-arithmetic-error", "geometry-area-crossed-out",
            "geometry-conflicting-methods", "percentages-correct-no-working",
            "indices-correct-no-working-demanded", "speed-ambiguous-handwriting",
            "grouped-mean-dependent-steps", "fractions-independent-b-marks"])
        self.assertEqual(sum(len(c["scheme"]) for c in cases), 69)
        self.assertEqual(sum(1 for c in cases for e in c["expected"] if e is None), 2)

    def test_report_stays_consistent_with_golden_dataset(self):
        cases = gold_cases()
        predictions = [_prediction(c, seconds=1.0) for c in cases]
        report = metrics.build_report(cases, predictions, metrics.load_governance())
        summary = report["criteria"]
        self.assertEqual(summary["known_reference_criteria"], 67)
        self.assertEqual(summary["gold_null_criteria"], 2)
        self.assertEqual(summary["fully_scorable_cases"], 28)


if __name__ == "__main__":
    unittest.main()
