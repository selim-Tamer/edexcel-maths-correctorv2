"""Phase B4 Stage 2A offline adapter tests: deterministic, network-free.

Live Gemini/external API calls are forbidden in this suite; all fixtures are
literal dicts. 'gold_labels' helpers read the benchmark ONLY inside tests to
construct hand-authored fixtures; the adapter under test must never need them
(golden-label isolation is asserted directly).
"""
import unittest

from investigation.accuracy import cases, evaluate
from investigation import marking_adapter as adapter
import inspect


def gold_restrictive(scheme_key="scheme"):
    """Return gold cases stripped of every label field an adapter must not read."""
    out = []
    for case in cases():
        out.append({key: value for key, value in case.items()
                    if key not in ("expected", "uncertain", "derivation")})
    return out


def by_id(case_data):
    return {c["id"]: c for c in case_data}


def full_gold_by_id():
    return by_id(cases())


def gold_and_unreadable_expected(case_id):
    """Expected award list for one case (TEST scope only; the adapter never sees it)."""
    return by_id(cases())[case_id]["expected"]


def rows(case_id, values, codes=None):
    """Build a breakdown for one case from hand-picked awarded values."""
    gold = by_id(cases())   # dict here by design; tests may read labels, adapters may not
    return [{"criterion_id": c["id"], "awarded": values[i],
             "code": gold[case_id]["scheme"][i]["code"]}
            for i, c in enumerate(gold[case_id]["scheme"])]


def response_for(case_id, values, uncertain=False, codes=True):
    payload = {"id": case_id, "uncertain": uncertain,
               "breakdown": rows(case_id, values, codes)}
    return payload


def to_failed(gold, payload, expected_reason):
    """Run one failure-expecting response through adapter + evaluator."""
    preds = adapter.predictions_from_responses([payload], gold)
    assert len(preds) == 1
    pred = preds[0]
    assert pred["failed"], f"expected failure: {pred}"
    assert pred["failure_reason"] == expected_reason
    assert "awards" not in pred       # never fabricate awards
    # evaluator must count it as a genuine failure, not a zero-score completion
    single = [c for c in cases() if c["id"] == payload["id"]]
    r = evaluate(preds, case_data=single)
    assert (r["completed"], r["failures"], r["failure_rate"]) == (0, 1, 1.0)
    return pred


