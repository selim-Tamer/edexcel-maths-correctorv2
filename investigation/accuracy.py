"""Offline evaluation only; no provider imports or publication paths.

Adapted from user-supplied Pasted text(9).txt; criterion IDs and weights
come from Pasted text(8).txt. These synthetic gold labels require moderation.
Prefer awards=[{'id': criterion_id, 'awarded': bool}, ...], in any order.
Legacy boolean arrays remain supported in scheme order and are counted in
legacy_positional_completions; mixed formats are invalid.

Unknown/duplicate/malformed case IDs reject the batch with ValueError because
they cannot be assigned unambiguously. Invalid known-case predictions and
missing cases count as failures. All known gold criteria remain in accuracy
denominators, even for failures; false-award/deduction counts cover completions
only. Null gold excludes a whole case from both exact-agreement metrics, but
its known criteria still count. Weights affect total scores, not criterion counts.
seconds may be omitted or null; supplied values must be finite and nonnegative.
median_attempt_seconds retains its historical name but includes valid completions
with timing only. Identity checks are evaluator tests, not model accuracy.
"""
import argparse
import json
import statistics
import math
from pathlib import Path

def validate_golden_dataset(cases_list):
    if not isinstance(cases_list, list):
        raise ValueError('Expected gold case list')
    seen_case_ids = set()
    for case in cases_list:
        if not isinstance(case, dict):
            raise ValueError('Expected gold case object')
        cid = case.get('id')
        if not _valid_id(cid) or cid in seen_case_ids:
            raise ValueError(f"Duplicate or invalid case ID: {cid}")
        seen_case_ids.add(cid)

        if type(case.get('uncertain')) is not bool:
            raise ValueError(f"Case {cid} has invalid uncertain flag")

        if (not isinstance(case.get('scheme'), list) or not case['scheme']
                or not isinstance(case.get('expected'), list)):
            raise ValueError('Expected nonempty scheme and expected list')
        if len(case['scheme']) != len(case['expected']):
            raise ValueError(f"Case {cid} scheme/expected length mismatch")

        seen_crit_ids = set()
        for i, crit in enumerate(case['scheme']):
            if not isinstance(crit, dict):
                raise ValueError('Expected criterion object')
            crit_id = crit.get('id')
            if not _valid_id(crit_id) or crit_id in seen_crit_ids:
                raise ValueError(f"Case {cid} has invalid/duplicate criterion ID: {crit_id}")
            seen_crit_ids.add(crit_id)
            if not _valid_id(crit.get('code')):
                raise ValueError('Expected nonempty criterion code')

            weight = crit.get('weight')
            if type(weight) is not int or type(weight) is bool or weight <= 0:
                raise ValueError(f"Case {cid} criterion {crit_id} has invalid integer weight")

            exp = case['expected'][i]
            if type(exp) not in (bool, type(None)):
                raise ValueError(f"Case {cid} criterion {crit_id} expected value must be strictly bool or None")

def _valid_id(value):
    return type(value) is str and bool(value.strip())


