"""Opt-in, one-shot Gemini diagnostics. Default invocation is entirely offline.

Only fixed synthetic content is accepted. No env/secrets credential discovery,
user PDFs, retries, provider switching, tools, Files API or grade publication.
"""
import argparse
import base64
import getpass
import json
import re
import sys
import time
from datetime import date

MODELS = ('gemini-3.8-flash', 'gemini-3.6-flash', 'gemini-3.5-flash-lite')
ENDPOINT = 'https://generativelanguage.googleapis.com/v1beta/models/'
MAX_BODY = 1024 * 1024
A_SCHEMA = {'type':'object', 'properties':{'answer':{'type':'integer'}}, 'required':['answer']}
MARK_SCHEMA = {
    'type':'object', 'properties':{
        'student_transcription':{'type':'string'}, 'total':{'type':'integer'},
        'uncertain':{'type':'boolean'},
        'awards':{'type':'array', 'items':{'type':'object', 'properties':{
            'code':{'type':'string', 'enum':['M1','A1']}, 'awarded':{'type':'boolean'},
            'evidence':{'type':'string'}}, 'required':['code','awarded','evidence']}},
    }, 'required':['student_transcription','total','uncertain','awards']}


def build_request(level, variant='digital'):
    if level not in ('A','B','C') or variant not in ('digital','strokes'):
        raise ValueError('Unsupported diagnostic selection')
    schema = A_SCHEMA if level == 'A' else MARK_SCHEMA
    if level == 'A':
        parts = [{'text':'What is 2 + 2? Return JSON with integer answer.'}]
    else:
        prompt = ('Mark the actual student working using each scheme criterion. '
                  'Return JSON, with one M1 and one A1 award and concrete student evidence. '
                  'Do not substitute scheme answers for student work. '
                  'Set uncertain true if the source is ambiguous. This is synthetic, not an official grade.')
        if level == 'B':
            prompt += (' Question: Solve 2x + 3 = 11. Student: 2x = 8, x = 4. '
                       'Scheme: M1 for subtracting 3 to get 2x = 8; '
                       'A1 for dividing by 2 to get x = 4 after valid method.')
            parts = [{'text':prompt}]
        else:
            from .pdf_fixtures import pdf_bytes
            parts = [{'text':prompt + ' First PDF: student. Second PDF: scheme.'}]
            for kind in (variant, 'scheme'):
                parts.append({'inlineData':{'mimeType':'application/pdf',
                    'data':base64.b64encode(pdf_bytes(kind)).decode('ascii')}})
    # No thinking override or output truncation. Small fixture, original evidence.
    return {'contents':[{'role':'user','parts':parts}],
            'generationConfig':{'responseMimeType':'application/json','responseSchema':schema}}


def assess(level, value):
    """Strict local shape/criterion checks; not a proof of examiner accuracy."""
    if not isinstance(value, dict): raise ValueError('Invalid shape')
    if level == 'A':
        if set(value) != {'answer'} or type(value['answer']) is not int:
            raise ValueError('Invalid shape')
        return {'score_agreement':value['answer'] == 4}
    if set(value) != {'student_transcription','total','uncertain','awards'}:
        raise ValueError('Invalid shape')
    if (type(value['total']) is not int or not 0 <= value['total'] <= 2
            or type(value['uncertain']) is not bool
            or not isinstance(value['student_transcription'], str)
            or not value['student_transcription'].strip()
            or not isinstance(value['awards'], list) or len(value['awards']) != 2):
        raise ValueError('Invalid marking')
    awards = {}
    for row in value['awards']:
        if (not isinstance(row, dict) or set(row) != {'code','awarded','evidence'}
                or row['code'] not in ('M1','A1') or row['code'] in awards
                or type(row['awarded']) is not bool or not isinstance(row['evidence'], str)
                or not row['evidence'].strip()):
            raise ValueError('Invalid evidence')
        awards[row['code']] = row['awarded']
    if sum(awards.values()) != value['total'] or (awards['A1'] and not awards['M1']):
        raise ValueError('Inconsistent marks')
    return {'score_agreement':value['total'] == 2,
            'method_mark_agreement':awards['M1'], 'accuracy_mark_agreement':awards['A1'],
            'uncertainty_flag':value['uncertain']}


def summarize(status, body, level, model):
    result = {'http_status':status, 'outcome':'invalid_response', 'structured_valid':False}
    if status != 200:
        result['outcome'] = {400:'request_rejected',401:'authentication',403:'access_denied',
            404:'not_found',429:'quota',503:'service_unavailable',504:'provider_timeout'}.get(status,'http_error')
        return result  # Never inspect or retain raw error messages.
    try:
        envelope = json.loads(body)
        candidates = envelope['candidates']
        if len(candidates) != 1 or candidates[0].get('finishReason') != 'STOP':
            result['outcome'] = 'incomplete_or_blocked'; return result
        parts = candidates[0]['content']['parts']
        text = ''.join(p['text'] for p in parts if isinstance(p,dict) and not p.get('thought') and 'text' in p)
        value = json.loads(text)
        result.update(assess(level, value), structured_valid=True, outcome='completed')
        version = envelope.get('modelVersion')
        if isinstance(version,str) and re.fullmatch(re.escape(model)+r'(?:-[0-9a-z.-]{1,40})?',version):
            result['reported_model'] = version
        for key in ('promptTokenCount','candidatesTokenCount','thoughtsTokenCount','totalTokenCount'):
            number = envelope.get('usageMetadata',{}).get(key)
            if type(number) is int and 0 <= number <= 10**9: result[key] = number
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        pass
    return result


