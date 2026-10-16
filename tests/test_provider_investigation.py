"""Actual independent diagnostic code; injected HTTP only, never live requests."""
import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch
from support import load_app
from investigation import diagnose as d
from investigation import accuracy as a
from investigation.pdf_fixtures import pdf_bytes
from investigation.profile import profile


def envelope(value, finish='STOP'):
    return json.dumps({'candidates':[{'finishReason':finish,'content':{'parts':[{'text':json.dumps(value)}]}}],
        'modelVersion':d.MODELS[0], 'usageMetadata':{'promptTokenCount':20}}).encode()


def valid_marks():
    return {'student_transcription':'2x = 8, x = 4','total':2,'uncertain':False,
        'awards':[{'code':'M1','awarded':True,'evidence':'2x=8'},
                  {'code':'A1','awarded':True,'evidence':'x=4'}]}


class DiagnosticTests(unittest.TestCase):
    def test_offline_default_ignores_all_environment_credentials(self):
        with patch.dict(os.environ,{'GEMINI_API_KEY':'PRIVATE','GOOGLE_API_KEY':'PRIVATE'}), \
                patch.object(d,'probe',side_effect=AssertionError('network')), \
                patch.object(d.getpass,'getpass',side_effect=AssertionError('credential access')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(d.main([]),0)
        self.assertNotIn('PRIVATE',output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['requests'],0)

    def test_live_requires_fresh_pricing_attestation_before_credentials(self):
        with patch.object(d.getpass,'getpass',side_effect=AssertionError('credential access')), \
                contextlib.redirect_stderr(io.StringIO()):
            for args in (['--live'],['--live','--confirm-unbilled-test-project','--pricing-verified-on','2000-01-01']):
                with self.assertRaises(SystemExit): d.main(args)

    def test_live_noninteractive_refused(self):
        with patch.object(d.sys.stdin,'isatty',return_value=False), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                d.main(['--live','--confirm-unbilled-test-project','--pricing-verified-on',d.date.today().isoformat()])

    def test_worker_failure_does_not_claim_zero_requests_or_print_exception(self):
        with patch.object(d.sys.stdin,'isatty',return_value=True), \
                patch('builtins.input',return_value='UNBILLED TEST PROJECT'), \
                patch.object(d.getpass,'getpass',return_value='PRIVATE'), \
                patch.object(d,'live_probe',side_effect=RuntimeError('PRIVATE')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(d.main(['--live','--confirm-unbilled-test-project',
                '--pricing-verified-on',d.date.today().isoformat()]),2)
        result=json.loads(output.getvalue().splitlines()[-1])
        self.assertIsNone(result['attempt_count'])
        self.assertEqual(result['attempt_count_upper_bound'],1)
        self.assertNotIn('PRIVATE',output.getvalue())

    def test_configuration_rejects_paid_models_arbitrary_paths_and_long_timeouts(self):
        for model,timeout in [('gemini-4-argon',30),(d.MODELS[0],120),(d.MODELS[0],0)]:
            with self.assertRaises(ValueError): d.probe(model,'A',timeout,'synthetic')
        with self.assertRaises(ValueError): d.build_request('PRIVATE')

    def test_parent_deadline_terminates_one_worker_without_replay(self):
        from unittest.mock import Mock
        context=Mock(); receive=Mock(); send=Mock(); process=Mock()
        context.Pipe.return_value=(receive,send); context.Process.return_value=process
        receive.poll.return_value=False; process.is_alive.return_value=True
        result=d.live_probe(d.MODELS[0],'A',5,'PRIVATE','digital',context=context)
        process.start.assert_called_once(); process.terminate.assert_called_once()
        context.Process.assert_called_once(); receive.poll.assert_called_once_with(7)
        self.assertEqual(result['attempt_count_upper_bound'],1)
        self.assertNotIn('PRIVATE',json.dumps(result))

    def test_abc_requests_preserve_only_fixed_synthetic_evidence(self):
        self.assertIn('2 + 2',d.build_request('A')['contents'][0]['parts'][0]['text'])
        self.assertIn('M1',d.build_request('B')['contents'][0]['parts'][0]['text'])
        parts=d.build_request('C','strokes')['contents'][0]['parts']
        self.assertEqual(sum('inlineData' in p for p in parts),2)
        self.assertNotIn('2x = 8',parts[0]['text'])
        for level in ('A','B','C'):
            config=d.build_request(level)['generationConfig']
            self.assertNotIn('thinkingConfig',config)
            self.assertNotIn('tools',config)

    def test_schema_correct_incorrect_and_partial(self):
        self.assertTrue(d.assess('A',{'answer':4})['score_agreement'])
        self.assertFalse(d.assess('A',{'answer':5})['score_agreement'])
        self.assertTrue(d.assess('B',valid_marks())['score_agreement'])
        partial=valid_marks(); partial['total']=1; partial['awards'][1]['awarded']=False
        self.assertFalse(d.assess('B',partial)['score_agreement'])
        self.assertTrue(d.assess('B',partial)['method_mark_agreement'])

    def test_missing_contradictory_and_boolean_scores_rejected(self):
        bad=valid_marks(); bad['total']=1
        missing=valid_marks(); missing['awards'][0]['evidence']=''
        duplicate=valid_marks(); duplicate['awards'][1]['code']='M1'
        for payload in (bad,missing,duplicate,{'total':2}):
            with self.assertRaises(ValueError): d.assess('B',payload)
        with self.assertRaises(ValueError): d.assess('A',{'answer':True})

    def test_error_body_never_retained_and_status_preserved(self):
        for code in (400,401,403,404,429,503,504):
            result=d.summarize(code,b'PRIVATE credentials and student answer','B',d.MODELS[0])
            self.assertEqual(result['http_status'],code)
            self.assertNotIn('PRIVATE',json.dumps(result))

    def test_success_log_excludes_raw_model_response_and_blocks_truncation(self):
        value=valid_marks(); value['student_transcription']='PRIVATE'
        self.assertNotIn('PRIVATE',json.dumps(d.summarize(200,envelope(value),'B',d.MODELS[0])))
        self.assertEqual(d.summarize(200,envelope(value,'MAX_TOKENS'),'B',d.MODELS[0])['outcome'],'incomplete_or_blocked')
        self.assertFalse(d.summarize(200,b'not json','B',d.MODELS[0])['structured_valid'])

    def test_pdf_fixture_reproducible_and_stroke_work_not_a_text_layer(self):
        from pypdf import PdfReader
        for kind in ('digital','strokes','scheme'):
            self.assertEqual(pdf_bytes(kind),pdf_bytes(kind))
            reader=PdfReader(io.BytesIO(pdf_bytes(kind)))
            self.assertEqual(len(reader.pages),1)
            self.assertLess(len(pdf_bytes(kind)),20000)
        self.assertNotIn('2x = 8',PdfReader(io.BytesIO(pdf_bytes('strokes'))).pages[0].extract_text())

    def test_profile_matches_actual_production_constants(self):
        production=load_app(); p=profile()
        self.assertEqual(p['production']['models'],production['PRIMARY_MODELS'])
        self.assertEqual(p['production']['schema_characters'],1544)
        self.assertEqual(p['network_requests'],0)


class AccuracyTests(unittest.TestCase):
    def predictions(self):
        return [{'id':c['id'],'awards':[False if x is None else x for x in c['expected']],
                 'uncertain':c['uncertain'],'seconds':1} for c in a.cases()]

    def test_gold_not_in_prompt_and_all_required_topics_present(self):
        self.assertTrue({'algebra','geometry','trigonometry','graphs','statistics','multi-step'} <= {c['topic'] for c in a.cases()})
        for c in a.cases():
            self.assertNotIn('expected',a.prompt_case(c)); self.assertNotIn('derivation',a.prompt_case(c))
            self.assertEqual(len(c['scheme']),len(c['expected']))
        self.assertEqual(0*5+1*6+2*2+3*3+4*2,27)
        self.assertEqual(round(1690/60,1),28.2)
        self.assertEqual((67+1)/2,34)

    def test_scorer_identity_is_only_a_metric_self_test_not_model_accuracy(self):
        result=a.evaluate(self.predictions())
        self.assertEqual(result['exact_score_agreement'],1)
        self.assertEqual(result['scorable_cases'],12)
        self.assertTrue(result['teacher_review_required'])

    def test_same_total_wrong_method_detected(self):
        predictions=self.predictions()
        predictions[1]['awards']=[False,True]
        result=a.evaluate(predictions)
        self.assertEqual(result['exact_score_agreement'],1)
        self.assertLess(result['question_level_criterion_accuracy'],1)
        self.assertEqual(result['false_awards'],1)
        self.assertEqual(result['incorrect_deductions'],1)

    def test_missing_invalid_and_failed_attempts_not_excluded(self):
        predictions=self.predictions()[1:]
        predictions[0]['awards']=[1,0]
        predictions[1]['failed']=True
        result=a.evaluate(predictions)
        self.assertEqual(result['failures'],3)
        self.assertLess(result['exact_score_agreement'],1)
        self.assertEqual(a.evaluate([])['failure_rate'],1)

    def test_unknown_duplicate_ids_and_nonfinite_duration(self):
        p=self.predictions()
        with self.assertRaises(ValueError): a.evaluate(p+[p[0]])
        with self.assertRaises(ValueError): a.evaluate([{'id':'PRIVATE'}])
        p[0]['seconds']=float('nan')
        self.assertEqual(a.evaluate(p)['failures'],1)
