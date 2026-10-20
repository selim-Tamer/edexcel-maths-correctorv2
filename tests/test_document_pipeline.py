"""Offline privacy, expiration, request-budget and preparation regressions."""
import ast
import copy
import importlib.util
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from support import load_app, SOURCE
from test_pdf_processing import synthetic_pdf
from pipeline_fakes import Clock, Provider, ApiError, uploaded
import document_pipeline as dp


class TransportTests(unittest.TestCase):
    def test_active_response_has_no_sleep_or_get_and_preserves_bytes(self):
        clock = Clock(); client = Provider(clock, transfer=0, processing=0)
        events=[]; data=b"%PDF-exact bytes\x00\xff"
        dp.transfer(client, data, "scan.pdf", events.append, clock)
        self.assertEqual(list(client.data.values()), [data])
        self.assertFalse(client.files.get.called)
        self.assertTrue(all(not Path(p).exists() for p in client.paths))
        self.assertEqual([e['stage'] for e in events if e['done']],
                         ['Local preparation', 'Transfer to Gemini', 'Gemini processing'])

    def test_poll_backoff_reduces_calls_with_bounded_detection_delay(self):
        clock=Clock(); client=Provider(clock)
        events=[]; dp.transfer(client,b'%PDF-x','scan.pdf',events.append,clock)
        self.assertEqual(client.files.get.call_count,7)
        self.assertEqual(clock.monotonic(),13.5)
        self.assertEqual(events[-1]['elapsed'],11.5)
        self.assertEqual(client.files.upload.call_args.kwargs['config']['http_options']['retry_options']['attempts'],1)

    def test_retry_after_is_obeyed_without_reuploading(self):
        clock=Clock(); client=Provider(clock,processing=0)
        remote=dp.transfer(client,b'%PDF-x','scan.pdf',clock=clock)
        client.files.get.side_effect=[ApiError(429,'7'),remote]
        self.assertIs(dp.get_status(client,remote.name,clock.monotonic()+30,clock),remote)
        self.assertIn(7,clock.sleeps)
        self.assertEqual(client.files.upload.call_count,1)

    def test_http_date_retry_after(self):
        from email.utils import formatdate
        clock=Clock()
        error=ApiError(503,formatdate(clock.time()+8,usegmt=True))
        self.assertEqual(dp.retry_delay(error,2,clock.time()),8)

    def test_retry_budget_and_long_retry_after(self):
        for retry_after,expected_calls in [(None,3),('200',1)]:
            clock=Clock(); client=Provider(clock)
            client.files.get.side_effect=ApiError(429,retry_after)
            with self.assertRaises((ApiError,TimeoutError)):
                dp.transfer(client,b'%PDF-x','scan.pdf',clock=clock)
            self.assertEqual(client.files.get.call_count,expected_calls)
            self.assertEqual(client.files.upload.call_count,1)
            client.files.delete.assert_called_once()

    def test_no_retry_for_auth_not_found_or_ambiguous_upload(self):
        for code in (400,401,403,404):
            clock=Clock(); client=Provider(clock)
            client.files.get.side_effect=ApiError(code)
            with self.assertRaises(ApiError): dp.transfer(client,b'%PDF-x','scan.pdf',clock=clock)
            self.assertEqual(client.files.get.call_count,1)
        clock=Clock(); client=Provider(clock)
        client.files.upload.side_effect=ApiError(503)
        with self.assertRaises(ApiError): dp.transfer(client,b'%PDF-x','scan.pdf',clock=clock)
        self.assertEqual(client.files.upload.call_count,1)

    def test_deadline_includes_retries_and_never_oversleeps(self):
        clock=Clock(); client=Provider(clock,processing=1000)
        with self.assertRaises(TimeoutError): dp.transfer(client,b'%PDF-x','scan.pdf',clock=clock)
        self.assertEqual(clock.monotonic(),182)
        self.assertLessEqual(client.files.get.call_count,92)
        last_timeout=client.files.get.call_args.kwargs['config']['http_options']['timeout']
        self.assertLessEqual(last_timeout,500)
        client.files.delete.assert_called_once()

    def test_provider_overruns_deadline_even_if_returns_active(self):
        clock=Clock(); client=Provider(clock,status_time=181)
        with self.assertRaises(TimeoutError): dp.transfer(client,b'%PDF-x','scan.pdf',clock=clock)
        client.files.delete.assert_called_once()


