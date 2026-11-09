"""Offline tests execute the exact inline workflow program, never a runner job."""
import ast
import contextlib
import io
import json
from pathlib import Path
import types
import unittest
from unittest import mock

WORKFLOW=Path(__file__).resolve().parents[1]/'.github/workflows/br02-oidc-hostname-diagnostic.yml'
VALUE='ACTIONS_ID_TOKEN_REQUEST_URL'
SENTINEL='synthetic-sensitive-value-do-not-log'


def load_program():
    workflow=json.loads(WORKFLOW.read_text(encoding='utf8'))
    run=workflow['jobs']['hostname']['steps'][0]['run']
    prefix="python3 -I -B - <<'PY'\n"
    if not run.startswith(prefix) or not run.endswith('\nPY\n'):
        raise AssertionError('Unexpected inline-program wrapper')
    source=run[len(prefix):-len('PY\n')]
    module=types.ModuleType('offline_hostname_diagnostic')
    exec(compile(source,'<reviewed-inline-hostname-diagnostic>','exec'),module.__dict__)
    return workflow,source,module


class HostnameDiagnosticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow,cls.source,cls.program=load_program()

    def inspect(self,value,present=True):
        result=self.program.inspect_url(value,present)
        self.assertTrue(all(type(v) is bool for k,v in result.items() if k!='hostname'))
        self.assertEqual('hostname' in result,result['hostname_emitted'])
        if 'hostname' in result:
            self.assertLessEqual(len(result['hostname']),253)
            self.assertTrue(result['hostname'].isascii())
        return result

    def refused(self,value):
        result=self.inspect(value)
        self.assertFalse(result['hostname_emitted'])
        self.assertNotIn(SENTINEL,json.dumps(result))
        return result

    def test_valid_hostname_only_no_path_or_query_disclosure(self):
        result=self.inspect('https://vstoken.actions.githubusercontent.com/opaque/'+SENTINEL+'?opaque='+SENTINEL)
        self.assertEqual(result['hostname'],'vstoken.actions.githubusercontent.com')
        self.assertNotIn(SENTINEL,json.dumps(result))
        self.assertNotIn('https://',json.dumps(result))

    def test_case_and_one_root_dot_are_normalized_with_boolean_evidence(self):
        result=self.inspect('HTTPS://VSTOKEN.ACTIONS.GITHUBUSERCONTENT.COM.:443/opaque')
        self.assertEqual(result['hostname'],'vstoken.actions.githubusercontent.com')
        self.assertTrue(result['trailing_dot_removed'])
        self.refused('https://vstoken.actions.githubusercontent.com../')

    def test_absent_empty_and_missing_authority_are_distinct(self):
        absent=self.inspect(None,False)
        self.assertFalse(absent['variable_present']);self.assertFalse(absent['url_parsed'])
        empty=self.refused('')
        self.assertTrue(empty['variable_present']);self.assertFalse(empty['value_nonempty'])
        nohost=self.refused('https:/opaque?opaque='+SENTINEL)
        self.assertTrue(nohost['url_parsed']);self.assertFalse(nohost['hostname_present'])

    def test_hostname_and_label_length_boundaries(self):
        accepted='.'.join(['a'*63,'b'*63,'c'*63,'d'*61])
        self.assertEqual(len(accepted),253)
        self.assertEqual(self.inspect('https://'+accepted+'/')['hostname'],accepted)
        too_long='.'.join(['a'*63,'b'*63,'c'*63,'d'*62])
        self.assertFalse(self.refused('https://'+too_long+'/')['hostname_length_valid'])
        self.refused('https://'+'a'*64+'.example/')

    def test_hostname_syntax_refuses_ip_unicode_and_encoded_values(self):
        for authority in ('127.0.0.1','127.1','2130706433','[::1]','[v1.fe80]',
                          '[v1.host.example]','singlelabel',
                          'bad_name.example','-bad.example','bad-.example',
                          'bad..example','%73ecret.example','host.ex\u00e4mple',
                          '\u212a.example'):
            with self.subTest(case=authority):self.refused('https://'+authority+'/')

    def test_suspicious_url_components_are_not_disclosed(self):
        for value in ('http://host.example/','https://'+SENTINEL+'@host.example/',
                      'https://:'+SENTINEL+'@host.example/','https://@host.example/',
                      'https://host.example:8443/','https://host.example/#'+SENTINEL,
                      'https://host.example\\'+SENTINEL+'/',
                      'https://host.example/\n'+SENTINEL,
                      ' https://host.example/','https://host.example/\x7f'+SENTINEL):
            self.refused(value)

    def test_malformed_and_oversized_values_never_raise_or_leak(self):
        for value in ('https://['+SENTINEL,'https://host.example:'+SENTINEL+'/',
                      'https://host.example:65536/','https://host.example:-1/',
                      'https://host.example\uff0f'+SENTINEL+'/',
                      'https://host.example/'+'x'*8192,None,object(),b'https://host.example/'):
            self.refused(value)

    def test_valid_dns_observation_does_not_assert_origin_authorization(self):
        result=self.inspect('https://unverified.example/opaque?opaque='+SENTINEL)
        self.assertEqual(result['hostname'],'unverified.example')
        self.assertNotIn('authorized',result)

    def run_main(self,env):
        output=io.StringIO();errors=io.StringIO()
        with mock.patch.object(self.program.os,'environ',env), \
                contextlib.redirect_stdout(output),contextlib.redirect_stderr(errors):
            status=self.program.main()
        self.assertEqual(errors.getvalue(),'')
        self.assertNotIn(SENTINEL,output.getvalue())
        return status,json.loads(output.getvalue())

    def test_main_reads_only_request_url_and_emits_one_safe_record(self):
        reads=[]
        class OnlyUrl(dict):
            def get(self,key,*args):
                reads.append(key)
                if key!=VALUE:raise AssertionError('Unexpected environment read')
                return super().get(key,*args)
            def __contains__(self,key):
                reads.append(key)
                if key!=VALUE:raise AssertionError('Unexpected environment read')
                return super().__contains__(key)
        status,result=self.run_main(OnlyUrl({VALUE:'https://host.example/'+SENTINEL+'?q='+SENTINEL}))
        self.assertEqual(status,0);self.assertEqual(result['hostname'],'host.example')
        self.assertEqual(reads,[VALUE,VALUE])

    def test_main_refuses_without_traceback_for_missing_or_bad_value(self):
        for env in ({},{VALUE:''},{VALUE:'https://['+SENTINEL}):
            status,result=self.run_main(env)
            self.assertEqual(status,1);self.assertFalse(result['hostname_emitted'])

    def test_unexpected_environment_failure_is_sanitized(self):
        class Broken(dict):
            def get(self,*args):raise RuntimeError(SENTINEL)
        status,result=self.run_main(Broken())
        self.assertEqual(status,1);self.assertTrue(result['internal_error'])
        self.assertFalse(result['hostname_emitted'])

    def test_output_failure_has_no_unsanitized_exception(self):
        with mock.patch.object(self.program.os,'environ',{VALUE:'https://host.example/'}), \
                mock.patch('builtins.print',side_effect=RuntimeError(SENTINEL)):
            self.assertEqual(self.program.main(),2)

    def test_exact_program_has_no_token_network_process_or_dynamic_import_path(self):
        tree=ast.parse(self.source)
        imports=[]
        for node in ast.walk(tree):
            if isinstance(node,ast.Import):imports.extend(n.name for n in node.names)
            elif isinstance(node,ast.ImportFrom):imports.append(node.module)
        self.assertEqual(set(imports),{'json','os','re','sys','urllib.parse'})
        for name in ('ACTIONS_ID_TOKEN_REQUEST_TOKEN','urlopen','requests','getIDToken',
                     'subprocess','socket','__import__','eval(','exec('):
            self.assertNotIn(name,self.source)

    def test_workflow_is_manual_owner_sha_bound_and_service_free(self):
        w=self.workflow;self.assertEqual(set(w['on']),{'workflow_dispatch'})
        self.assertEqual(w['permissions'],{})
        inputs=w['on']['workflow_dispatch']['inputs']
        self.assertEqual(set(inputs),{'approve_diagnostic','approved_sha'})
        self.assertIs(inputs['approve_diagnostic']['default'],False)
        self.assertEqual(len(w['jobs']),1);job=w['jobs']['hostname']
        self.assertEqual(job['runs-on'],'ubuntu-24.04');self.assertEqual(job['timeout-minutes'],2)
        self.assertEqual(job['permissions'],{'id-token':'write'})
        self.assertEqual(len(job['steps']),1)
        self.assertNotIn('uses',job['steps'][0]);self.assertNotIn('${{',job['steps'][0]['run'])
        for name in ('services','container','environment','strategy','secrets','env'):
            self.assertNotIn(name,job)
        for gate in ('inputs.approve_diagnostic','inputs.approved_sha == github.sha',
                     'github.run_attempt == 1',"github.actor_id == '329718877'",
                     "github.repository_id == '1389123218'",
                     "github.ref == 'refs/heads/br-02/postgres-result-persistence'"):
            self.assertIn(gate,job['if'])
        self.assertEqual(w['concurrency'],{'group':'br02-oidc-hostname-diagnostic','cancel-in-progress':False})


if __name__=='__main__':unittest.main(verbosity=2)
