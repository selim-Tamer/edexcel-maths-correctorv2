"""Actual old/new request functions; simulated provider timing, never live Gemini."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from support import load_app, fake_client, remote, model_question, benchmark as fixtures
from pipeline_fakes import Clock, ApiError


def run(old, scenario):
    f = load_app()
    if old:
        exec((Path(__file__).parent/'fixtures/marking_request_baseline.py').read_text(encoding='utf-8'), f)
    clock = Clock(); f['time'] = clock
    client = fake_client()
    sample = {'questions': [model_question(q, 'partial') for q in fixtures()['questions'][:10]]}
    def reply(**kwargs):
        if scenario == 'timeout':
            clock.sleep(120)
            raise TimeoutError('synthetic timeout')
        if scenario == 'unavailable':
            clock.sleep(1)
            raise ApiError(503)
        if scenario == 'quota':
            clock.sleep(1)
            raise ApiError(429)
        clock.sleep(60)
        return SimpleNamespace(text=json.dumps(sample))
    client.models.generate_content.side_effect = reply
    outcome = 'completed'
    try:
        result, _ = f['grade_with_model'](client, f['PRIMARY_MODELS'], remote(), remote(name='scheme'),
            selected_questions=[str(n) for n in range(1, 11)], scheme_maxima={str(n): 2 for n in range(1, 11)})
        assert sum(q['awarded_marks'] for q in result['questions']) == 10
    except RuntimeError:
        outcome = 'failed_without_marks'
    calls = client.models.generate_content.call_args_list
    args = calls[0].kwargs
    return {'outcome': outcome, 'generation_calls': len(calls), 'virtual_seconds': clock.seconds,
            'prompt_characters': len(args['contents'][-1]),
            'system_characters': len(args['config']['system_instruction']),
            'schema_characters': len(json.dumps(args['config']['response_schema'], separators=(',', ':'))),
            'response_characters': len(json.dumps(sample)) if outcome == 'completed' else 0}


def benchmark():
    return {'live_requests': 0, 'timing': 'SIMULATED: success 60s, timeout 120s, failure 1s per generation; no SDK retry assumption',
            'token_note': 'Character counts are exact; not tokenizer counts or measured model speed.',
            'cases': {name: {'before': run(True, name), 'after': run(False, name)}
                      for name in ('success', 'timeout', 'unavailable', 'quota')}}


if __name__ == '__main__':
    print(json.dumps(benchmark(), indent=2))
