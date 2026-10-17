import unittest
from investigation.accuracy import evaluate

class TestOfflineAccuracyEvaluator(unittest.TestCase):
    def setUp(self):
        self.mock_cases = [
            {
                "id": "weighted-case", "topic": "test", "question": "test", "student": "test",
                "scheme": [
                    {"id": "c1", "code": "M1", "weight": 1, "criterion": "M1"},
                    {"id": "c2", "code": "M2", "weight": 2, "criterion": "M2"},
                    {"id": "c3", "code": "A1ft", "weight": 1, "criterion": "A1ft"},
                    {"id": "c4", "code": "A2", "weight": 2, "criterion": "A2"},
                    {"id": "c5", "code": "B1", "weight": 1, "criterion": "B1"},
                    {"id": "c6", "code": "B2", "weight": 2, "criterion": "B2"}
                ],
                "expected": [True, True, False, True, False, True], "uncertain": False
            },
            {
                "id": "null-case", "topic": "test", "question": "test", "student": "test",
                "scheme": [
                    {"id": "n1", "code": "M1", "weight": 1, "criterion": "M1"},
                    {"id": "n2", "code": "A1", "weight": 1, "criterion": "A1"}
                ],
                "expected": [True, None], "uncertain": True
            }
        ]

    def test_integer_expected_awards_raises_error(self):
        invalid_case = [{
            "id": "int-case", "topic": "test", "question": "test", "student": "test",
            "scheme": [{"id": "c1", "code": "M1", "weight": 1, "criterion": "M1"}],
            "expected": [1], "uncertain": False  # 1 instead of True
        }]
        preds = [{"id": "int-case", "uncertain": False, "awards": [{"id": "c1", "awarded": True}]}]
        with self.assertRaisesRegex(ValueError, "strictly bool or None"):
            evaluate(preds, case_data=invalid_case)

    def test_invalid_criterion_weights_raises_error(self):
        invalid_case = [{
            "id": "weight-case", "topic": "test", "question": "test", "student": "test",
            "scheme": [{"id": "c1", "code": "M1", "weight": 0, "criterion": "M1"}],
            "expected": [True], "uncertain": False
        }]
        preds = [{"id": "weight-case", "uncertain": False, "awards": [{"id": "c1", "awarded": True}]}]
        with self.assertRaisesRegex(ValueError, "invalid integer weight"):
            evaluate(preds, case_data=invalid_case)

    def test_unhashable_criterion_ids_increments_failure(self):
        # Prediction provides a dict ID (unhashable)
        preds = [{"id": "weighted-case", "uncertain": False, "seconds": 1.0,
                  "awards": [{"id": {"bad": "id"}, "awarded": True}]}]
        res = evaluate(preds, case_data=[self.mock_cases[0]])
        self.assertEqual(res["failures"], 1)

    def test_empty_string_criterion_ids_increments_failure(self):
        # Prediction provides an empty string ID
        preds = [{"id": "weighted-case", "uncertain": False, "seconds": 1.0,
                  "awards": [{"id": "   ", "awarded": True}]}]
        res = evaluate(preds, case_data=[self.mock_cases[0]])
        self.assertEqual(res["failures"], 1)

    def test_median_attempt_seconds_excludes_failed(self):
        preds = [
            {"id": "weighted-case", "uncertain": False, "seconds": 10.0, "awards": [
                {"id": "c1", "awarded": True}, {"id": "c2", "awarded": True},
                {"id": "c3", "awarded": False}, {"id": "c4", "awarded": True},
                {"id": "c5", "awarded": False}, {"id": "c6", "awarded": True}
            ]},
            # Fails due to missing prediction arrays
            {"id": "null-case", "uncertain": True, "seconds": 100.0, "awards": [{"id": "n1", "awarded": True}]}
        ]
        res = evaluate(preds, case_data=self.mock_cases)
        self.assertEqual(res["latency_observations"], 1)
        self.assertEqual(res["median_attempt_seconds"], 10.0)

    def test_weighted_disagreement(self):
        cases = [{"id": "diff-weight", "scheme": [
            {"id": "m2", "code": "M2", "weight": 2, "criterion": "M2"},
            {"id": "a1", "code": "A1", "weight": 1, "criterion": "A1"}],
            "expected": [True, False], "uncertain": False}]

        preds = [{"id": "diff-weight", "uncertain": False, "awards": [{"id": "m2", "awarded": False}, {"id": "a1", "awarded": True}]}]
        res = evaluate(preds, case_data=cases)
        self.assertEqual(res["exact_scores"], 0)
        self.assertEqual(res["exact_criterion_vectors"], 0)

    def test_null_expected_handling(self):
        preds = [{"id": "null-case", "uncertain": True, "awards": [{"id": "n1", "awarded": True}, {"id": "n2", "awarded": True}]}]
        res = evaluate(preds, case_data=[self.mock_cases[1]])
        self.assertEqual(res["scorable_cases"], 0)
        self.assertIsNone(res["exact_score_agreement"])
        self.assertEqual(res["criteria"], 1)

    def test_missing_and_extra_predictions(self):
        preds_missing = [{"id": "weighted-case", "uncertain": False, "awards": [{"id": "c1", "awarded": True}]}]
        preds_extra = [{"id": "weighted-case", "uncertain": False, "awards": [
            {"id": "c1", "awarded": True}, {"id": "c2", "awarded": True}, {"id": "c3", "awarded": False},
            {"id": "c4", "awarded": True}, {"id": "c5", "awarded": False}, {"id": "c6", "awarded": True}, {"id": "c7", "awarded": True}]}]

        self.assertEqual(evaluate(preds_missing, case_data=[self.mock_cases[0]])["failures"], 1)
        self.assertEqual(evaluate(preds_extra, case_data=[self.mock_cases[0]])["failures"], 1)

    def test_reordered_criteria(self):
        preds = [{"id": "weighted-case", "uncertain": False, "awards": [
            {"id": "c6", "awarded": True}, {"id": "c5", "awarded": False}, {"id": "c4", "awarded": True},
            {"id": "c3", "awarded": False}, {"id": "c2", "awarded": True}, {"id": "c1", "awarded": True}]}]
        res = evaluate(preds, case_data=[self.mock_cases[0]])
        self.assertEqual(res["exact_scores"], 1)

    def test_optional_seconds_field(self):
        preds_no_seconds = [{"id": "weighted-case", "uncertain": False, "awards": [
            {"id": "c1", "awarded": True}, {"id": "c2", "awarded": True}, {"id": "c3", "awarded": False},
            {"id": "c4", "awarded": True}, {"id": "c5", "awarded": False}, {"id": "c6", "awarded": True}]}]
        res = evaluate(preds_no_seconds, case_data=[self.mock_cases[0]])
        self.assertEqual(res["failures"], 0)
        self.assertIsNone(res["median_attempt_seconds"])



