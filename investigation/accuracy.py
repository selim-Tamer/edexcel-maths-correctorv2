"""Offline evaluation only; no provider imports or publication paths."""
import argparse
import json
import statistics
from pathlib import Path


def cases():
    return json.loads(Path(__file__).with_name('accuracy_cases.json').read_text(encoding='utf-8'))['cases']


def prompt_case(case):
    # Gold labels and their derivations never enter model input.
    return {key:case[key] for key in ('id','topic','question','student','scheme')}


def evaluate(predictions):
    """One record per case. Missing/invalid attempts count as failures, not zeros.

    A prediction has id, awards (booleans in scheme order), uncertain (bool),
    seconds (finite nonnegative number) and optional failed=true. No raw answers
    are accepted or emitted. Coverage and failure counts accompany agreement.
    """
    import math
    if not isinstance(predictions,list): raise ValueError('Expected prediction list')
    gold = {c['id']:c for c in cases()}
    seen = {}
    for p in predictions:
        if not isinstance(p,dict) or p.get('id') not in gold or p['id'] in seen:
            raise ValueError('Unknown or duplicate fixture ID')
        seen[p['id']] = p
    totals = dict(cases=len(gold), completed=0, failures=0, scorable_cases=0,
                  exact_scores=0, exact_criterion_vectors=0, criteria=0, correct_criteria=0,
                  method_criteria=0, correct_method_criteria=0, false_awards=0,
                  incorrect_deductions=0, uncertainty_expected=0, uncertainty_detected=0,
                  unnecessary_uncertainty=0)
    times=[]
    for key, case in gold.items():
        known = [(i,v) for i,v in enumerate(case['expected']) if v is not None]
        scorable = len(known) == len(case['expected'])
        totals['scorable_cases'] += scorable
        totals['criteria'] += len(known)
        totals['method_criteria'] += sum(case['scheme'][i]['code'].startswith('M') for i,_ in known)
        totals['uncertainty_expected'] += case['uncertain']
        p = seen.get(key,{})
        duration=p.get('seconds')
        if type(duration) in (int,float) and math.isfinite(duration) and duration >= 0:
            times.append(duration)
        awards=p.get('awards')
        if (p.get('failed') or not isinstance(awards,list) or len(awards)!=len(case['expected'])
                or any(type(v) is not bool for v in awards) or type(p.get('uncertain')) is not bool
                or type(duration) not in (int,float) or not math.isfinite(duration) or duration < 0):
            totals['failures']+=1; continue
        totals['completed']+=1
        totals['uncertainty_detected'] += case['uncertain'] and p['uncertain']
        totals['unnecessary_uncertainty'] += not case['uncertain'] and p['uncertain']
        if scorable:
            totals['exact_scores'] += sum(awards)==sum(case['expected'])
            totals['exact_criterion_vectors'] += awards==case['expected']
        for i, expected in known:
            match=awards[i]==expected
            totals['correct_criteria'] += match
            totals['correct_method_criteria'] += match and case['scheme'][i]['code'].startswith('M')
            totals['false_awards'] += awards[i] and not expected
            totals['incorrect_deductions'] += expected and not awards[i]
    def ratio(n,d): return n/d if d else None
    return {**totals,
        'exact_score_agreement':ratio(totals['exact_scores'],totals['scorable_cases']),
        'question_level_criterion_accuracy':ratio(totals['exact_criterion_vectors'],totals['scorable_cases']),
        'method_mark_accuracy':ratio(totals['correct_method_criteria'],totals['method_criteria']),
        'criterion_accuracy':ratio(totals['correct_criteria'],totals['criteria']),
        'failure_rate':ratio(totals['failures'],totals['cases']),
        'median_attempt_seconds':statistics.median(times) if times else None,
        'latency_observations':len(times),'teacher_review_required':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('predictions',type=Path)
    args=parser.parse_args()
    try:
        output=evaluate(json.loads(args.predictions.read_text(encoding='utf-8')))
    except Exception:
        parser.exit(2,'Invalid offline prediction file; contents withheld.\n')
    print(json.dumps(output,sort_keys=True,indent=2))


if __name__=='__main__': main()