def _valid_duration(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def cases():
    data = json.loads(Path(__file__).with_name('accuracy_cases.json').read_text(encoding='utf-8'))['cases']
    validate_golden_dataset(data)
    return data

def prompt_case(case):
    return {key:case[key] for key in ('id','topic','question','student','scheme')}

def evaluate(predictions, case_data=None):
    if not isinstance(predictions, list): raise ValueError('Expected prediction list')

    gold_cases = case_data if case_data is not None else cases()
    if case_data is not None:
        validate_golden_dataset(gold_cases)

    gold = {c['id']: c for c in gold_cases}
    seen = {}
    for p in predictions:
        if (not isinstance(p, dict) or not _valid_id(p.get('id'))
                or p['id'] not in gold or p['id'] in seen):
            raise ValueError('Unknown or duplicate fixture ID')
        seen[p['id']] = p

    totals = dict(cases=len(gold), completed=0, failures=0, scorable_cases=0,
                  exact_scores=0, exact_criterion_vectors=0, criteria=0, correct_criteria=0,
                  method_criteria=0, correct_method_criteria=0, false_awards=0,
                  incorrect_deductions=0, uncertainty_expected=0, uncertainty_detected=0,
                  unnecessary_uncertainty=0, legacy_positional_completions=0)

    # median_attempt_seconds explicitly includes only valid, completed predictions
    times = []

    for key, case in gold.items():
        scheme_map = {c['id']: c for c in case['scheme']}
        expected_awards = {}
        for i, expected_val in enumerate(case['expected']):
            if expected_val is not None:
                expected_awards[case['scheme'][i]['id']] = expected_val

        scorable = (len(expected_awards) == len(case['scheme']))
        totals['scorable_cases'] += scorable
        totals['criteria'] += len(expected_awards)
        totals['method_criteria'] += sum(c['code'].startswith('M') for c in scheme_map.values() if c['id'] in expected_awards)
        totals['uncertainty_expected'] += case['uncertain']

        p = seen.get(key, {})
        duration = p.get('seconds')

        if (type(p.get('failed', False)) is not bool or p.get('failed', False)
                or type(p.get('uncertain')) is not bool):
            totals['failures'] += 1; continue

        if duration is not None:
            if not _valid_duration(duration):
                totals['failures'] += 1; continue

        awards = p.get('awards')
        if not isinstance(awards, list):
            totals['failures'] += 1; continue
        legacy = bool(awards) and all(type(a) is bool for a in awards)
        if legacy:
            if len(awards) != len(case['scheme']):
                totals['failures'] += 1; continue
            awards = [{'id': c['id'], 'awarded': a}
                      for c, a in zip(case['scheme'], awards)]

        valid_awards = True
        for a in awards:
            if not isinstance(a, dict):
                valid_awards = False; break
            cid = a.get('id')
            # Validate criterion IDs as nonempty strings
            if type(cid) is not str or not cid.strip():
                valid_awards = False; break
            if type(a.get('awarded')) is not bool:
                valid_awards = False; break

        if not valid_awards:
            totals['failures'] += 1; continue

        predicted_map = {a['id']: a['awarded'] for a in awards}

        if len(predicted_map) != len(awards) or set(predicted_map.keys()) != set(scheme_map.keys()):
            totals['failures'] += 1; continue

        totals['completed'] += 1
        totals['legacy_positional_completions'] += legacy

        # Only append time metadata if the prediction successfully completes validation
        if duration is not None:
            times.append(duration)

        totals['uncertainty_detected'] += case['uncertain'] and p['uncertain']
        totals['unnecessary_uncertainty'] += not case['uncertain'] and p['uncertain']

        if scorable:
            gold_score = sum(scheme_map[cid]['weight'] for cid, awarded in expected_awards.items() if awarded)
            pred_score = sum(scheme_map[cid]['weight'] for cid, awarded in predicted_map.items() if awarded)
            totals['exact_scores'] += (gold_score == pred_score)
            totals['exact_criterion_vectors'] += (predicted_map == expected_awards)

        for cid, expected in expected_awards.items():
            pred = predicted_map[cid]
            match = (pred == expected)
            totals['correct_criteria'] += match
            totals['correct_method_criteria'] += match and scheme_map[cid]['code'].startswith('M')
            totals['false_awards'] += pred and not expected
            totals['incorrect_deductions'] += expected and not pred

    def ratio(n, d): return n / d if d else None
    return {**totals,
        'exact_score_agreement': ratio(totals['exact_scores'], totals['scorable_cases']),
        'question_level_criterion_accuracy': ratio(totals['exact_criterion_vectors'], totals['scorable_cases']),
        'method_mark_accuracy': ratio(totals['correct_method_criteria'], totals['method_criteria']),
        'criterion_accuracy': ratio(totals['correct_criteria'], totals['criteria']),
        'failure_rate': ratio(totals['failures'], totals['cases']),
        'median_attempt_seconds': statistics.median(times) if times else None,
        'latency_observations': len(times), 'teacher_review_required': True}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('predictions', type=Path)
    args = parser.parse_args()
    try:
        output = evaluate(json.loads(args.predictions.read_text(encoding='utf-8')))
    except Exception:
        parser.exit(2, 'Invalid offline prediction file; contents withheld.\n')
    print(json.dumps(output, sort_keys=True, indent=2))

if __name__ == '__main__': main()