class AdapterOracleBehaviour(unittest.TestCase):
    def test_perfect_oracle_agrees_with_evaluator(self):
        gold = cases()
        payloads = [
            response_for("algebra-correct", [True, True]),
            response_for("follow-through", [True, False, True]),
            response_for("simultaneous-equations-ecf", [True, False, True, True]),
            response_for("graphs-gradient", [True, True, True]),
        ]
        preds = adapter.predictions_from_responses(payloads, gold)
        subset = [c for c in cases() if c["id"] in {p["id"] for p in preds}]
        r = evaluate(preds, case_data=subset)
        self.assertEqual((r["completed"], r["failures"]), (4, 0))
        self.assertEqual(r["exact_scores"], 4)
        self.assertEqual(r["exact_criterion_vectors"], 4)
        self.assertEqual(r["false_awards"], 0)
        self.assertEqual(r["incorrect_deductions"], 0)
        # evaluator semantics unchanged: predictions for the remaining golden
        # cases count as missing -> explicit failures across the full dataset
        full = evaluate(preds, case_data=gold)
        self.assertEqual(full["failures"], 26)

    def test_completely_incorrect_prediction(self):
        gold = cases()
        payload = response_for("algebra-correct", [False, False])
        preds = adapter.predictions_from_responses([payload], gold)
        r = evaluate(preds, case_data=[case for case in cases() if case["id"] == "algebra-correct"])
        self.assertEqual((r["completed"], r["exact_scores"]), (1, 0))
        self.assertEqual(r["incorrect_deductions"], 2)
        self.assertEqual(r["false_awards"], 0)

    def test_partially_correct_prediction_is_segmented(self):
        gold = cases()
        # deliberately flipped middle award: awarded-mark evidence disagrees with evaluator oracle
        payload = response_for("simultaneous-equations-ecf", [True, True, True, True])
        preds = adapter.predictions_from_responses([payload], gold)
        r = evaluate(preds, case_data=[c for c in cases() if c["id"] == "simultaneous-equations-ecf"])
        self.assertEqual(r["false_awards"], 1)     # crit-sim-2 is False in gold
        self.assertEqual(r["exact_scores"], 0)
        self.assertEqual(r["exact_criterion_vectors"], 0)

    def test_repeated_identical_mark_codes_stay_distinct(self):
        # crit-exp-1 and crit-exp-2 BOTH have code "M1"; a code-based alignment
        # would confuse them, while ID-based alignment must not.
        gold = cases()
        subset = [c for c in cases() if c["id"] == "expanding-partial-multi-mark"]
        aligned = {"id": "expanding-partial-multi-mark", "uncertain": False, "breakdown": [
            {"criterion_id": "crit-exp-1", "awarded": True, "code": "M1"},
            {"criterion_id": "crit-exp-2", "awarded": False, "code": "M1"},
            {"criterion_id": "crit-exp-3", "awarded": False, "code": "A1"},
        ]}
        crossings = {"id": "expanding-partial-multi-mark", "uncertain": False, "breakdown": [
            {"criterion_id": "crit-exp-1", "awarded": False, "code": "M1"},
            {"criterion_id": "crit-exp-2", "awarded": True, "code": "M1"},
            {"criterion_id": "crit-exp-3", "awarded": False, "code": "A1"},
        ]}
        # direction 1: matches the gold distinction exactly -> exact vector
        preds = adapter.predictions_from_responses([aligned], gold)
        r = evaluate(preds, case_data=subset)
        self.assertEqual(r["exact_criterion_vectors"], 1)
        self.assertEqual((r["false_awards"], r["incorrect_deductions"]), (0, 0))
        # direction 2: same score, crossed awards -> caught per criterion only
        preds = adapter.predictions_from_responses([crossings], gold)
        r = evaluate(preds, case_data=subset)
        self.assertEqual(r["exact_scores"], 1)          # totals are deceptively equal (1=1)
        self.assertEqual(r["exact_criterion_vectors"], 0)  # but the ID-mapping is wrong
        self.assertEqual((r["false_awards"], r["incorrect_deductions"]), (1, 1))

    def test_reordered_breakdown_rows_do_not_change_alignment(self):
        gold = cases()
        forward = response_for("trigonometry-alternative-method", [True, True])
        reverse = response_for("trigonometry-alternative-method", [True, True])
        reverse["breakdown"] = list(reversed(reverse["breakdown"]))
        subset = [c for c in cases() if c["id"] == "trigonometry-alternative-method"]
        r1 = evaluate(adapter.predictions_from_responses([forward], gold), case_data=subset)
        # reversed rows are submitted as a separate batch: duplicate case
        # responses in one batch are correctly rejected by the adapter
        r2 = evaluate(adapter.predictions_from_responses([reverse], gold), case_data=subset)
        self.assertEqual((r1["exact_scores"], r2["exact_scores"]), (1, 1))
        self.assertEqual((r1["false_awards"], r2["false_awards"]), (0, 0))
        self.assertEqual(r1["exact_criterion_vectors"], r2["exact_criterion_vectors"])


class AdapterValidationFailures(unittest.TestCase):

    def test_missing_criterion_id_converts_to_failure(self):
        gold = cases()
        payload = response_for("algebra-correct", [True, True])
        payload["breakdown"] = payload["breakdown"][:1]  # dropped crit-alg-c-2
        to_failed(gold, payload, "breakdown_count_mismatch")

    def test_duplicate_criterion_id_converts_to_failure(self):
        gold = cases()
        payload = response_for("algebra-correct", [True, True])
        payload["breakdown"][1] = dict(payload["breakdown"][0])
        to_failed(gold, payload, "duplicate_criterion_id")

    def test_unknown_criterion_id_converts_to_failure(self):
        gold = cases()
        payload = response_for("algebra-correct", [True, True])
        payload["breakdown"][1]["criterion_id"] = "crit-does-not-exist"
        to_failed(gold, payload, "unknown_criterion_id")

    def test_non_boolean_award_values_rejected(self):
        for bad in (0, 1, "true", "false", None):
            with self.subTest(bad=bad):
                gold = cases()
                payload = response_for("algebra-correct", [True, None])
                payload["breakdown"][1]["awarded"] = bad
                to_failed(gold, payload, "non_boolean_award")

    def test_missing_award_field_never_substituted_with_false(self):
        gold = cases()
        payload = response_for("algebra-correct", [True, True])
        del payload["breakdown"][1]["awarded"]
        pred = to_failed(gold, payload, "missing_award_field")
        self.assertIsNone(pred.get("awards"))

    def test_mismatched_code_rejected(self):
        gold = cases()
        payload = response_for("algebra-correct", [True, True])
        payload["breakdown"][1]["code"] = "B1"  # scheme says A1
        to_failed(gold, payload, "code_mismatch")

    def test_mismatched_weight_rejected(self):
        gold = cases()
        payload = response_for("graphs-gradient", [True, True, True])
        payload["breakdown"][0]["weight"] = 5  # scheme says 1
        to_failed(gold, payload, "weight_mismatch")

    def test_excess_breakdown_rows_rejected(self):
        gold = cases()
        payload = response_for("algebra-correct", [True, True])
        payload["breakdown"].append({"criterion_id": "crit-alg-c-2", "awarded": True})
        to_failed(gold, payload, "breakdown_count_mismatch")

    def test_missing_breakdown_converts_to_failure(self):
        gold = cases()
        payload = {"id": "algebra-correct", "uncertain": False}
        to_failed(gold, payload, "missing_breakdown")

    def test_explicit_failure_skips_breakdown(self):
        gold = cases()
        payload = {"id": "algebra-correct", "uncertain": True, "failed": True}
        pred = to_failed(gold, payload, "explicit_failure")
        self.assertTrue(pred["uncertain"])

    def test_batch_errors_raise_instead_of_fabricating(self):
        gold = cases()
        with self.assertRaises(ValueError):
            adapter.predictions_from_responses([{"id": "no-such-case", "uncertain": False}], gold)
        with self.assertRaises(ValueError):
            adapter.predictions_from_responses([response_for("algebra-correct", [True, True])] * 2, gold)
        with self.assertRaises(ValueError):
            adapter.predictions_from_responses(["not-a-dict"], gold)