class FileCacheTests(unittest.TestCase):
    def setUp(self):
        self.clock=Clock(); self.client=Provider(self.clock,transfer=0,processing=0)
        self.cache=dp.FileCache('credential-and-account',clock=self.clock)
    def run_doc(self, data=b'%PDF-scheme', owner='assignment:scheme', reference=True,retain=True):
        run=dp.DocumentRun(self.client,self.cache,retain)
        try: return run.acquire(data,'scan.pdf',owner,reference)
        finally: run.close()

    def test_reference_reuse_checks_remote_once_no_duplicate_post(self):
        first=self.run_doc(); second=self.run_doc()
        self.assertEqual(first.name,second.name)
        self.assertEqual(self.client.files.upload.call_count,1)
        self.assertEqual(self.client.files.get.call_count,1)
        self.client.files.delete.assert_not_called()

    def test_student_always_deleted_and_never_reused_across_runs(self):
        first=self.run_doc(reference=False); second=self.run_doc(reference=False)
        self.assertNotEqual(first.name,second.name)
        self.assertEqual(self.client.files.delete.call_count,2)
        self.assertFalse(self.cache.entries)

    def test_default_immediate_cleanup_even_reference(self):
        self.run_doc(retain=False)
        self.client.files.delete.assert_called_once()
        self.assertFalse(self.cache.entries)

    def test_hash_owner_session_and_credentials_isolation(self):
        first=self.run_doc()
        self.assertNotEqual(self.run_doc(data=b'%PDF-changed').name,first.name)
        self.assertNotEqual(self.run_doc(owner='other-assignment:scheme').name,first.name)
        for scope in ('credential-and-account','other-credential'):
            run=dp.DocumentRun(self.client,dp.FileCache(scope,clock=self.clock),True)
            self.assertNotEqual(run.acquire(b'%PDF-scheme','scan.pdf','assignment:scheme',True).name,first.name)
            run.close()

    def test_same_run_duplicate_single_flight_and_distinct_student_owners(self):
        run=dp.DocumentRun(self.client,self.cache,True)
        with ThreadPoolExecutor(max_workers=2) as executor:
            rows=list(executor.map(lambda _:run.acquire(b'%PDF-x','scan.pdf','student:1'), range(2)))
        self.assertEqual(rows[0].name,rows[1].name)
        self.assertNotEqual(run.acquire(b'%PDF-x','scan.pdf','student:2').name,rows[0].name)
        self.assertEqual(self.client.files.upload.call_count,2)
        run.close(); self.assertEqual(self.client.files.delete.call_count,2)

    def test_ttl_is_not_extended_by_reuse(self):
        first=self.run_doc(); self.clock.sleep(599); self.run_doc()
        self.clock.sleep(1)
        self.assertNotEqual(self.run_doc().name,first.name)
        self.client.files.delete.assert_called_once_with(name=first.name)

    def test_unknown_and_near_expiry_never_retained(self):
        for expiry in (None,'bad','2020-01-01T00:00:00Z'):
            with self.subTest(expiry=expiry):
                self.client.files.upload.side_effect=lambda **kw: SimpleNamespace(name='uncacheable',state='ACTIVE',expiration_time=expiry)
                self.run_doc()
                self.assertFalse(self.cache.entries)
        self.assertEqual(self.client.files.delete.call_count,3)

    def test_external_deletion_reuploads_but_auth_and_quota_do_not(self):
        first=self.run_doc(); self.client.files.get.side_effect=ApiError(404)
        self.assertNotEqual(self.run_doc().name,first.name)
        for code in (403,429,503):
            self.client.files.get.side_effect=ApiError(code)
            before=self.client.files.upload.call_count
            with self.assertRaises(ApiError): self.run_doc()
            self.assertEqual(self.client.files.upload.call_count,before)

    def test_changed_remote_state_and_expiration_invalidate(self):
        for state in ('FAILED','PROCESSING','UNKNOWN'):
            self.cache.clear(); first=self.run_doc()
            self.client.files.get.side_effect=lambda **kw:self.client.remote(first.name,state)
            self.assertNotEqual(self.run_doc().name,first.name)
            self.client.files.get.side_effect=self.client.get

    def test_capacity_and_clear_delete_remote_files(self):
        self.cache.capacity=1
        first=self.run_doc(); self.run_doc(data=b'%PDF-other')
        self.client.files.delete.assert_called_once_with(name=first.name)
        self.cache.clear(); self.assertEqual(self.client.files.delete.call_count,2)

    def test_active_reference_not_deleted_by_ttl_or_capacity(self):
        self.cache.capacity=1
        run=dp.DocumentRun(self.client,self.cache,True)
        first=run.acquire(b'%PDF-scheme','a.pdf','scheme',True)
        self.clock.sleep(601)
        second=run.acquire(b'%PDF-second','b.pdf','question',True)
        self.client.files.delete.assert_not_called()
        run.close()
        self.assertEqual({c.kwargs['name'] for c in self.client.files.delete.call_args_list}, {first.name,second.name})

    def test_clear_defers_deletion_of_active_reference_until_close(self):
        run=dp.DocumentRun(self.client,self.cache,True)
        remote=run.acquire(synthetic_pdf(2),'scheme.pdf','scheme',True)
        self.cache.clear()
        self.client.files.delete.assert_not_called()
        run.close()
        self.client.files.delete.assert_called_once_with(name=remote.name)
        self.assertFalse(self.cache.entries)

    def test_shortened_provider_expiry_invalidates_cached_handle(self):
        old=self.run_doc()
        self.client.files.get.side_effect=lambda **kw:self.client.remote(old.name,'ACTIVE',expiry=30)
        new=self.run_doc()
        self.assertNotEqual(old.name,new.name)
        self.client.files.delete.assert_called_once_with(name=old.name)

    def test_cleanup_failure_count_and_idempotent_close(self):
        self.client.files.delete.side_effect=ApiError(503)
        run=dp.DocumentRun(self.client,self.cache)
        run.acquire(b'%PDF-student','s.pdf','student'); run.close(); run.close()
        self.assertEqual(self.cache.cleanup_failures,1)
        self.assertEqual(self.client.files.delete.call_count,1)


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.f=load_app(); self.clock=Clock(); self.client=Provider(self.clock,transfer=0,processing=0)
        self.session={'files':dp.FileCache('key-account',clock=self.clock),'metadata':dp.Memo(clock=self.clock),'results':dp.Memo(clock=self.clock)}
        self.students=[uploaded(b'%PDF-student1','one.pdf'),uploaded(b'%PDF-student2','two.pdf')]
        self.scheme=uploaded(synthetic_pdf(2),'scheme.pdf')
        self.f['extract_scheme_maxima']=Mock(return_value=({'1':2},{'1':'4'},None))
        self.f['discover_question_numbers']=Mock(return_value=['1','2','3','4'])
        self.f['select_spread_questions']=Mock(return_value=['1','3'])
    def prepare(self,students=None,scheme=None,audit=False,random_count=None,question=None,retain=False):
        run=dp.DocumentRun(self.client,self.session['files'],retain)
        try:
            return self.f['prepare_manual_batch'](self.client,students or self.students,scheme or self.scheme,question,audit,random_count,self.session,run)
        finally: run.close()
    def cache_results(self,plan):
        for key in plan['keys'].values(): self.session['results'].put(key,{'status':'Marked','result':{'untouched':True}})

    def test_full_warm_run_does_zero_remote_or_generation_requests(self):
        plan=self.prepare(); self.cache_results(plan)
        self.client.files.upload.reset_mock(); self.client.files.get.reset_mock()
        self.f['extract_scheme_maxima'].reset_mock()
        again=self.prepare()
        self.assertEqual(again['pending'],[])
        self.assertEqual(again['remotes'],{})
        self.client.files.upload.assert_not_called(); self.client.files.get.assert_not_called()
        self.f['extract_scheme_maxima'].assert_not_called()

    def test_partial_hit_uploads_only_missing_student_and_references(self):
        plan=self.prepare(); self.session['results'].put(plan['keys'][0],{'status':'Marked'})
        self.client.files.upload.reset_mock(); self.f['extract_scheme_maxima'].reset_mock()
        again=self.prepare()
        self.assertEqual(again['pending'],[1]); self.assertEqual(self.client.files.upload.call_count,2)
        self.f['extract_scheme_maxima'].assert_not_called()

    def test_changed_student_scheme_question_audit_and_models_invalidate(self):
        plan=self.prepare(); self.cache_results(plan)
        cases=[{'students':[uploaded(b'%PDF-changed','one.pdf'),self.students[1]]},
               {'scheme':uploaded(b'%PDF-new-scheme')}, {'question':uploaded(b'%PDF-question')}, {'audit':True}]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs): self.assertTrue(self.prepare(**kwargs)['pending'])
        self.f['PRIMARY_MODELS']=['new-model-config']
        self.assertTrue(self.prepare()['pending'])

    def test_random_inventory_cached_but_new_selection_invalidates_results(self):
        first=self.prepare(random_count=2); self.cache_results(first)
        self.f['select_spread_questions'].return_value=['2','4']
        second=self.prepare(random_count=2)
        self.assertEqual(second['selected'],['2','4']); self.assertEqual(second['pending'],[0,1])
        self.assertEqual(self.f['discover_question_numbers'].call_count,1)

    def test_failures_and_incomplete_audits_are_never_hits(self):
        plan=self.prepare()
        for row in ({'status':'Failed'},{'status':'Marked','audit_warning':'failed audit'}):
            for key in plan['keys'].values(): self.session['results'].put(key,row)
            self.assertEqual(self.prepare()['pending'],[0,1])

    def test_failed_scheme_extraction_is_retried(self):
        self.f['extract_scheme_maxima'].return_value=({}, {}, 'not available')
        self.prepare(); self.prepare()
        self.assertEqual(self.f['extract_scheme_maxima'].call_count,2)

    def test_memo_results_are_copies_expire_and_are_bounded(self):
        memo=dp.Memo(capacity=1,clock=self.clock)
        row={'marks':[1]}; memo.put('one',row); row['marks'][0]=99
        result=memo.get('one'); result['marks'][0]=22
        self.assertEqual(memo.get('one')['marks'],[1])
        memo.put('two',{}); self.assertIsNone(memo.get('one'))
        self.clock.sleep(600); self.assertIsNone(memo.get('two'))

    def test_actual_session_controller_clears_on_key_or_account_change(self):
        ui=SimpleNamespace(session_state={},user=SimpleNamespace(sub='account1'))
        self.f['st']=ui; self.f['google_logged_in']=lambda:True
        first=self.f['document_session']('synthetic-key-1')
        first['results'].put('private-result',{'status':'Marked'})
        self.assertIs(self.f['document_session']('synthetic-key-1'),first)
        second=self.f['document_session']('synthetic-key-2')
        self.assertIsNone(second['results'].get('private-result'))
        self.assertNotEqual(first['files'].scope,second['files'].scope)
        ui.user.sub='account2'
        third=self.f['document_session']('synthetic-key-2')
        self.assertNotEqual(second['files'].scope,third['files'].scope)

    def test_classroom_references_scoped_to_assignment_and_evidence_reused(self):
        helper=self.f['prepare_classroom_reference']
        for context in ('assignment1','assignment1','assignment2'):
            run=dp.DocumentRun(self.client,self.session['files'],True)
            helper(self.client,self.scheme,self.session,run,context,None,lambda *_:None)
            run.close()
        self.assertEqual(self.client.files.upload.call_count,2)
        self.assertEqual(self.client.files.get.call_count,1)
        self.assertEqual(self.f['extract_scheme_maxima'].call_count,2)

    def test_failure_after_reference_upload_releases_student_owned_files(self):
        run=dp.DocumentRun(self.client,self.session['files'])
        self.f['extract_scheme_maxima'].side_effect=RuntimeError('synthetic extraction error')
        with self.assertRaises(RuntimeError):
            try:
                self.f['prepare_manual_batch'](self.client,self.students,self.scheme,None,False,None,self.session,run)
            finally: run.close()
        self.assertEqual(self.client.files.delete.call_count, 3)
        self.assertEqual({c.kwargs["name"] for c in self.client.files.delete.call_args_list}, set(self.client.data))

class PerformanceBudgetTests(unittest.TestCase):
    def test_baseline_comparison_request_and_latency_budgets(self):
        from benchmark_document_pipeline import benchmark
        data=benchmark()
        old,new=data['cold_single_document']
        self.assertLess(new['status_gets'], old['status_gets']/2)
        self.assertLessEqual(new['virtual_seconds']-old['virtual_seconds'],2)
        self.assertEqual(data['warm_reference']['uploads'],0)
        self.assertEqual(data['warm_reference']['status_gets'],1)
        self.assertEqual(data['repeat_completed_batch_before']['uploads'],3)
        self.assertEqual(data['repeat_completed_batch_after'],{'uploads':0,'status_gets':0,'scheme_generations':0})

if __name__=='__main__': unittest.main()