class AdditionalRegressionTests(unittest.TestCase):
    def fixture(self, code="M2", weight=2):
        return [{"id": "case", "scheme": [
            {"id": "a", "code": code, "weight": weight},
            {"id": "b", "code": "A1", "weight": 1}],
            "expected": [True, False], "uncertain": False}]

    def prediction(self, awards=None):
        return {"id": "case", "uncertain": False, "awards": awards if awards is not None else [
            {"id": "a", "awarded": True}, {"id": "b", "awarded": False}]}

    def test_weighted_m2_b2_and_suffixes(self):
        for code in ("M2", "B2", "M2ft", "B2 cao"):
            with self.subTest(code=code):
                result = evaluate([self.prediction([
                    {"id": "a", "awarded": False}, {"id": "b", "awarded": True}])], self.fixture(code))
                self.assertEqual(result['exact_scores'], 0)
                self.assertEqual(result['exact_criterion_vectors'], 0)

    def test_same_score_wrong_awards(self):
        result = evaluate([self.prediction([
            {"id": "b", "awarded": True}, {"id": "a", "awarded": False}])], self.fixture(weight=1))
        self.assertEqual(result['exact_scores'], 1)
        self.assertEqual(result['exact_criterion_vectors'], 0)
        self.assertEqual(result['false_awards'], 1)
        self.assertEqual(result['incorrect_deductions'], 1)

    def test_invalid_gold_shapes(self):
        for gold in ({}, [None], [1], [{"id": "x"}], [{"id": [], "uncertain": False}]):
            with self.subTest(gold=gold), self.assertRaises(ValueError):
                evaluate([], gold)

    def test_invalid_gold_values(self):
        for field, values in (("weight", (True, False, 0, -1, 1.5, "2", None)),
                              ("id", ([], {}, "", "  ", None)),
                              ("code", (None, [], ""))):
            for value in values:
                gold = self.fixture(); gold[0]['scheme'][0][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    evaluate([], gold)
        for value in (0, 1, "true", [], {}):
            gold = self.fixture(); gold[0]['expected'][0] = value
            with self.subTest(expected=value), self.assertRaises(ValueError):
                evaluate([], gold)

    def test_duplicate_gold_ids_and_length(self):
        gold = self.fixture()
        with self.assertRaises(ValueError): evaluate([], gold + gold)
        gold[0]['scheme'][1]['id'] = 'a'
        with self.assertRaises(ValueError): evaluate([], gold)
        gold = self.fixture(); gold[0]['expected'].pop()
        with self.assertRaises(ValueError): evaluate([], gold)

    def test_invalid_prediction_case_ids(self):
        for value in ([], {}, "", "  ", None, 1, "unknown"):
            p = self.prediction(); p['id'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                evaluate([p], self.fixture())
        with self.assertRaises(ValueError): evaluate([self.prediction()] * 2, self.fixture())
        for value in (None, {}, [None], [1]):
            with self.subTest(value=value), self.assertRaises(ValueError): evaluate(value, self.fixture())

    def test_malformed_awards(self):
        variants = [[], [True, {"id": "b", "awarded": False}], [1, 0],
                    [{"id": "a", "awarded": True}] * 2]
        for value in ([], {}, "", "  ", None, 1, "unknown"):
            variants.append([{"id": value, "awarded": True}, {"id": "b", "awarded": False}])
        for value in (None, 1, 0, "true", [], {}):
            variants.append([{"id": "a", "awarded": value}, {"id": "b", "awarded": False}])
        for awards in variants:
            with self.subTest(awards=awards):
                self.assertEqual(evaluate([self.prediction(awards)], self.fixture())['failures'], 1)

    def test_denominators_include_missing_and_invalid(self):
        gold = self.fixture()
        import copy
        other = copy.deepcopy(gold[0]); other['id'] = 'missing'; gold.append(other)
        r = evaluate([self.prediction()], gold)
        self.assertEqual((r['cases'], r['completed'], r['failures']), (2, 1, 1))
        self.assertEqual((r['scorable_cases'], r['criteria'], r['method_criteria']), (2, 4, 2))
        for key in ('exact_score_agreement', 'question_level_criterion_accuracy',
                    'criterion_accuracy', 'method_mark_accuracy', 'failure_rate'):
            self.assertEqual(r[key], 0.5)
        self.assertEqual(evaluate([], gold)['failures'], 2)

    def test_optional_and_invalid_timing(self):
        for value in (None, 0, 1.5):
            p = self.prediction(); p['seconds'] = value
            r = evaluate([p], self.fixture())
            self.assertEqual(r['completed'], 1)
            self.assertEqual(r['latency_observations'], int(value is not None))
        for value in (True, False, -1, "1", [], {}, float('nan'), float('inf'), 10**400):
            p = self.prediction(); p['seconds'] = value
            with self.subTest(value=value):
                r = evaluate([p], self.fixture())
                self.assertEqual(r['failures'], 1)
                self.assertEqual(r['latency_observations'], 0)

    def test_invalid_flags(self):
        for field in ('uncertain', 'failed'):
            for value in (None, 0, 1, "false", [], {}):
                p = self.prediction(); p[field] = value
                with self.subTest(field=field, value=value):
                    self.assertEqual(evaluate([p], self.fixture())['failures'], 1)

    def test_legacy_compatibility_is_reported(self):
        r = evaluate([self.prediction([True, False])], self.fixture())
        self.assertEqual(r['exact_scores'], 1)
        self.assertEqual(r['legacy_positional_completions'], 1)

    def test_gold_identity_reordered_and_prompt_excludes_labels(self):
        from investigation.accuracy import cases, prompt_case
        gold = cases()
        self.assertEqual(len(gold), 13)
        predictions = []
        for case in reversed(gold):
            self.assertEqual(set(prompt_case(case)), {'id', 'topic', 'question', 'student', 'scheme'})
            predictions.append({'id': case['id'], 'uncertain': case['uncertain'], 'awards': list(reversed([
                {'id': c['id'], 'awarded': v if v is not None else False}
                for c, v in zip(case['scheme'], case['expected'])]))})
        r = evaluate(predictions)
        self.assertEqual((r['completed'], r['scorable_cases'], r['criteria']), (13, 12, 27))
        self.assertEqual(r['exact_score_agreement'], 1)
        self.assertEqual(r['question_level_criterion_accuracy'], 1)
        self.assertEqual(r['criterion_accuracy'], 1)
        self.assertEqual(r['uncertainty_detected'], 1)
        self.assertEqual(r['legacy_positional_completions'], 0)

    def test_empty_dataset(self):
        r = evaluate([], [])
        self.assertEqual(r['cases'], 0)
        self.assertIsNone(r['failure_rate'])
        self.assertIsNone(r['criterion_accuracy'])


if __name__ == '__main__':
    unittest.main()