class AdapterMarkCodeSemantics(unittest.TestCase):
    def test_m1_a1_b1_dM1_a1ft_distinguishations(self):
        gold = cases()
        payloads = [
            response_for("graphs-gradient", [True, True, True]),         # M1, A1, B1
            response_for("follow-through", [True, False, True]),         # M1, A1, A1ft
            response_for("quadratics-dependent-method", [False, False, True]),  # M1, dM1, A1
        ]
        preds = adapter.predictions_from_responses(payloads, gold)
        subset = {c["id"]: c for c in cases()}
        case_data = [subset[p["id"]] for p in preds]
        r = evaluate(preds, case_data=case_data)
        self.assertEqual((r["completed"], r["failures"]), (3, 0))
        # gold: graphs 3/3 (exact); follow-through [T,F,T] matches gold exactly;
        # quadratics [F,F,T] gives crit-quad-3 a false award (gold is [F,F,F])
        self.assertEqual(r["exact_scores"], 2)
        self.assertEqual(r["exact_criterion_vectors"], 2)
        self.assertEqual(r["false_awards"], 1)
        self.assertEqual(r["incorrect_deductions"], 0)
        # code classes must travel through the adapter identically:
        for pred, payload in zip(preds, payloads):
            case = subset[pred["id"]]
            gold_codes = [cr["code"] for cr in case["scheme"]]
            pred_codes = [next(row["code"] for row in payload["breakdown"]
                               if row["criterion_id"] == cr["id"])
                          for cr in case["scheme"]]
            self.assertEqual(gold_codes, pred_codes)

    def test_dependent_and_ecf_criteria_matched_by_id(self):
        gold = cases()
        payloads = [
            response_for("grouped-mean-dependent-steps", [True, False, False]),  # dM1 withheld
            response_for("simultaneous-equations-ecf", [True, False, True, True]),  # ECF A1ft
        ]
        preds = adapter.predictions_from_responses(payloads, gold)
        subset = {c["id"]: c for c in cases()}
        case_data = [subset[p["id"]] for p in preds]
        r = evaluate(preds, case_data=case_data)
        self.assertEqual(r["exact_criterion_vectors"], 2)
        self.assertEqual(r["exact_scores"], 2)
        self.assertEqual(r["failures"], 0)


class AdapterUncertaintyBehaviour(unittest.TestCase):
    def test_unreadable_answers_declared_unknown_not_invented(self):
        gold = cases()
        payloads = [
            {"id": "unreadable-final", "uncertain": True, "breakdown": [
                {"criterion_id": "crit-unr-1", "awarded": True, "code": "M1"},
                {"criterion_id": "crit-unr-2", "awarded": False, "code": "A1", "unknown": True},
            ]},
            {"id": "speed-ambiguous-handwriting", "uncertain": True, "breakdown": [
                {"criterion_id": "crit-spd-1", "awarded": True, "code": "M1"},
                {"criterion_id": "crit-spd-2", "awarded": False, "code": "A1", "unknown": True},
            ]},
        ]
        preds = adapter.predictions_from_responses(payloads, gold)
        self.assertEqual(preds[0]["declared_unknown"], ["crit-unr-2"])
        self.assertEqual(preds[1]["declared_unknown"], ["crit-spd-2"])
        r = evaluate(preds, case_data=cases())
        # both golden cases are gold-uncertain (1+1); adapter refusal maps 1:1
        self.assertEqual(r["uncertainty_expected"], 2)
        self.assertEqual(r["uncertainty_detected"], 2)
        # unresolved criteria scores are excluded from the denominators
        self.assertEqual(r["criteria"], 67)
        self.assertEqual(r["scorable_cases"], 28)

    def test_unknown_row_without_uncertainty_declared_is_failure(self):
        gold = cases()
        payload = response_for("algebra-correct", [True, True])
        payload["breakdown"][0]["unknown"] = True  # uncertainty flag NOT set
        to_failed(gold, payload, "uncertainty_not_declared")

    def test_uncertainty_signal_without_unknown_rows_is_kept(self):
        gold = cases()
        payload = {"id": "algebra-correct", "uncertain": True, "breakdown": rows("algebra-correct", [True, True])}
        preds = adapter.predictions_from_responses([payload], gold)
        single = [c for c in cases() if c["id"] == "algebra-correct"]
        r = evaluate(preds, case_data=single)
        self.assertEqual((r["completed"], r["failures"]), (1, 0))
        self.assertEqual(r["uncertainty_expected"], 0)
        self.assertEqual(r["uncertainty_detected"], 0)
        self.assertEqual(r["unnecessary_uncertainty"], 1)  # evaluator surfaces over-flagging