def probe(model, level, timeout, credential, *, variant='digital', transport=None):
    """One fixed-host request. Injectable transport is for offline tests only."""
    if model not in MODELS or type(timeout) is not int or not 5 <= timeout <= 60:
        raise ValueError('Unsupported model or timeout')
    if not isinstance(credential,str) or not credential or '\n' in credential or '\r' in credential:
        raise ValueError('Invalid credential')
    payload = build_request(level, variant)
    import httpx
    started = time.monotonic()
    result = {'model':model,'level':level,'variant':variant if level=='C' else 'text',
              'attempt_count':0,'timeout_seconds':timeout,'live':transport is None}
    try:
        with httpx.Client(transport=transport if transport is not None else httpx.HTTPTransport(retries=0),
                          timeout=timeout, follow_redirects=False, trust_env=False) as client:
            result['attempt_count'] = 1
            with client.stream('POST', ENDPOINT+model+':generateContent',
                               headers={'x-goog-api-key':credential}, json=payload) as response:
                result['http_status'] = response.status_code
                data = bytearray()
                for chunk in response.iter_bytes():
                    if len(data)+len(chunk) > MAX_BODY:
                        result['outcome'] = 'response_too_large'; break
                    if time.monotonic()-started > timeout:
                        result['outcome'] = 'client_timeout'; break
                    data.extend(chunk)
                else:
                    result.update(summarize(response.status_code, data, level, model))
    except httpx.TimeoutException:
        result['outcome'] = 'client_timeout'
    except Exception:
        result['outcome'] = 'transport_or_client_failure'
    result['elapsed_seconds'] = round(time.monotonic()-started, 3)
    return result


def _live_worker(pipe, model, level, timeout, credential, variant):
    try:
        pipe.send(probe(model,level,timeout,credential,variant=variant))
    except BaseException:
        pipe.send({'outcome':'local_worker_failure','attempt_count_upper_bound':1})
    finally:
        pipe.close()


def live_probe(model, level, timeout, credential, variant, context=None):
    """Parent watchdog bounds local runtime; terminating does not cancel provider compute."""
    import multiprocessing
    context = context or multiprocessing.get_context('spawn')
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=_live_worker,args=(send,model,level,timeout,credential,variant))
    started=time.monotonic()
    try:
        process.start(); send.close()
        if receive.poll(timeout+2):
            return receive.recv()
        return {'model':model,'level':level,'outcome':'local_deadline',
                'attempt_count':None,'attempt_count_upper_bound':1,'live':True,
                'elapsed_seconds':round(time.monotonic()-started,3)}
    finally:
        if process.is_alive(): process.terminate()
        if process.pid is not None: process.join(2)
        receive.close(); send.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--model', choices=MODELS, default=MODELS[0])
    parser.add_argument('--level', choices=('A','B','C'), default='A')
    parser.add_argument('--variant', choices=('digital','strokes'), default='digital')
    parser.add_argument('--timeout', type=int, choices=range(5,61), default=30, metavar='5..60')
    parser.add_argument('--confirm-unbilled-test-project', action='store_true')
    parser.add_argument('--pricing-verified-on', default='')
    args = parser.parse_args(argv)
    if not args.live:
        payload = build_request(args.level,args.variant)
        print(json.dumps({'mode':'offline_plan','requests':0,'model':args.model,'level':args.level,
            'schema_characters':len(json.dumps(payload['generationConfig']['responseSchema'])),
            'pdf_count':sum('inlineData' in p for p in payload['contents'][0]['parts'])}))
        return 0
    if not args.confirm_unbilled_test_project or args.pricing_verified_on != date.today().isoformat():
        parser.error('Live diagnostics require a verified unbilled test project and today\'s pricing verification date.')
    if not sys.stdin.isatty(): parser.error('Live tests require an interactive terminal; environment credentials are never used.')
    print('One generation request; no retries. Use a NEW test-only project with NO billing attached.\n'
          'A model with a free tier can still bill a paid project. This tool cannot inspect billing.\n'
          'Verify current free access and no billing before continuing. Synthetic content only.')
    if input('Type UNBILLED TEST PROJECT to continue: ') != 'UNBILLED TEST PROJECT':
        return 2
    key = getpass.getpass('Test-only Gemini API key (hidden): ')
    try:
        result = live_probe(args.model,args.level,args.timeout,key,args.variant)
    except Exception:
        # The worker may already have sent its request before communication failed.
        print(json.dumps({'outcome':'local_diagnostic_failure',
                          'attempt_count':None,'attempt_count_upper_bound':1})); return 2
    finally:
        key = None
    print(json.dumps(result,sort_keys=True))
    return 0 if result.get('outcome') == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
