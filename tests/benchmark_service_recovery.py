"""PR12 versus current controller; scripted synthetic outcomes, NOT live rates."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from support import fake_client
from pipeline_fakes import ApiError, Clock
import marking_requests


def run(module, case):
    clock = Clock()
    client = fake_client()
    count = 0
    def respond(**kwargs):
        nonlocal count
        count += 1
        if case == 'success':
            clock.sleep(60)
        elif case == 'permanent_503':
            clock.sleep(1)
            raise ApiError(503)
        elif case == 'recovers_after_5s':
            if clock.monotonic() < 5:
                clock.sleep(1)
                raise ApiError(503)
            clock.sleep(60)
        elif case == 'healthy_fallback':
            if count == 1:
                clock.sleep(1)
                raise ApiError(503)
            clock.sleep(60)
        elif case == 'retry_after_12s':
            if clock.monotonic() < 12:
                clock.sleep(1)
                raise ApiError(503, '12')
            clock.sleep(60)
        elif case == 'retry_after_120s':
            clock.sleep(1)
            raise ApiError(503, '120')
        return SimpleNamespace(text='{"questions": [{"awarded_marks": 1, "max_marks": 2}]}')
    client.models.generate_content.side_effect = respond
    outcome = 'completed'
    try:
        module.generate(client, ['gemini-test-a', 'gemini-test-b'], ['synthetic'], 'system', {},
            lambda **kw: kw, json.loads, clock=clock)
    except module.MarkingFailure:
        outcome = 'failed_without_marks'
    record = module.state_for(client)['last']
    return dict(outcome=outcome, requests=count, simulated_seconds=clock.seconds,
                sleep_seconds=record['sleep_seconds'])


def benchmark():
    path = Path(__file__).parent / 'fixtures/pre_pr13_marking_requests.py'
    spec = importlib.util.spec_from_file_location('pr12_controller', path)
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    cases = {name: {'before': run(baseline, name), 'after': run(marking_requests, name)}
             for name in ('success', 'healthy_fallback', 'permanent_503', 'recovers_after_5s',
                          'retry_after_12s', 'retry_after_120s')}
    return {'live_requests': 0, 'note': 'Scripted recovery windows, not a production probability estimate.',
            'cases': cases}


if __name__ == '__main__':
    print(json.dumps(benchmark(), indent=2))