class AdapterGoldenLabelIsolation(unittest.TestCase):
    def test_adapter_reads_no_label_fields(self):
        restricted = gold_restrictive()
        payload = response_for("algebra-correct", [True, True])
        preds = adapter.predictions_from_responses([payload], restricted)
        self.assertEqual(preds[0]["awards"], [
            {"id": "crit-alg-c-1", "awarded": True},
            {"id": "crit-alg-c-2", "awarded": True},
        ])

    def test_full_dataset_adapter_parity_without_label_fields(self):
        """Same oracle responses -> identical predictions whether labels removed or not.

        The responses themselves are built from labels in TEST scope only; the
        adapter must operate identically on gold cases whose expected/uncertain/
        derivation fields were deleted.
        """
        gold = full_gold_by_id()
        payloads = [
            {"id": case_id, "uncertain": True if case_id in ("unreadable-final",
                                                             "speed-ambiguous-handwriting") else False,
             "breakdown": [{"criterion_id": scheme["id"],
                            "awarded": expected_value if expected_value is not None else False,
                            "code": scheme["code"]}
                           for scheme, expected_value in zip(gold[case_id]["scheme"],
                                                             gold_and_unreadable_expected(case_id))]}
            for case_id in gold
        ]
        restricted = gold_restrictive()
        preds_restricted = adapter.predictions_from_responses(payloads, restricted)
        preds_full = adapter.predictions_from_responses(payloads, cases())
        self.assertEqual(preds_restricted, preds_full)
        r = evaluate(preds_restricted, case_data=cases())
        self.assertEqual((r["cases"], r["completed"], r["failures"]), (30, 30, 0))
        self.assertEqual(r["exact_scores"], 28)
        self.assertEqual(r["criteria"], 67)

    def test_adapter_module_has_no_label_references(self):
        import ast
        source = inspect.getsource(adapter)
        tree = ast.parse(source)
        docstring = ast.get_docstring(tree) or ""
        code_only = source.replace(docstring, "", 1)
        for label in ('"expected"', "'expected'", '"derivation"', "'derivation'",
                      '.get("expected"', ".get('expected'", "['expected']", '[expected]'):
            self.assertNotIn(label, code_only, label)
        # reading case-level uncertain is also forbidden: the adapter must
        # derive uncertainty purely from the model response, so restricted
        # gold cases (no expected/uncertain/derivation keys) behave identically.

    def test_seconds_propagate_for_latency(self):
        gold = cases()
        payload = response_for("algebra-correct", [True, True])
        payload["seconds"] = 2
        preds = adapter.predictions_from_responses([payload], gold)
        single = [c for c in cases() if c["id"] == "algebra-correct"]
        r = evaluate(preds, case_data=single)
        self.assertEqual(r["latency_observations"], 1)
        self.assertEqual(r["median_attempt_seconds"], 2)

    def test_failed_prediction_declines_latency(self):
        gold = cases()
        payload = {"id": "algebra-correct", "uncertain": True, "failed": True, "seconds": 2}
        preds = adapter.predictions_from_responses([payload], gold)
        single = [c for c in cases() if c["id"] == "algebra-correct"]
        r = evaluate(preds, case_data=single)
        self.assertEqual((r["completed"], r["latency_observations"]), (0, 0))

    def test_schema_constant_documents_evaluation_contract(self):
        self.assertIn("criterion_id", adapter.EVALUATION_RESPONSE_SCHEMA["properties"]["breakdown"]["items"]["properties"])
        self.assertEqual(
            adapter.EVALUATION_RESPONSE_SCHEMA["properties"]["breakdown"]["items"]["required"],
            ["criterion_id", "awarded"],
        )


if __name__ == "__main__":
    unittest.main()
