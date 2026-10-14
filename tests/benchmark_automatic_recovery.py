"""PR13/current controller with identical virtual timing and fixed jitter."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from support import fake_client
from pipeline_fakes import Clock, ApiError
import marking_requests as current


def run(module, scenario):
    clock = Clock(); client = fake_client(); requests = []
    def reply(**kw):
        requests.append(kw['model'])
        if scenario == 'all_503' or (scenario in ('third_recovers', 'next_student') and len(requests) <= (3 if scenario == 'next_student' else 2)):
            clock.sleep(1); raise ApiError(503)
        if scenario == 'connection' and len(requests) == 1:
            clock.sleep(1); raise ConnectionRefusedError('synthetic')
        clock.sleep(60)
        return SimpleNamespace(text='{"questions":[{"awarded_marks":1,"max_marks":2}]}')
    client.models.generate_content.side_effect = reply
    outcomes = []
    for _ in range(2 if scenario == 'next_student' else 1):
        try:
            module.generate(client, ['gemini-test-a','gemini-test-b','gemini-test-c'],
                ['synthetic'], 'system', {}, lambda **kw: kw, json.loads, clock=clock)
            outcomes.append('completed')
        except module.MarkingFailure:
            outcomes.append('failed_without_marks')
    return {'outcomes': outcomes, 'requests': len(requests), 'simulated_seconds': clock.seconds}


def benchmark():
    spec = importlib.util.spec_from_file_location('pr13', Path(__file__).parent/'fixtures/pre_pr14_marking_requests.py')
    old = importlib.util.module_from_spec(spec); spec.loader.exec_module(old)
    with patch.object(current.random, 'uniform', return_value=0.5), patch.object(current, 'configured_attempts', return_value=3):
        return {'live_requests': 0, 'note': 'Scripted service, 60s success / 1s failure, fixed 0.5s jitter. Not a live success rate.',
                'cases': {case: {'before': run(old, case), 'after': run(current, case)}
                          for case in ('success','third_recovers','all_503','connection','next_student')}}


if __name__ == '__main__':
    print(json.dumps(benchmark(), indent=2))
