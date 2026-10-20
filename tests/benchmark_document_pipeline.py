"""Reproducible virtual-service timings and actual production preparation request counts.
Run: python tests/benchmark_document_pipeline.py. All data and service calls are synthetic.
Virtual seconds are a controlled scenario, NOT measurements of a live deployment.
"""
import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock
from support import load_app
from pipeline_fakes import Clock, Provider, uploaded
import document_pipeline as dp

def baseline():
    path=Path(__file__).parent/'fixtures/upload_baseline.py'
    spec=importlib.util.spec_from_file_location('upload_baseline',path)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module

def benchmark():
    old=baseline(); output={"live_requests":0,"scenario":"2s upload, 10s processing, 50ms status RTT; virtual clock"}
    samples=[]
    for label,implementation in [('PR4',old.upload_bytes_to_gemini),('optimized',dp.transfer)]:
        clock=Clock(); provider=Provider(clock,status_time=.05)
        old.time=clock
        if label=='PR4': implementation(provider,b'%PDF-synthetic','synthetic.pdf')
        else: implementation(provider,b'%PDF-synthetic','synthetic.pdf',clock=clock)
        samples.append(dict(version=label,virtual_seconds=round(clock.monotonic(),3),uploads=provider.files.upload.call_count,status_gets=provider.files.get.call_count))
    output['cold_single_document']=samples
    clock=Clock(); provider=Provider(clock,status_time=.05)
    cache=dp.FileCache('synthetic-key-account',clock=clock)
    first=dp.DocumentRun(provider,cache,True); first.acquire(b'%PDF-scheme','s.pdf','reference',True); first.close()
    before=clock.monotonic(); provider.files.upload.reset_mock(); provider.files.get.reset_mock()
    warm=dp.DocumentRun(provider,cache,True); warm.acquire(b'%PDF-scheme','s.pdf','reference',True); warm.close()
    output['warm_reference']=dict(virtual_seconds=round(clock.monotonic()-before,3),uploads=provider.files.upload.call_count,status_gets=provider.files.get.call_count)
    # Count the exact old UI preparation statements; use zero-duration fakes here
    # because a shared virtual clock cannot simulate concurrent elapsed time.
    students=[uploaded(b'%PDF-student')]; scheme=uploaded(b'%PDF-scheme','scheme.pdf'); question=uploaded(b'%PDF-questions','q.pdf')
    clock=Clock(); provider=Provider(clock,transfer=0,processing=0); old.time=clock
    old.extract_scheme_maxima=Mock(return_value=({'1':2},{'1':'4'},None))
    old.prepare_manual_baseline(provider,students,scheme,question)
    output['repeat_completed_batch_before']=dict(uploads=provider.files.upload.call_count,scheme_generations=old.extract_scheme_maxima.call_count)
    f=load_app(); f['extract_scheme_maxima']=Mock(return_value=({'1':2},{'1':'4'},None))
    session={'files':dp.FileCache('test',clock=clock),'metadata':dp.Memo(clock=clock),'results':dp.Memo(clock=clock)}
    run=dp.DocumentRun(provider,session['files']); plan=f['prepare_manual_batch'](provider,students,scheme,question,False,None,session,run); run.close()
    session['results'].put(plan['keys'][0],{'status':'Marked'})
    provider.files.upload.reset_mock(); provider.files.get.reset_mock(); f['extract_scheme_maxima'].reset_mock()
    run=dp.DocumentRun(provider,session['files']); f['prepare_manual_batch'](provider,students,scheme,question,False,None,session,run); run.close()
    output['repeat_completed_batch_after']=dict(uploads=provider.files.upload.call_count,status_gets=provider.files.get.call_count,scheme_generations=f['extract_scheme_maxima'].call_count)
    return output

if __name__=='__main__': print(json.dumps(benchmark(),indent=2))
