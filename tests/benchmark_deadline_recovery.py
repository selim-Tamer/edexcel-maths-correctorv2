"""PR14/current: deterministic timing, not measured Gemini performance."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from support import fake_client
from pipeline_fakes import Clock
from test_deadline_recovery import deadline_error
import marking_requests as current


def run(module, scenario):
    clock = Clock(); client = fake_client(); calls = []
    def reply(**kw):
        calls.append(kw)
        if scenario == 'ambiguous':
            clock.sleep(120); raise TimeoutError('synthetic')
        if scenario in ('recover', 'persistent', 'next_student') and (kw['model'] == 'gemini-a' or scenario == 'persistent'):
            clock.sleep(kw['config']['http_options']['timeout']/1000)
            raise deadline_error()
        clock.sleep(20)
        return SimpleNamespace(text='{"questions":[{"awarded_marks":1,"max_marks":2}]}')
    client.models.generate_content.side_effect = reply
    outcomes = []
    for _ in range(2 if scenario == 'next_student' else 1):
        try:
            module.generate(client, ['gemini-a','gemini-b','gemini-c'], ['synthetic'],
                            'system', {}, lambda **kw: kw, json.loads, clock=clock)
            outcomes.append('completed')
        except module.MarkingFailure:
            outcomes.append('failed_without_marks')
    return {'outcomes': outcomes, 'requests': len(calls), 'simulated_seconds': clock.seconds}


def benchmark():
    spec = importlib.util.spec_from_file_location('pr14', Path(__file__).parent/'fixtures/pre_pr15_marking_requests.py')
    old = importlib.util.module_from_spec(spec); spec.loader.exec_module(old)
    with patch.object(current.random, 'uniform', return_value=0), patch.object(current, 'configured_attempts', return_value=3):
        return {'live_requests': 0, 'note': 'Scripted 20s success / request-timeout deadline. No live latency or accuracy claim.',
                'cases': {case: {'before': run(old, case), 'after': run(current, case)}
                          for case in ('success','recover','persistent','ambiguous','next_student')}}


if __name__ == '__main__':
    print(json.dumps(benchmark(), indent=2))
