"""Offline-only CI boundary tests. No credentials, native child or SQL runs."""
import contextlib
import hashlib
import hmac
import importlib
import io
import itertools
import tempfile
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest
from unittest import mock
from types import SimpleNamespace

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
ci=runner=target=None
PAYLOAD='synthetic-error-text-not-a-real-secret'


class TokenResponse(io.BytesIO):
    status=200


_jtis=itertools.count()

def issue_ticket(*,claims=None,environ=None):
    value=IdentityTests().claims() if claims is None else dict(claims)
    if claims is None:
        now=int(time.time());value.update(iat=now-1,nbf=now-1,exp=now+120,jti='offline-'+str(next(_jtis)))
    env={'GITHUB_ACTIONS':'true','GITHUB_SHA':'a'*40,'BR02_CI_APPROVED_SHA':'a'*40,
         'GITHUB_RUN_ID':'1234','ACTIONS_ID_TOKEN_REQUEST_URL':'https://vstoken.actions.githubusercontent.com/inert',
         'ACTIONS_ID_TOKEN_REQUEST_TOKEN':'inert-not-a-token'}
    if environ:env.update(environ)
    opener=mock.Mock();opener.open.return_value=TokenResponse(b'{"value":"inert-signed-token"}')
    with mock.patch.object(ci.urllib.request,'build_opener',return_value=opener), \
            mock.patch.object(ci,'_verify_jwt',return_value=value) as verify:
        result=ci.authenticate(env)
        verify.assert_called_once_with('inert-signed-token')
        return result


@contextlib.contextmanager
def issued_context(binding=None,approval=None,*,ticket=None,seconds=60):
    binding=TargetTests().binding() if binding is None else binding
    approval=ci.make_approval(binding,{'fixture':'inert-admin','app':'inert-app','worker':'inert-worker'}) if approval is None else approval
    if ticket is None:_,ticket=issue_ticket()
    stream=mock.MagicMock();stream.__enter__.return_value=stream;stream.fileno.return_value=16
    with mock.patch.object(ci.os,'name','posix'),mock.patch.object(ci.os,'geteuid',create=True,return_value=0), \
            mock.patch.object(tempfile,'TemporaryFile',return_value=stream), \
            mock.patch('secrets.token_bytes',return_value=b'K'*32), \
            mock.patch.object(ci.os,'open',return_value=17),mock.patch.object(ci.os,'close'):
        with ci.installed(approval,binding,ticket=ticket,seconds=seconds) as context:
            yield context


def child_env(**changes):
    env={'BR02_CI_FD':'17','BR02_CI_RUN_ID':'1234','BR02_CI_SHA':'a'*40,'BR02_CI_ATTEMPT':'1'}
    env.update(changes);return env


@contextlib.contextmanager
def child_native(payload,*,uid=0,links=0,mode=None,access=None):
    import stat
    with mock.patch.dict(sys.modules,{'fcntl':SimpleNamespace(F_GETFL=3,fcntl=lambda *args:os.O_RDONLY if access is None else access)}), \
            mock.patch.object(ci.os,'name','posix'),mock.patch.object(ci.os,'O_ACCMODE',3,create=True), \
            mock.patch.object(ci.os,'getuid',create=True,return_value=60001), \
            mock.patch.object(ci.os,'fstat',return_value=SimpleNamespace(st_uid=uid,st_nlink=links,st_mode=stat.S_IFREG|0o600 if mode is None else mode)), \
            mock.patch.object(ci.os,'pread',create=True,return_value=payload),mock.patch.object(ci.os,'close') as close:
        yield close


def child_payload(binding=None,**changes):
    value={'binding':TargetTests().binding() if binding is None else binding,'app':'inert-app','expires':time.monotonic()+60}
    value.update(changes)
    raw=json.dumps(value,sort_keys=True).encode();key=b'K'*32
    return key+hmac.digest(key,raw,'sha256')+raw


class TargetDiagnosticTests(unittest.TestCase):
    def event(self,output,stage,reason='refused'):
        lines=[line for line in output.getvalue().splitlines() if line.startswith('BR02_CI_TARGET_DIAGNOSTIC ')]
        self.assertEqual(len(lines),1)
        self.assertEqual(json.loads(lines[0].split(' ',1)[1]),{'stage':stage,'reason':reason})
        self.assertNotIn(PAYLOAD,output.getvalue())

    def test_container_predicates_emit_only_fixed_stage_and_keep_refusal(self):
        cases=[('container_shape',lambda x:None),
               ('container_id',lambda x:dict(x,Id=PAYLOAD)),
               ('container_image',lambda x:dict(x,Config={'Image':PAYLOAD})),
               ('container_running',lambda x:dict(x,State={'Running':False})),
               ('port_binding',lambda x:dict(x,NetworkSettings={'Ports':{'5432/tcp':[{'HostIp':'0.0.0.0','HostPort':'55432'}]}})),
               ('network_count',lambda x:dict(x,NetworkSettings={'Ports':x['NetworkSettings']['Ports'],'Networks':{}})),
               ('network_address',lambda x:dict(x,NetworkSettings={'Ports':x['NetworkSettings']['Ports'],'Networks':{'test':{'IPAddress':'8.8.8.8'}}})),
               ('mount_count',lambda x:dict(x,Mounts=[])),
               ('mount_type',lambda x:dict(x,Mounts=[dict(x['Mounts'][0],Type='bind')])),
               ('mount_destination',lambda x:dict(x,Mounts=[dict(x['Mounts'][0],Destination=PAYLOAD)])),
               ('volume_name',lambda x:dict(x,Mounts=[dict(x['Mounts'][0],Name=PAYLOAD)]))]
        for stage,change in cases:
            output=io.StringIO()
            with self.subTest(stage=stage),contextlib.redirect_stderr(output):
                with self.assertRaises(ci.CIDenied) as caught:ci.validate_target(change(TargetTests().info()),{})
                self.assertEqual(caught.exception.code,'target')
                self.event(output,stage)

    def test_missing_fields_and_identity_shape_are_sanitized(self):
        for info,identity,stage in (({'Id':'a'*64}, {},'container_image'),
                                    (TargetTests().info(),{},'identity_binding')):
            output=io.StringIO()
            with self.subTest(stage=stage),contextlib.redirect_stderr(output):
                with self.assertRaises(ci.CIDenied):ci.validate_target(info,identity)
                self.event(output,stage,'malformed')

    def test_valid_target_and_binding_emit_nothing(self):
        output=io.StringIO()
        with contextlib.redirect_stderr(output):binding=TargetTests().binding()
        self.assertEqual(binding['database'],'br02_ci');self.assertEqual(output.getvalue(),'')

    def test_failed_log_sink_and_unknown_vocabulary_never_change_denial(self):
        with mock.patch('builtins.print',side_effect=OSError(PAYLOAD)):
            with self.assertRaises(ci.CIDenied):ci.validate_target(None,{})
        output=io.StringIO()
        with contextlib.redirect_stderr(output):ci._target_diagnostic(PAYLOAD,PAYLOAD)
        self.event(output,'unknown')

    def bootstrap_probe(self,stage=None,server_address='172.18.0.2'):
        binding=TargetTests().binding();_,ticket=issue_ticket()
        row=['br02_ci','PostgreSQL 17.11 synthetic',server_address,5432]
        for name,index,value in [('database_name',0,PAYLOAD),('database_version',1,PAYLOAD),
                                 ('database_address',2,'8.8.8.8'),('database_port',3,5433)]:
            if stage==name and (index!=2 or server_address=='172.18.0.2'):row[index]=value
        conn=mock.MagicMock();conn.__enter__.return_value=conn
        def execute(query,*args):
            cursor=mock.Mock();text=str(query)
            if text.startswith('select current_database'):
                # PostgreSQL's inet-to-text cast always includes the netmask;
                # host(inet) returns only the address. No database is executed.
                reported=list(row)
                if 'inet_server_addr()::text' in text and reported[2] is not None:
                    import ipaddress
                    address=ipaddress.ip_address(reported[2])
                    reported[2]=str(address)+'/'+str(address.max_prefixlen)
                else:
                    self.assertIn('host(inet_server_addr())',text)
                cursor.fetchone.return_value=reported
            elif 'pg_catalog.pg_tables' in text:cursor.fetchone.return_value=(1 if stage=='database_empty' else 0,)
            elif text.startswith('select rolname'):
                roles=[(name,False,False,False,False,False) for name in ('marker_app_login','marker_worker_login','br02_ci_owner')]
                if stage=='role_attributes':roles[0]=('marker_app_login',True,False,False,False,False)
                cursor.fetchall.return_value=roles
            elif text.startswith('select count(*) from pg_roles'):cursor.fetchone.return_value=(1 if stage=='role_membership' else 0,)
            elif 'pg_control_system' in text:cursor.fetchone.return_value=('123456',)
            return cursor
        conn.execute.side_effect=execute;output=io.StringIO()
        with mock.patch('psycopg.connect',return_value=conn) as connect, \
                mock.patch.object(ci.os,'name','posix'),mock.patch.object(ci.os,'geteuid',create=True,return_value=0), \
                mock.patch('secrets.token_urlsafe',return_value='inert-not-a-password'),contextlib.redirect_stderr(output):
            if stage:
                with self.assertRaises(ci.CIDenied) as caught:runner.bootstrap(binding,ticket)
                self.assertEqual(caught.exception.code,'target');self.event(output,stage)
            else:
                dsns=runner.bootstrap(binding,ticket)
                self.assertEqual(set(dsns),{'fixture','app','worker','owner'})
                self.assertEqual(output.getvalue(),'')
            connect.assert_called_once()
        if stage in ('database_name','database_version','database_address','database_port','database_empty'):
            self.assertLessEqual(conn.execute.call_count,2)
        return output

    def test_realistic_pg_inet_output_accepts_only_exact_bound_ipv4(self):
        # This fails at database_address under the original ::text query.
        self.bootstrap_probe()

    def test_unrelated_null_mapped_or_malformed_server_addresses_still_refused(self):
        for address in ('172.18.0.3','172.19.0.2','127.0.0.1','0.0.0.0','8.8.8.8',
                        '::1','fd00::2','::ffff:172.18.0.2',None,'',PAYLOAD,
                        '172.18.0.2/32','172.18.0.2/24','172.018.0.2','172.18.0.2 '):
            with self.subTest(address_type=type(address).__name__):
                self.bootstrap_probe('database_address',server_address=address)

    def test_ipv6_and_ambiguous_docker_targets_remain_outside_approved_ipv4_scope(self):
        for address in ('fd00::2','fd00:0:0:0:0:0:0:2','::ffff:172.18.0.2','::ffff:ac12:2'):
            info=TargetTests().info()
            info['NetworkSettings']['Networks']['inert-network']['IPAddress']=address
            with self.subTest(family='ipv6'),contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(ci.CIDenied):ci.validate_target(info,{})
        for kind in ('extra_network','wrong_port','wrong_interface'):
            info=TargetTests().info()
            if kind=='extra_network':info['NetworkSettings']['Networks']['other']={'IPAddress':'172.19.0.2'}
            elif kind=='wrong_port':info['NetworkSettings']['Ports']['5432/tcp'][0]['HostPort']='55433'
            else:info['NetworkSettings']['Ports']['5432/tcp'][0]['HostIp']='0.0.0.0'
            with self.subTest(kind=kind),contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(ci.CIDenied):ci.validate_target(info,{})

    def test_bootstrap_database_predicates_stop_before_migrations(self):
        for stage in ('database_name','database_version','database_address','database_port','database_empty'):
            with self.subTest(stage=stage):self.bootstrap_probe(stage)

    def test_bootstrap_role_predicates_remain_fail_closed_and_success_silent(self):
        for stage in ('role_attributes','role_membership'):
            with self.subTest(stage=stage):self.bootstrap_probe(stage)
        self.bootstrap_probe()

    def test_main_early_target_and_approval_failures_are_classified(self):
        identity={'run_id':'1234','sha':'a'*40,'attempt':'1'}
        for stage,container,infos in [('container_environment',PAYLOAD,[]),
                                      ('inspect_cardinality','a'*64,[]),
                                      ('inspect_cardinality','a'*64,[dict(TargetTests().info(),Id='b'*64)]),
                                      ('approval_binding','a'*64,[TargetTests().info()])]:
            output=io.StringIO();env={'BR02_CI_CONTAINER':container}
            with self.subTest(stage=stage),mock.patch.object(runner.sys,'argv',['run_br02_pg_ci.py']), \
                    mock.patch.object(runner.sys,'platform','linux'),mock.patch.object(runner.os,'name','posix'), \
                    mock.patch.object(runner.os,'geteuid',create=True,return_value=0),mock.patch.object(runner.os,'environ',env), \
                    mock.patch.object(ci,'authenticate',return_value=(identity,object())), \
                    mock.patch.object(runner,'command',side_effect=['a'*40,'',json.dumps(infos)]), \
                    mock.patch.object(runner,'bootstrap',return_value={}), \
                    mock.patch.object(ci,'make_approval',side_effect=ci.CIDenied('target')), \
                    mock.patch.object(runner,'isolated_worker') as worker,contextlib.redirect_stdout(output),contextlib.redirect_stderr(output):
                self.assertEqual(runner.main(),2);worker.assert_not_called();self.event(output,stage)


class IdentityTests(unittest.TestCase):
    def claims(self):
        return dict(iss=ci.ISSUER,aud=ci.AUDIENCE,repository=ci.REPOSITORY,repository_id=ci.REPOSITORY_ID,
            repository_owner_id=ci.OWNER_ID,actor_id=ci.OWNER_ID,event_name='workflow_dispatch',ref=ci.REF,
            workflow_ref=ci.WORKFLOW,runner_environment='github-hosted',repository_visibility='public',
            run_attempt='1',run_id='1234',sha='a'*40,workflow_sha='a'*40,exp=1200,iat=990,nbf=990,jti='inert-jti')
    def test_valid_signed_claims_logic_accepts_exact_binding(self):
        self.assertEqual(ci.validate_claims(self.claims(),{'run_id':'1234','sha':'a'*40},1000),
                         {'run_id':'1234','sha':'a'*40,'attempt':'1'})
    def test_missing_forged_or_inconsistent_claims_refused(self):
        for field in self.claims():
            value=self.claims(); value.pop(field)
            with self.subTest(field=field),self.assertRaises(ci.CIDenied):ci.validate_claims(value,{'run_id':'1234','sha':'a'*40},1000)
        for field in ('aud','repository','actor_id','workflow_ref','sha','run_attempt','runner_environment','repository_visibility'):
            value=self.claims();value[field]=PAYLOAD
            with self.assertRaises(ci.CIDenied) as caught:ci.validate_claims(value,{'run_id':'1234','sha':'a'*40},1000)
            self.assertNotIn(PAYLOAD,str(caught.exception))
    def test_stale_and_not_yet_valid_claims_refused(self):
        for now in (980,1200,10000):
            with self.assertRaises(ci.CIDenied):ci.validate_claims(self.claims(),{'run_id':'1234','sha':'a'*40},now)
    def test_environment_alone_is_never_authority(self):
        with self.assertRaises(ci.CIDenied):ci.authenticate({'GITHUB_ACTIONS':'true','GITHUB_SHA':'a'*40,'BR02_CI_APPROVED_SHA':'a'*40})
        self.assertFalse(ci.active());self.assertFalse(target.execution_authorized())
    def test_untrusted_oidc_request_origin_refused_before_network(self):
        env={'GITHUB_ACTIONS':'true','GITHUB_SHA':'a'*40,'BR02_CI_APPROVED_SHA':'a'*40,
             'ACTIONS_ID_TOKEN_REQUEST_URL':'https://untrusted.invalid/','ACTIONS_ID_TOKEN_REQUEST_TOKEN':PAYLOAD}
        with mock.patch.object(ci.urllib.request,'urlopen') as request:
            with self.assertRaises(ci.CIDenied):ci.authenticate(env)
            request.assert_not_called()
    def test_jwt_verification_pins_algorithm_issuer_audience_and_required_claims(self):
        import jwt
        fake=mock.Mock();fake.get_signing_key_from_jwt.return_value.key='inert-key'
        with mock.patch.object(jwt,'PyJWKClient',return_value=fake) as keys, \
                mock.patch.object(jwt,'decode',return_value=self.claims()) as decode:
            ci._verify_jwt('inert-token')
        keys.assert_called_once_with(ci.ISSUER+'/.well-known/jwks',timeout=10)
        self.assertEqual(decode.call_args.kwargs['algorithms'],['RS256'])
        self.assertEqual(decode.call_args.kwargs['audience'],ci.AUDIENCE)
        self.assertIn('run_id',decode.call_args.kwargs['options']['require'])
    def test_invalid_jwt_signature_is_not_accepted(self):
        import jwt
        fake=mock.Mock();fake.get_signing_key_from_jwt.return_value.key='inert-key'
        with mock.patch.object(jwt,'PyJWKClient',return_value=fake):
            with self.assertRaises(jwt.InvalidTokenError):ci._verify_jwt('invalid.jwt.payload')


class TargetTests(unittest.TestCase):
    def info(self):
        return {'Id':'a'*64,'Config':{'Image':ci.IMAGE},'State':{'Running':True},
            'NetworkSettings':{'Ports':{'5432/tcp':[{'HostIp':'127.0.0.1','HostPort':'55432'}]},
                               'Networks':{'inert-network':{'IPAddress':'172.18.0.2'}}},
            'Mounts':[{'Type':'volume','Destination':'/var/lib/postgresql/data','Name':'b'*64}]}
    def binding(self):return ci.validate_target(self.info(),{'run_id':'1234','sha':'a'*40,'attempt':'1'})
    def test_exact_owned_target_metadata_accepted(self):
        value=self.binding();self.assertEqual(value['endpoint'],'127.0.0.1:55432')
        self.assertEqual(value['server_address'],'172.18.0.2')
    def test_public_binding_wrong_image_storage_and_network_refused(self):
        for change in ('public','image','mount','network','not_running'):
            value=self.info()
            if change=='public':value['NetworkSettings']['Ports']['5432/tcp'][0]['HostIp']='0.0.0.0'
            if change=='image':value['Config']['Image']='postgres:latest'
            if change=='mount':value['Mounts'][0]['Type']='bind'
            if change=='network':value['NetworkSettings']['Networks']['inert-network']['IPAddress']='8.8.8.8'
            if change=='not_running':value['State']['Running']=False
            with self.assertRaises(ci.CIDenied):ci.validate_target(value,{'run_id':'1234','sha':'a'*40,'attempt':'1'})
    def test_manifest_and_approval_bind_run_storage_roles(self):
        binding=self.binding();approval=ci.make_approval(binding,{'fixture':'inert-admin','app':'inert-app','worker':'inert-worker'})
        self.assertEqual(approval.run_marker,'1234')
        self.assertEqual(approval.approved_manifest.binding['storage'],'b'*64)
        self.assertEqual(approval.approved_manifest.binding['role_inventory'],['marker_app_login','marker_worker_login'])
    def test_forged_and_expired_issuer_tickets_refused(self):
        binding=self.binding();approval=ci.make_approval(binding,{'fixture':'inert-admin','app':'inert-app','worker':'inert-worker'})
        with self.assertRaises(ci.CIDenied):
            with ci.installed(approval,binding,ticket=object()):pass
        _,ticket=issue_ticket()
        with mock.patch.object(ci.time,'monotonic',return_value=time.monotonic()+301):
            with self.assertRaises(ci.CIDenied):
                with ci.installed(approval,binding,ticket=ticket):pass
    def test_valid_controlled_issuer_context_and_exact_approval_only(self):
        with issued_context() as context:
            ci.authorize(context.approval);self.assertTrue(target.execution_authorized())
            with self.assertRaises(ci.CIDenied):ci.authorize(object())
            with self.assertRaises(ci.CIDenied):ci.authorize(None)
        self.assertFalse(target.execution_authorized())
    def test_expired_context_and_stage2_authority_stay_closed(self):
        token=ci._current.set(ci.Context(object(),{},-1))
        try:self.assertFalse(target.execution_authorized())
        finally:ci._current.reset(token)
        from tests import br02_pg_authorization as auth
        with self.assertRaises(auth.AuthorizationDenied):auth._trusted_authority()
    def test_unissued_context_object_cannot_open_gate(self):
        token=ci._current.set(ci.Context(object(),{},time.monotonic()+60))
        try:
            self.assertFalse(target.execution_authorized())
            with self.assertRaises(ci.CIDenied):ci.authorize(object())
        finally:ci._current.reset(token)
    def test_unissued_ticket_cannot_generate_credentials_or_bootstrap(self):
        import secrets
        with mock.patch.object(secrets,'token_urlsafe') as generation:
            with self.assertRaises(ci.CIDenied):runner.bootstrap(self.binding(),object())
            generation.assert_not_called()
    def test_active_ci_context_cannot_call_stage2_catalog(self):
        import br02_pg_authorization
        connection=mock.Mock()
        with issued_context() as context:
            with self.assertRaises(target.TargetRefused):target.stage2_run_catalog_comparison(connection,context.approval,context.approval.approved_manifest)
            connection.execute.assert_not_called()
    def test_import_identities_are_one_authority(self):
        self.assertIs(importlib.import_module('br02_pg_target'),importlib.import_module('tests.br02_pg_target'))


class ChildTests(unittest.TestCase):
    def test_missing_child_capability_rejected(self):
        with self.assertRaises(ci.CIDenied):ci.adopt_child({})
    def test_capability_has_no_bootstrap_transport_and_uses_readonly_fd(self):
        with issued_context() as context:
            stream=mock.MagicMock()
            with mock.patch.object(ci.os,'dup',return_value=18) as duplicate,mock.patch.object(ci.os,'fdopen',return_value=stream) as opened:
                result,env=ci.child_transport(context.approval)
            duplicate.assert_called_once_with(17);opened.assert_called_once_with(18,'rb')
            self.assertEqual(env,{'BR02_CI_FD':'18','BR02_CI_RUN_ID':'1234','BR02_CI_SHA':'a'*40,'BR02_CI_ATTEMPT':'1'});self.assertNotIn('inert-admin',repr(env))
    def test_process_fixture_uses_real_approval_marker_not_placeholder(self):
        source=(ROOT/'tests/test_br02_pg_processes.py').read_text()
        segment=source[source.index('class _LiveProcessesBase'):]
        self.assertIn('self.approval = _live_approval(self.dsn)',segment)
        self.assertNotIn('synthetic_approved_manifest()',segment)
    def test_child_accepts_only_issuer_owned_readonly_unlinked_capability(self):
        import stat
        binding=TargetTests().binding();key=b'K'*32
        raw=json.dumps({'binding':binding,'app':'inert-app','expires':time.monotonic()+60},sort_keys=True).encode()
        payload=key+hmac.digest(key,raw,'sha256')+raw
        fcntl=SimpleNamespace(F_GETFL=3,fcntl=lambda *args:os.O_RDONLY)
        previous=ci._current.get()
        try:
            with mock.patch.dict(sys.modules,{'fcntl':fcntl}),mock.patch.object(ci.os,'name','posix'), \
                 mock.patch.object(ci.os,'O_ACCMODE',3,create=True), \
                 mock.patch.object(ci.os,'getuid',create=True,return_value=60001), \
                 mock.patch.object(ci.os,'fstat',return_value=SimpleNamespace(st_uid=0,st_nlink=0,st_mode=stat.S_IFREG|0o600)), \
                 mock.patch.object(ci.os,'pread',create=True,return_value=payload),mock.patch.object(ci.os,'close') as close:
                ci.adopt_child(child_env());ci.authorize(None)
                self.assertTrue(ci._current.get().child)
                self.assertEqual(ci._current.get().approval.target_dsn,'bootstrap-not-forwarded')
                close.assert_called_once_with(17)
        finally:
            ci._current.set(previous)
    def test_child_rejects_tampered_capability_and_closes_handle(self):
        import stat
        with mock.patch.dict(sys.modules,{'fcntl':SimpleNamespace(F_GETFL=3,fcntl=lambda *args:os.O_RDONLY)}), \
             mock.patch.object(ci.os,'O_ACCMODE',3,create=True), \
             mock.patch.object(ci.os,'fstat',return_value=SimpleNamespace(st_uid=0,st_nlink=0,st_mode=stat.S_IFREG|0o600)), \
             mock.patch.object(ci.os,'pread',create=True,return_value=b'X'*100),mock.patch.object(ci.os,'close') as close:
            with self.assertRaises(ci.CIDenied):ci.adopt_child({'BR02_CI_FD':'17'})
            close.assert_called_once_with(17)


class ProvenanceRegressionTests(unittest.TestCase):
    def test_construct_register_and_select_cannot_authorize_either_import(self):
        binding=TargetTests().binding();approval=ci.make_approval(binding,{'fixture':'inert-admin','app':'inert-app','worker':'inert-worker'})
        forged=ci.Context(approval,binding,time.monotonic()+60)
        with self.assertRaises(ci.CIDenied):ci._register(forged)
        # Even a caller-written legacy registry with the correct fingerprint
        # has no relationship to issuer-owned state.
        with mock.patch.object(ci,'_live',{id(forged):(forged,ci._fingerprint(forged))},create=True):
            token=ci._current.set(forged)
            try:
                for module in ('br02_pg_target','tests.br02_pg_target'):
                    gate=importlib.import_module(module)
                    self.assertFalse(gate.execution_authorized())
                    with self.assertRaises(gate.TargetRefused):gate.authorize_execution(approval)
                with self.assertRaises(ci.CIDenied):
                    with ci.fixture_context(approval):pass
                with self.assertRaises(ci.CIDenied):ci.child_transport(approval)
            finally:ci._current.reset(token)
    def test_forged_child_context_selection_cannot_authorize_without_issuance(self):
        binding=TargetTests().binding();approval=ci.make_approval(binding,{'fixture':'inert-admin','app':'inert-app','worker':'inert-worker'})
        context=ci.Context(approval,binding,time.monotonic()+60,True)
        token=ci._current.set(context)
        try:
            with self.assertRaises(ci.CIDenied):ci._register(context)
            self.assertFalse(ci.active())
            with self.assertRaises(target.TargetRefused):target.authorize_execution(None)
        finally:ci._current.reset(token)
    def test_fabricated_legacy_ticket_registry_is_not_provenance(self):
        binding=TargetTests().binding();fake=object()
        with mock.patch.object(ci,'_issued',{fake:({'run_id':'1234','sha':'a'*40,'attempt':'1'},time.monotonic()+60)},create=True):
            with self.assertRaises(ci.CIDenied):ci.require_ticket(fake,binding)
    def test_missing_and_invalid_identity_cannot_issue(self):
        for change in ({'jti':None},{'run_attempt':'2'},{'workflow_ref':'wrong'},{'repository':'other'},{'sha':'b'*40}):
            claims=IdentityTests().claims();now=int(time.time())
            claims.update(iat=now-1,nbf=now-1,exp=now+60,jti='invalid-'+str(next(_jtis)));claims.update(change)
            with self.assertRaises(ci.CIDenied):issue_ticket(claims=claims)
        response=mock.Mock();response.open.return_value=TokenResponse(b'{"value":"inert"}')
        with mock.patch.object(ci.urllib.request,'build_opener',return_value=response), \
                mock.patch.object(ci,'_verify_jwt',side_effect=ValueError(PAYLOAD)):
            with self.assertRaises(ci.CIDenied) as caught:
                ci.authenticate({'GITHUB_ACTIONS':'true','GITHUB_SHA':'a'*40,'BR02_CI_APPROVED_SHA':'a'*40,
                                 'GITHUB_RUN_ID':'1234','ACTIONS_ID_TOKEN_REQUEST_URL':'https://vstoken.actions.githubusercontent.com/inert',
                                 'ACTIONS_ID_TOKEN_REQUEST_TOKEN':'inert-not-a-token'})
        self.assertNotIn(PAYLOAD,str(caught.exception))
    def test_authenticated_issuance_accepts_and_consumes_ticket(self):
        _,ticket=issue_ticket()
        with issued_context(ticket=ticket) as context:
            self.assertTrue(target.execution_authorized());target.authorize_execution(context.approval)
        self.assertFalse(ci.active())
        with self.assertRaises(ci.CIDenied):
            with issued_context(ticket=ticket):pass
    def test_issued_context_expiry_and_exception_cleanup_keep_gate_closed(self):
        class OperationFailure(Exception):pass
        with self.assertRaises(OperationFailure):
            with issued_context(seconds=1) as context:
                self.assertTrue(ci.active())
                with mock.patch.object(ci.time,'monotonic',return_value=context.expires+1):
                    self.assertFalse(ci.active())
                    with self.assertRaises(target.TargetRefused):target.authorize_execution(context.approval)
                raise OperationFailure()
        self.assertFalse(ci.active())
    def test_duplicate_signed_jti_is_not_reissued(self):
        now=int(time.time());claims=IdentityTests().claims()
        claims.update(iat=now-1,nbf=now-1,exp=now+60,jti='duplicate-'+str(next(_jtis)))
        issue_ticket(claims=claims)
        with self.assertRaises(ci.CIDenied):issue_ticket(claims=claims)
    def test_cross_run_attempt_commit_and_target_ticket_replay_refused(self):
        _,ticket=issue_ticket();binding=TargetTests().binding()
        with mock.patch.object(ci.os,'name','posix'),mock.patch.object(ci.os,'geteuid',create=True,return_value=0):
            ci.require_ticket(ticket,binding)
            for key,value in (('run_id','5678'),('attempt','2'),('sha','b'*40),('instance','c'*64),
                              ('storage','c'*64),('server_address','172.18.0.3'),('database','other'),
                              ('endpoint','127.0.0.1:5432'),('repository','other'),('workflow','other'),('scope','stage2')):
                with self.subTest(key=key),self.assertRaises(ci.CIDenied):ci.require_ticket(ticket,dict(binding,**{key:value}))
    def test_returned_identity_mutation_does_not_mutate_ticket(self):
        identity,ticket=issue_ticket();identity['run_id']='5678'
        binding=TargetTests().binding();binding['run_id']='5678'
        with self.assertRaises(ci.CIDenied):ci.require_ticket(ticket,binding)
    def test_mutation_and_fabricated_replacement_context_are_refused(self):
        with issued_context() as context:
            clone=ci.Context(context.approval,dict(context.binding),context.expires,False,context.capability_fd)
            token=ci._current.set(clone)
            try:self.assertFalse(ci.active())
            finally:ci._current.reset(token)
            context.binding['attempt']='2'
            self.assertFalse(ci.active())
    def test_invalid_approval_and_duration_do_not_create_transport(self):
        from dataclasses import replace
        with issued_context() as parent:
            for approval in (replace(parent.approval,rollback_drill_authorized=True),replace(parent.approval,app_dsn='other')):
                with self.assertRaises(ci.CIDenied):
                    with ci.fixture_context(approval):pass
        for seconds in (0,-1,float('nan'),float('inf'),481):
            with self.subTest(seconds=seconds),self.assertRaises(ci.CIDenied):
                with issued_context(seconds=seconds):pass
    def test_descriptor_failure_cannot_replay_consumed_ticket(self):
        _,ticket=issue_ticket()
        with mock.patch.object(ci.os,'open',side_effect=OSError(PAYLOAD)):
            binding=TargetTests().binding();approval=ci.make_approval(binding,{'fixture':'inert-admin','app':'inert-app','worker':'inert-worker'})
            with mock.patch.object(ci.os,'name','posix'),mock.patch.object(ci.os,'geteuid',create=True,return_value=0), \
                    mock.patch.object(tempfile,'TemporaryFile',return_value=mock.MagicMock()), \
                    mock.patch('secrets.token_bytes',return_value=b'K'*32):
                with self.assertRaises(ci.CIDenied) as caught:
                    with ci.installed(approval,binding,ticket=ticket):pass
        self.assertNotIn(PAYLOAD,str(caught.exception))
        with self.assertRaises(ci.CIDenied):ci.require_ticket(ticket,binding)
    def test_valid_parent_derived_fixture_retains_issuer_provenance(self):
        with issued_context() as parent:
            approval=ci.make_approval(parent.binding,{'fixture':'inert-owner','app':'inert-app','worker':'inert-worker'})
            with ci.fixture_context(approval):
                target.authorize_execution(approval);self.assertFalse(ci._current.get().child)
            target.authorize_execution(parent.approval)
    def test_valid_inherited_child_remains_app_only_and_cannot_issue_children(self):
        previous=ci._current.get()
        try:
            with child_native(child_payload()):
                ci.adopt_child(child_env());target.authorize_execution(None)
                approval=ci._current.get().approval
                self.assertEqual(approval.app_dsn,'inert-app')
                self.assertEqual(approval.target_dsn,'bootstrap-not-forwarded');self.assertEqual(approval.worker_dsn,'')
                with self.assertRaises(ci.CIDenied):ci.child_transport(approval)
                with self.assertRaises(ci.CIDenied):
                    with ci.fixture_context(approval):pass
        finally:ci._current.set(previous)
    def test_forged_self_signed_bytes_do_not_replace_os_issuer_provenance(self):
        # A caller can calculate this HMAC. Only a root-owned anonymous read-only
        # inherited descriptor is provenance; the HMAC alone is not authority.
        for attrs in ({'uid':60001},{'links':1},{'access':os.O_RDWR},{'mode':0}):
            with self.subTest(attrs=attrs),child_native(child_payload(),**attrs),self.assertRaises(ci.CIDenied):
                ci.adopt_child(child_env())
        self.assertFalse(ci.active())
    def test_child_cross_run_attempt_and_commit_mismatches_refused(self):
        for key,value in (('BR02_CI_RUN_ID','5678'),('BR02_CI_ATTEMPT','2'),('BR02_CI_SHA','b'*40)):
            with self.subTest(key=key),child_native(child_payload()),self.assertRaises(ci.CIDenied):
                ci.adopt_child(child_env(**{key:value}))
        for key,value in (('attempt','2'),('scope','stage2'),('repository','other'),('database','other')):
            binding=TargetTests().binding();binding[key]=value
            with self.subTest(key=key),child_native(child_payload(binding)),self.assertRaises(ci.CIDenied):ci.adopt_child(child_env())
    def test_child_stale_malformed_and_native_failure_refused(self):
        for payload in (child_payload(expires=time.monotonic()-1),child_payload(expires=float('nan')),
                        child_payload(app=''),b'X'*100):
            with child_native(payload),self.assertRaises(ci.CIDenied):ci.adopt_child(child_env())
        with child_native(child_payload()) as close,mock.patch.object(ci.os,'pread',side_effect=OSError(PAYLOAD)):
            with self.assertRaises(ci.CIDenied) as caught:ci.adopt_child(child_env())
            close.assert_called_once_with(17);self.assertNotIn(PAYLOAD,str(caught.exception))
    def test_context_import_spellings_share_the_issuer_registry(self):
        self.assertIs(importlib.import_module('br02_pg_ci_context'),importlib.import_module('tests.br02_pg_ci_context'))
        with issued_context() as context:
            importlib.import_module('br02_pg_ci_context').authorize(context.approval)
            for module in ('br02_pg_target','tests.br02_pg_target'):
                importlib.import_module(module).authorize_execution(context.approval)
    def test_dsn_alone_never_grants_execution(self):
        with mock.patch.dict(os.environ,{'AI_MARKER_TEST_PG_DSN':'inert-app','GITHUB_ACTIONS':'true'}):
            self.assertFalse(target.execution_authorized())
            with self.assertRaises(target.TargetRefused):target.authorize_execution(None)


class InventoryTests(unittest.TestCase):
    def test_exact_inventory_has_fifty_selected_methods_and_two_explicit_exclusions(self):
        names={module+'.'+cls+'.'+name for module,classes in runner.TESTS.items() for cls,methods in classes.items() for name in methods}
        self.assertEqual(len(names),52);self.assertEqual(len(names-set(runner.EXCLUDED)),50)
        self.assertTrue(set(runner.EXCLUDED)<=names)
    def test_actual_existing_classes_match_the_pinned_inventory_without_running_them(self):
        modules={name:importlib.import_module(name) for name in runner.TESTS}
        selected=runner.build_suite(modules,runner.TESTS)
        self.assertEqual(len(selected),50)
        self.assertFalse(set(test.id() for test in selected)&set(runner.EXCLUDED))
    def test_missing_or_added_method_or_empty_suite_is_failure(self):
        class Example(unittest.TestCase):
            def test_one(self):pass
        modules={'inert':SimpleNamespace(Example=Example)}
        self.assertEqual(len(runner.build_suite(modules,{'inert':{'Example':['test_one']}})),1)
        for inventory in ({},{'inert':{'Example':['test_missing']}},{'inert':{'Missing':['test_one']}}):
            with self.assertRaises(ci.CIDenied):runner.build_suite(modules,inventory)
    def test_unexpected_skip_missing_id_and_raw_exception_do_not_pass_or_leak(self):
        class Example(unittest.TestCase):
            def test_one(self):pass
        test=Example('test_one');result=runner.StrictResult()
        result.addSkip(test,PAYLOAD)
        self.assertFalse(result.passed([test.id()]));self.assertNotIn(PAYLOAD,repr(result.skipped))
        result=runner.StrictResult();result.addSuccess(test)
        self.assertFalse(result.passed([test.id(),'missing']))
        result.addError(test,(ValueError,ValueError(PAYLOAD),None));self.assertNotIn(PAYLOAD,repr(result.errors))
    def test_migration_order_and_sql_role_privileges(self):
        self.assertEqual(len(ci.MIGRATIONS),9)
        self.assertEqual(ci.MIGRATIONS[0],'postgres_v1.sql');self.assertEqual(ci.MIGRATIONS[-1],'postgres_v9_result_context.sql')
        sql=(ROOT/'tests/br02_pg_ci_setup.sql').read_text().lower()
        self.assertIn('nobypassrls',sql);self.assertIn('nocreat erole'.replace(' ',''),sql)
        self.assertNotIn('password ',sql);self.assertNotIn('disable row level security',sql)
    def test_runtime_probe_keeps_restricted_login_after_fixture_seed(self):
        class Probe(unittest.TestCase):
            def setUp(self):self.dsn='inert-admin';self.seeded_as=self.dsn
            def test_runtime_role_cannot_update_or_delete_context(self):pass
        test=Probe('test_runtime_role_cannot_update_or_delete_context')
        runner.runtime_probe_fixture(test,'inert-app').setUp()
        self.assertEqual(test.seeded_as,'inert-admin');self.assertEqual(test.dsn,'inert-app')
    def test_workflow_registration_never_auto_authorizes_and_has_no_secrets_or_uploads(self):
        # JSON is valid YAML; the workflow uses that subset so this check needs
        # no new parser dependency in GitHub's existing pinned test environment.
        value=json.loads((ROOT/'.github/workflows/br02-postgres-integration.yml').read_text())
        self.assertEqual(set(value['on']),{'workflow_dispatch'})
        self.assertIs(value['on']['workflow_dispatch']['inputs']['approve_execution']['default'],False)
        job=value['jobs']['integration'];self.assertEqual(job['runs-on'],'ubuntu-24.04')
        self.assertIn('github.event.repository.private == false',job['if'])
        self.assertEqual(job['timeout-minutes'],15);self.assertEqual(job['permissions'],{'contents':'read','id-token':'write'})
        text=json.dumps(value);self.assertNotIn('secrets.',text);self.assertNotIn('upload-artifact',text)
        self.assertNotIn('matrix',job);self.assertIn(ci.IMAGE,text)


class IsolationTests(unittest.TestCase):
    def isolated(self, fail_cleanup=False):
        import signal
        def no_user(uid):raise KeyError(uid)
        calls=[]
        def command(*args):
            calls.append(args)
            if fail_cleanup and args[:3]==('/usr/sbin/iptables','-D','OUTPUT'):raise ci.CIDenied('isolation')
            return ''
        with mock.patch.dict(sys.modules,{'pwd':SimpleNamespace(getpwuid=no_user)}), \
             mock.patch.object(ci,'active',return_value=True), \
             mock.patch.object(ci,'_current',SimpleNamespace(get=lambda:SimpleNamespace(expires=time.monotonic()+480))), \
             mock.patch.object(Path,'iterdir',return_value=[]),mock.patch.object(runner,'command',side_effect=command), \
             mock.patch.object(os,'fork',create=True,return_value=43210), \
             mock.patch.object(os,'waitpid',create=True,return_value=(43210,0)), \
             mock.patch.object(os,'killpg',create=True,side_effect=ProcessLookupError) as kill, \
             mock.patch.object(signal,'SIGKILL',create=True,new=9), \
             mock.patch.object(os,'WNOHANG',create=True,new=1):
            if fail_cleanup:
                with self.assertRaises(ci.CIDenied):runner.isolated_worker({'run_id':'1234','server_address':'172.18.0.2'}, {})
            else:self.assertEqual(runner.isolated_worker({'run_id':'1234','server_address':'172.18.0.2'}, {}),0)
            kill.assert_called_once()
            self.assertEqual(kill.call_args.args[0],43210)
        return calls
    def test_egress_rules_apply_only_reserved_uid_and_owned_process_group(self):
        calls=self.isolated()
        self.assertIn(('/usr/sbin/iptables','-I','OUTPUT','1','-m','owner','--uid-owner','60001','-j','BR02_1234'),calls)
        self.assertIn(('/usr/sbin/ip6tables','-I','OUTPUT','1','-m','owner','--uid-owner','60001','-j','REJECT'),calls)
        self.assertEqual(calls[-1],('/usr/sbin/iptables','-X','BR02_1234'))
    def test_failed_rule_removal_does_not_skip_remaining_cleanup(self):
        calls=self.isolated(True)
        self.assertEqual(calls[-2:], [('/usr/sbin/iptables','-F','BR02_1234'),('/usr/sbin/iptables','-X','BR02_1234')])


class IdentityDiagnosticProposalTests(unittest.TestCase):
    def env(self):
        return {'GITHUB_ACTIONS':'true','GITHUB_SHA':'a'*40,'BR02_CI_APPROVED_SHA':'a'*40,
                'GITHUB_RUN_ID':'1234','ACTIONS_ID_TOKEN_REQUEST_URL':'https://vstoken.actions.githubusercontent.com/inert',
                'ACTIONS_ID_TOKEN_REQUEST_TOKEN':PAYLOAD}

    def failure(self,stage,*,env=None,raw=b'{"value":"synthetic.jwt.token"}',claims=None,error=None):
        opener=mock.Mock();opener.open.return_value=TokenResponse(raw)
        if stage=='token_acquisition':opener.open.side_effect=OSError(PAYLOAD)
        value=IdentityTests().claims() if claims is None else claims
        output=io.StringIO()
        with mock.patch.object(ci.urllib.request,'build_opener',return_value=opener), \
                mock.patch.object(ci,'_verify_jwt',side_effect=error,return_value=value), \
                mock.patch.object(ci.time,'time',return_value=1000),contextlib.redirect_stderr(output):
            with self.assertRaises(ci.CIDenied) as caught:ci._authenticated_identity(self.env() if env is None else env)
        event=json.loads(output.getvalue().split('BR02_CI_IDENTITY_DIAGNOSTIC ',1)[1])
        self.assertEqual(event['stage'],stage)
        self.assertEqual(set(event),{'stage','reason','checks'} if stage=='claim_binding' else
                         {'stage','reason','url_checks','hostname_observation'} if stage=='request_url' else {'stage','reason'})
        self.assertNotIn(PAYLOAD,output.getvalue()+str(caught.exception))
        self.assertNotIn('synthetic.jwt.token',output.getvalue())
        self.assertFalse(ci.active())
        return event

    def test_environment_and_missing_request_values_are_distinguished(self):
        env=self.env();env['GITHUB_ACTIONS']='false';self.failure('environment',env=env)
        for field,stage in [('ACTIONS_ID_TOKEN_REQUEST_URL','request_url'),('ACTIONS_ID_TOKEN_REQUEST_TOKEN','request_environment')]:
            env=self.env();env.pop(field);self.assertEqual(self.failure(stage,env=env)['reason'],'missing_field')

    def test_untrusted_origin_and_transport_failure_remain_refused(self):
        env=self.env();env['ACTIONS_ID_TOKEN_REQUEST_URL']='https://untrusted.invalid/?secret='+PAYLOAD
        self.failure('request_url',env=env);self.failure('token_acquisition')

    def test_malformed_response_never_reaches_claim_binding(self):
        for raw in (b'not-json',b'{}',b'X'*32769):
            with self.subTest(raw_length=len(raw)):self.failure('token_response',raw=raw)

    def test_signature_issuer_audience_and_malformed_jwt_are_distinct(self):
        import jwt
        for kind,label in [(jwt.InvalidSignatureError,'signature'),(jwt.InvalidIssuerError,'issuer'),
                           (jwt.InvalidAudienceError,'audience'),(jwt.DecodeError,'invalid_token'),
                           (jwt.PyJWKClientError,'signing_key')]:
            with self.subTest(reason=label):
                self.assertEqual(self.failure('jwt_verification',error=kind(PAYLOAD))['reason'],label)

    def test_every_bound_identity_mismatch_still_rejects(self):
        for field in ('repository','repository_id','repository_owner_id','actor_id','event_name','ref',
                      'workflow_ref','workflow_sha','sha','run_id','run_attempt','runner_environment','repository_visibility'):
            value=IdentityTests().claims();value[field]=PAYLOAD
            with self.subTest(field=field):
                event=self.failure('claim_binding',claims=value)
                self.assertTrue(any(flag is False for flag in event['checks'].values()))

    def test_registration_commit_cannot_replace_feature_workflow_identity(self):
        value=IdentityTests().claims();value['workflow_sha']='dde3979b0c20657263f5d07f9bebcd6cffbaae67'
        self.failure('claim_binding',claims=value)
        value=IdentityTests().claims();value['workflow_ref']=ci.REPOSITORY+'/.github/workflows/br02-postgres-integration.yml@refs/heads/main'
        self.failure('claim_binding',claims=value)

    def test_valid_feature_identity_has_no_diagnostic_and_same_return(self):
        opener=mock.Mock();opener.open.return_value=TokenResponse(b'{"value":"synthetic.jwt.token"}')
        output=io.StringIO();claims=IdentityTests().claims()
        with mock.patch.object(ci.urllib.request,'build_opener',return_value=opener), \
                mock.patch.object(ci,'_verify_jwt',return_value=claims), \
                mock.patch.object(ci.time,'time',return_value=1000),contextlib.redirect_stderr(output):
            identity,jti=ci._authenticated_identity(self.env())
        self.assertEqual(identity,{'run_id':'1234','sha':'a'*40,'attempt':'1'})
        self.assertEqual(jti,'inert-jti');self.assertEqual(output.getvalue(),'')

    def test_diagnostic_sink_failure_does_not_issue_authority(self):
        env=self.env();env['GITHUB_ACTIONS']='false'
        with mock.patch('builtins.print',side_effect=OSError(PAYLOAD)):
            with self.assertRaises(ci.CIDenied):ci.authenticate(env)
        self.assertFalse(ci.active())


class RequestUrlPredicateTests(unittest.TestCase):
    def test_rotating_documented_service_endpoints_reach_signed_verification(self):
        for host in ('run-actions-3-azure-eastus', 'run-actions-99-other-region', 'future-service'):
            with self.subTest(host=host):
                issue_ticket(environ={'ACTIONS_ID_TOKEN_REQUEST_URL':
                    'https://'+host+'.actions.githubusercontent.com/inert'})

    fields={'url_parsed','scheme_allowed','hostname_allowed','username_allowed',
            'password_allowed','port_parsed','port_allowed','query_parsed',
            'audience_query_built','url_built','input_allowed','authority_allowed','fragment_absent'}

    def refused(self,url,**changes):
        env=IdentityDiagnosticProposalTests().env()
        env['ACTIONS_ID_TOKEN_REQUEST_URL']=url;env.update(changes)
        output=io.StringIO()
        with mock.patch.object(ci.urllib.request,'build_opener') as opener, \
                mock.patch.object(ci,'_verify_jwt') as verify,contextlib.redirect_stderr(output):
            with self.assertRaises(ci.CIDenied) as caught:ci.authenticate(env)
        self.assertEqual(str(caught.exception),'BR02_CI_IDENTITY')
        opener.assert_not_called();verify.assert_not_called()
        event=json.loads(output.getvalue().split('BR02_CI_IDENTITY_DIAGNOSTIC ',1)[1])
        self.assertEqual(set(event),{'stage','reason','url_checks','hostname_observation'})
        self.assertEqual(event['stage'],'request_url')
        self.assertEqual(set(event['url_checks']),self.fields)
        self.assertTrue(all(type(v) is bool for v in event['url_checks'].values()))
        self.assertNotIn(PAYLOAD,output.getvalue()+str(caught.exception))
        self.assertFalse(ci.active())
        return event['url_checks']

    def test_accepted_urls_preserve_existing_origin_path_and_query_behavior(self):
        for authority in ('vstoken.actions.githubusercontent.com',
                          'VSTOKEN.ACTIONS.GITHUBUSERCONTENT.COM:443',
                          'run-actions-1-azure-eastus.actions.githubusercontent.com',
                          'RUN-ACTIONS-1-AZURE-EASTUS.ACTIONS.GITHUBUSERCONTENT.COM:443',
                          'run-actions-2-azure-eastus.actions.githubusercontent.com',
                          'RUN-ACTIONS-2-AZURE-EASTUS.ACTIONS.GITHUBUSERCONTENT.COM:443',
                          'run-actions-3-azure-eastus.actions.githubusercontent.com',
                          'future.actions.githubusercontent.com'):
            url='https://'+authority+'/synthetic/'+PAYLOAD+'?keep=one&blank=&dup=1&dup=2&audience=old&audience=second&%61udience=third'
            env=IdentityDiagnosticProposalTests().env();env['ACTIONS_ID_TOKEN_REQUEST_URL']=url
            opener=mock.Mock();opener.open.return_value=TokenResponse(b'{"value":"inert"}')
            output=io.StringIO()
            with mock.patch.object(ci.urllib.request,'build_opener',return_value=opener), \
                    mock.patch.object(ci,'_verify_jwt',return_value=IdentityTests().claims()), \
                    mock.patch.object(ci.time,'time',return_value=1000),contextlib.redirect_stderr(output):
                identity,_=ci._authenticated_identity(env)
            self.assertEqual(identity,{'run_id':'1234','sha':'a'*40,'attempt':'1'})
            request=opener.open.call_args.args[0];address=ci.urlsplit(request.full_url)
            self.assertEqual(address.path,'/synthetic/'+PAYLOAD)
            self.assertEqual(ci.parse_qsl(address.query,keep_blank_values=True),
                [('keep','one'),('blank',''),('dup','1'),('dup','2'),('audience',ci.AUDIENCE)])
            self.assertEqual(address.fragment,'');self.assertEqual(output.getvalue(),'')
            self.assertTrue(all(ci._request_url_checks(url).values()))

    def test_documented_namespace_rejects_lookalikes_and_domain_confusion(self):
        host='run-actions-2-azure-eastus.actions.githubusercontent.com'
        self.assertEqual(ci.REQUEST_DOMAIN,'actions.githubusercontent.com')
        for authority in (host+'.attacker.invalid','actions.githubusercontent.com',
                          host.replace('githubusercontent','githubusercontenT-attacker'),
                          host+'.',host+'%2eattacker.invalid','',
                          'vstoken.actions.githubusercontent.com.attacker.invalid'):
            with self.subTest(authority=authority):
                flags=self.refused('https://'+authority+'/?opaque='+PAYLOAD)
                self.assertFalse(flags['hostname_allowed'])

    def test_verified_host_preserves_transport_and_malformed_url_refusals(self):
        host='run-actions-2-azure-eastus.actions.githubusercontent.com'
        for url,field in (('http://'+host+'/','scheme_allowed'),
                          ('https://'+PAYLOAD+'@'+host+'/','username_allowed'),
                          ('https://:'+PAYLOAD+'@'+host+'/','password_allowed'),
                          ('https://'+host+':8443/','port_allowed'),
                          ('https://'+host+':invalid/','port_parsed'),
                          ('https://['+host,'url_parsed')):
            with self.subTest(field=field):self.assertFalse(self.refused(url)[field])

    def test_run_actions_1_refuses_lookalikes_and_other_url_failures(self):
        host='run-actions-1-azure-eastus.actions.githubusercontent.com'
        for authority in (host+'.attacker.invalid','notactions.githubusercontent.com',
                          host.replace('githubusercontent','githubusercontent-attacker'),
                          host+'.',host+'%2eattacker.invalid','arbitrary.invalid'):
            with self.subTest(authority=authority):
                self.assertFalse(self.refused('https://'+authority+'/')['hostname_allowed'])
        for url,field in (('http://'+host+'/','scheme_allowed'),
                          ('https://'+PAYLOAD+'@'+host+'/','username_allowed'),
                          ('https://:'+PAYLOAD+'@'+host+'/','password_allowed'),
                          ('https://'+host+':8443/','port_allowed'),
                          ('https://'+host+':bad/','port_parsed'),
                          ('https://['+host,'url_parsed')):
            with self.subTest(field=field):self.assertFalse(self.refused(url)[field])
        for name,field in (('parse_qsl','query_parsed'),('urlencode','audience_query_built'),('urlunsplit','url_built')):
            with self.subTest(operation=name),mock.patch.object(ci,name,side_effect=ValueError(PAYLOAD)):
                flags=self.refused('https://'+host+'/?opaque='+PAYLOAD)
                self.assertTrue(flags['hostname_allowed']);self.assertFalse(flags[field])

    def test_run_actions_1_does_not_bypass_signed_identity_or_claim_binding(self):
        import jwt
        helper=IdentityDiagnosticProposalTests();env=helper.env()
        env['ACTIONS_ID_TOKEN_REQUEST_URL']='https://run-actions-1-azure-eastus.actions.githubusercontent.com/inert'
        for kind,label in ((jwt.InvalidSignatureError,'signature'),(jwt.InvalidIssuerError,'issuer'),
                           (jwt.InvalidAudienceError,'audience')):
            with self.subTest(reason=label):
                self.assertEqual(helper.failure('jwt_verification',env=env,error=kind(PAYLOAD))['reason'],label)
        for field in ('iss','aud','repository','repository_id','repository_owner_id','actor_id','event_name',
                      'ref','workflow_ref','workflow_sha','sha','run_id','run_attempt','runner_environment','repository_visibility'):
            claims=IdentityTests().claims();claims[field]=PAYLOAD
            with self.subTest(claim=field):helper.failure('claim_binding',env=env,claims=claims)

    def test_verified_host_preserves_query_construction_failure_refusals(self):
        for name,field in (('parse_qsl','query_parsed'),('urlencode','audience_query_built'),('urlunsplit','url_built')):
            with self.subTest(operation=name),mock.patch.object(ci,name,side_effect=ValueError(PAYLOAD)):
                flags=self.refused('https://run-actions-2-azure-eastus.actions.githubusercontent.com/?opaque='+PAYLOAD)
                self.assertTrue(flags['hostname_allowed']);self.assertFalse(flags[field])

    def test_verified_host_still_requires_verified_and_bound_identity(self):
        import jwt
        helper=IdentityDiagnosticProposalTests()
        env=helper.env()
        env['ACTIONS_ID_TOKEN_REQUEST_URL']='https://run-actions-2-azure-eastus.actions.githubusercontent.com/inert'
        for kind,label in ((jwt.InvalidSignatureError,'signature'),(jwt.InvalidIssuerError,'issuer'),
                           (jwt.InvalidAudienceError,'audience')):
            with self.subTest(reason=label):
                self.assertEqual(helper.failure('jwt_verification',env=env,error=kind(PAYLOAD))['reason'],label)
        for field in ('iss','aud','repository','repository_id','repository_owner_id','actor_id','event_name',
                      'ref','workflow_ref','workflow_sha','sha','run_id','run_attempt','runner_environment','repository_visibility'):
            claims=IdentityTests().claims();claims[field]=PAYLOAD
            with self.subTest(claim=field):helper.failure('claim_binding',env=env,claims=claims)

    def test_each_rejected_origin_predicate_is_distinguished_without_network(self):
        cases=[('http://vstoken.actions.githubusercontent.com/','scheme_allowed'),
               ('https://untrusted.invalid/','hostname_allowed'),
               ('https://'+PAYLOAD+'@vstoken.actions.githubusercontent.com/','username_allowed'),
               ('https://:'+PAYLOAD+'@vstoken.actions.githubusercontent.com/','password_allowed'),
               ('https://vstoken.actions.githubusercontent.com:8443/','port_allowed')]
        for url,field in cases:
            with self.subTest(field=field):
                flags=self.refused(url+'?opaque='+PAYLOAD)
                self.assertTrue(flags['url_parsed']);self.assertFalse(flags[field])
                for key in ('scheme_allowed','hostname_allowed','username_allowed','password_allowed','port_allowed'):
                    if key!=field and not (field=='password_allowed' and key=='username_allowed'):
                        self.assertTrue(flags[key])

    def test_malformed_urls_distinguish_split_and_port_failures(self):
        flags=self.refused('https://['+PAYLOAD)
        self.assertFalse(flags['url_parsed']);self.assertFalse(flags['url_built'])
        for port in (PAYLOAD,'65536','-1'):
            flags=self.refused('https://vstoken.actions.githubusercontent.com:'+port+'/')
            self.assertTrue(flags['url_parsed']);self.assertTrue(flags['hostname_allowed'])
            self.assertFalse(flags['port_parsed']);self.assertFalse(flags['port_allowed'])
            self.assertTrue(flags['url_built'])

    def test_query_and_url_construction_exceptions_are_sanitized(self):
        for name,field in (('parse_qsl','query_parsed'),('urlencode','audience_query_built'),('urlunsplit','url_built')):
            with self.subTest(operation=name),mock.patch.object(ci,name,side_effect=ValueError(PAYLOAD)):
                flags=self.refused('https://vstoken.actions.githubusercontent.com/'+PAYLOAD+'?opaque='+PAYLOAD)
                self.assertTrue(flags['port_allowed']);self.assertFalse(flags[field])

    def test_missing_url_has_boolean_only_unassessed_checks(self):
        event=IdentityDiagnosticProposalTests().failure('request_url',env={
            'GITHUB_ACTIONS':'true','GITHUB_SHA':'a'*40,'BR02_CI_APPROVED_SHA':'a'*40})
        self.assertEqual(event['reason'],'missing_field')
        self.assertEqual(set(event['url_checks']),self.fields)
        self.assertTrue(all(type(v) is bool for v in event['url_checks'].values()))
        self.assertFalse(any(event['url_checks'].values()))

    def test_failed_diagnostic_observation_or_sink_does_not_change_refusal(self):
        env=IdentityDiagnosticProposalTests().env();env['ACTIONS_ID_TOKEN_REQUEST_URL']='https://untrusted.invalid/'+PAYLOAD
        for owner,name in ((ci,'_request_url_checks'),(sys.modules['builtins'],'print')):
            with self.subTest(operation=name),mock.patch.object(owner,name,side_effect=ValueError(PAYLOAD)):
                with self.assertRaises(ci.CIDenied) as caught:ci.authenticate(env)
                self.assertEqual(str(caught.exception),'BR02_CI_IDENTITY')
                self.assertFalse(ci.active())


class EndpointBoundaryTests(unittest.TestCase):
    def test_ambiguous_urls_fail_before_credentials_or_network(self):
        host='vstoken.actions.githubusercontent.com'
        urls=['https://'+host+tail for tail in ('/\nsecret','/\tsecret','/\\secret',
            '/#fragment','/#','/%xx','/?bad=%FF',':/',':0443/','./')]
        urls+=['https://@'+host+'/', 'https://:'+PAYLOAD+'@'+host+'/',
            'https://'+host+'/'+'a'*8192, 'https://\u212a.actions.githubusercontent.com/',
            'https://bad..actions.githubusercontent.com/',
            'https://-bad.actions.githubusercontent.com/',
            'https://'+'a'*64+'.actions.githubusercontent.com/',
            'https://127.0.0.1/', 'https://[::1]/',
            'https://'+host+'/?'+'&'.join('a=1' for _ in range(65))]
        for url in urls:
            with self.subTest(index=urls.index(url)):
                RequestUrlPredicateTests().refused(url)

    def test_invalid_request_credentials_never_open_transport(self):
        for credential in ('',None,123,'a\r\nb','a b','\u212a','a'*16385):
            env=IdentityDiagnosticProposalTests().env()
            env['ACTIONS_ID_TOKEN_REQUEST_TOKEN']=credential
            with mock.patch.object(ci.urllib.request,'build_opener') as opener:
                IdentityDiagnosticProposalTests().failure('request_environment',env=env)
            opener.assert_not_called()

    def test_transport_has_verified_tls_no_proxy_no_redirect_and_one_request(self):
        import ssl
        opener=mock.Mock();opener.open.return_value=TokenResponse(b'{"value":"inert"}')
        env=IdentityDiagnosticProposalTests().env()
        env['ACTIONS_ID_TOKEN_REQUEST_URL']='https://run-actions-3-azure-eastus.actions.githubusercontent.com/inert'
        with mock.patch.object(ci.urllib.request,'build_opener',return_value=opener) as build, \
                mock.patch.object(ci,'_verify_jwt',return_value=IdentityTests().claims()), \
                mock.patch.object(ci.time,'time',return_value=1000):
            ci._authenticated_identity(env)
        opener.open.assert_called_once()
        self.assertEqual(opener.open.call_args.kwargs,{'timeout':10})
        proxy,tls,redirect=build.call_args.args
        self.assertEqual(proxy.proxies,{})
        self.assertEqual(tls._context.verify_mode,ssl.CERT_REQUIRED)
        self.assertTrue(tls._context.check_hostname)
        request=opener.open.call_args.args[0]
        self.assertEqual(request.get_header('Authorization'),'Bearer '+PAYLOAD)
        for code in (301,302,303,307,308):
            response=TokenResponse(b'')
            with self.subTest(code=code),self.assertRaises(ci.CIDenied):
                redirect.http_error_302(request,response,code,'ignored',
                    {'location':'https://attacker.invalid/'})
            self.assertTrue(response.closed)

    def test_http_and_tls_failures_do_not_retry_or_verify(self):
        import ssl
        import urllib.error
        for error in (ssl.SSLCertVerificationError(PAYLOAD),TimeoutError(PAYLOAD),
                urllib.error.HTTPError('https://inert.invalid',503,PAYLOAD,{},TokenResponse(b''))):
            opener=mock.Mock();opener.open.side_effect=error
            with mock.patch.object(ci.urllib.request,'build_opener',return_value=opener), \
                    mock.patch.object(ci,'_verify_jwt') as verify,contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(ci.CIDenied):ci.authenticate(IdentityDiagnosticProposalTests().env())
            opener.open.assert_called_once();verify.assert_not_called()
            if isinstance(error,urllib.error.HTTPError):self.assertTrue(error.fp.closed)
            self.assertFalse(ci.active())

    def test_malformed_token_responses_never_reach_verifier(self):
        for raw in (b'[]',b'null',b'{"value":null}',b'{"value":123}',b'{"value":{}}',
                    b'{"value":""}',b'{"value":"a b"}',b'{"value":"a\\r\\nb"}',
                    b'{"value":"\\u212a"}',b'X'*32769):
            opener=mock.Mock();opener.open.return_value=TokenResponse(raw)
            with mock.patch.object(ci.urllib.request,'build_opener',return_value=opener), \
                    mock.patch.object(ci,'_verify_jwt') as verify,contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(ci.CIDenied):ci.authenticate(IdentityDiagnosticProposalTests().env())
            verify.assert_not_called();self.assertFalse(ci.active())

    def test_non_success_response_is_refused_and_closed(self):
        for status in (204,301,302,307,308,401,403,429,500):
            response=TokenResponse(b'{"value":"inert"}');response.status=status
            opener=mock.Mock();opener.open.return_value=response
            with mock.patch.object(ci.urllib.request,'build_opener',return_value=opener), \
                    mock.patch.object(ci,'_verify_jwt') as verify,contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(ci.CIDenied):ci.authenticate(IdentityDiagnosticProposalTests().env())
            self.assertTrue(response.closed);verify.assert_not_called()
            opener.open.assert_called_once()

    def test_rotating_host_does_not_relax_any_signed_claim(self):
        helper=IdentityDiagnosticProposalTests();env=helper.env()
        env['ACTIONS_ID_TOKEN_REQUEST_URL']='https://future-region.actions.githubusercontent.com/inert'
        for field in IdentityTests().claims():
            claims=IdentityTests().claims();claims.pop(field)
            with self.subTest(field=field):helper.failure('claim_binding',env=env,claims=claims)

    def test_real_local_rsa_signature_and_jwks_validation(self):
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        other=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        claims=IdentityTests().claims();now=int(time.time())
        claims.update(iat=now-1,nbf=now-1,exp=now+120)
        jwk=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        jwk.update(kid='offline-key',alg='RS256',use='sig')
        # Only JWKS bytes are substituted. PyJWT performs real key selection,
        # signature, issuer, audience, algorithm and timestamp validation.
        with mock.patch.object(jwt.PyJWKClient,'fetch_data',return_value={'keys':[jwk]}):
            good=jwt.encode(claims,key,algorithm='RS256',headers={'kid':'offline-key'})
            self.assertEqual(ci._verify_jwt(good)['run_id'],'1234')
            cases=[jwt.encode(claims,other,algorithm='RS256',headers={'kid':'offline-key'}),
                   jwt.encode(claims,'offline-symmetric-key',algorithm='HS256',headers={'kid':'offline-key'})]
            for field,value in (('iss','https://attacker.invalid'),('aud','wrong'),('exp',now-1),('nbf',now+60)):
                altered=dict(claims);altered[field]=value
                cases.append(jwt.encode(altered,key,algorithm='RS256',headers={'kid':'offline-key'}))
            for token in cases:
                with self.assertRaises(jwt.InvalidTokenError):ci._verify_jwt(token)


class IntegrationHostnameObservationTests(unittest.TestCase):
    def observe(self,value):
        result=ci._request_hostname_observation(value)
        self.assertTrue(all(type(v) is bool for k,v in result.items() if k!='hostname'))
        return result

    def test_valid_hostname_is_normalized_without_path_query_or_token(self):
        host='run-actions-9-azure-westus.actions.githubusercontent.com'
        result=self.observe('https://'+host.upper()+':443/private/'+PAYLOAD+'?opaque='+PAYLOAD)
        self.assertEqual(result['hostname'],host)
        self.assertTrue(result['hostname_emitted'])
        self.assertNotIn(PAYLOAD,json.dumps(result))
        self.assertNotIn('https:',json.dumps(result))
        self.assertNotIn('private',json.dumps(result))

    def test_missing_empty_and_hostless_values_are_distinct(self):
        missing=self.observe(None);empty=self.observe('');hostless=self.observe('https:///private?opaque='+PAYLOAD)
        self.assertFalse(missing['value_is_string'])
        self.assertTrue(empty['value_is_string']);self.assertFalse(empty['value_nonempty'])
        self.assertTrue(hostless['url_parsed']);self.assertFalse(hostless['hostname_present'])
        for result in (missing,empty,hostless):self.assertNotIn('hostname',result)

    def test_suspicious_and_malformed_values_never_emit_hostname(self):
        for value in (123,'https://[broken','https://trusted.invalid:bad/',
                      'http://trusted.invalid/','https://user:secret@trusted.invalid/',
                      'https://@trusted.invalid/','https://trusted.invalid:8443/',
                      'https://trusted.invalid/#fragment','https://trusted.invalid/\nsecret',
                      'https://trusted.invalid/\\secret','https://'+'a'*64+'.invalid/',
                      'https://'+'a.'*130+'invalid/','https://127.0.0.1/',
                      'https://[::1]/','https://[v1.foo]/','https://trusted%2einvalid/',
                      'https://-bad.invalid/','https://bad..invalid/','https://trusted.invalid./',
                      'https://\u212a.invalid/','https://trusted.invalid/'+'x'*8192):
            with self.subTest(value_type=type(value).__name__):
                result=self.observe(value)
                self.assertFalse(result['hostname_emitted']);self.assertNotIn('hostname',result)

    def test_observation_never_authorizes_lookalike_or_unknown_host(self):
        helper=IdentityDiagnosticProposalTests()
        for host in ('arbitrary.invalid',
                     'vstoken.actions.githubusercontent.com.attacker.invalid',
                     'actions.githubusercontent.com.attacker.invalid'):
            env=helper.env();env['ACTIONS_ID_TOKEN_REQUEST_URL']='https://'+host+'/private?opaque='+PAYLOAD
            with mock.patch.object(ci.urllib.request,'build_opener') as opener:
                event=helper.failure('request_url',env=env)
            opener.assert_not_called()
            self.assertEqual(event['hostname_observation']['hostname'],host)
            self.assertFalse(event['url_checks']['hostname_allowed'])

    def test_observation_failure_does_not_change_denial(self):
        env=IdentityDiagnosticProposalTests().env();env['ACTIONS_ID_TOKEN_REQUEST_URL']='https://attacker.invalid/'
        for name in ('_request_hostname_observation','_request_url_checks'):
            with mock.patch.object(ci,name,side_effect=ValueError(PAYLOAD)), \
                    mock.patch.object(ci.urllib.request,'build_opener') as opener,contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(ci.CIDenied):ci.authenticate(env)
            opener.assert_not_called();self.assertFalse(ci.active())

    def test_only_request_url_failures_include_hostname_observation(self):
        helper=IdentityDiagnosticProposalTests()
        event=helper.failure('request_url',env={'GITHUB_ACTIONS':'true','GITHUB_SHA':'a'*40,'BR02_CI_APPROVED_SHA':'a'*40})
        self.assertFalse(event['hostname_observation']['value_is_string'])
        self.assertEqual(event['reason'],'missing_field')
        self.assertNotIn('hostname_observation',helper.failure('token_acquisition'))
        helper.test_valid_feature_identity_has_no_diagnostic_and_same_return()


class IntegrationLifecycleRegressionTests(unittest.TestCase):
    def test_all_fifty_actual_fixtures_use_current_approval(self):
        report=self.fixture_probe(runner)
        self.assertEqual(len(report['tests']),50)
        self.assertTrue(report['passed'],report.get('failure_diagnostics'))

    @staticmethod
    def fixture_probe(subject):
        # Exercise the ACTUAL class/setup/teardown paths and authorization.
        # Only SQL transport, seed DML and method bodies are inert; this is
        # fixture-lifecycle evidence, never live integration evidence.
        import br02_pg_fixtures,psycopg
        modules={name:importlib.import_module(name) for name in subject.TESTS}
        def execute(*args,**kwargs):
            return SimpleNamespace(fetchall=lambda:[('schema_version','1'),('result_context_version','1')],
                                   fetchone=lambda:('1234',))
        def connect(*args,**kwargs):
            return SimpleNamespace(execute=execute,close=lambda:None)
        def seed(connection,bundle,approval,**kwargs):
            br02_pg_fixtures.require_seed_approval(approval)
        output=io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(psycopg,'connect',side_effect=connect))
            stack.enter_context(mock.patch.object(br02_pg_fixtures,'seed',side_effect=seed))
            for module_name,classes in subject.TESTS.items():
                for cls_name,names in classes.items():
                    cls=getattr(modules[module_name],cls_name)
                    for name in names:stack.enter_context(mock.patch.object(cls,name,lambda self:None))
            stack.enter_context(mock.patch.dict(os.environ,{}))
            context=stack.enter_context(issued_context())
            stack.enter_context(contextlib.redirect_stdout(output))
            subject.run_tests(context.binding,{'fixture':'inert-admin','app':'inert-app','worker':'inert-worker','owner':'inert-owner'})
        return json.loads(output.getvalue())

    def test_failure_diagnostics_are_fixed_bounded_and_phase_aware(self):
        class Example(unittest.TestCase):
            def setUp(self):raise ci.CIDenied('context')
            def test_one(self):pass
        result=runner.StrictResult()
        unittest.TestSuite([Example('test_one')]).run(result)
        self.assertEqual(result.diagnostics,[{'outcome_index':0,'phase':'setup','exception':'CIDenied'}])
        class Unknown(Exception):pass
        test=Example('test_one')
        for _ in range(110):result.addError(test,(Unknown,Unknown(PAYLOAD),None))
        self.assertEqual(len(result.diagnostics),100)
        self.assertEqual(result.diagnostics[-1]['exception'],'other')
        self.assertNotIn(PAYLOAD,json.dumps(result.diagnostics))
        self.assertFalse(result.passed([test.id()]))

    def test_execution_and_teardown_failures_remain_failures_without_messages(self):
        class Example(unittest.TestCase):
            def test_one(self):raise AssertionError(PAYLOAD)
            def tearDown(self):raise ValueError(PAYLOAD)
        result=runner.StrictResult()
        unittest.TestSuite([Example('test_one')]).run(result)
        self.assertEqual([d['phase'] for d in result.diagnostics],['execution','teardown'])
        self.assertEqual([d['exception'] for d in result.diagnostics],['AssertionError','ValueError'])
        self.assertFalse(result.passed([Example('test_one').id()]))
        self.assertNotIn(PAYLOAD,repr((result.errors,result.failures,result.diagnostics)))

    def test_each_case_reacquires_its_class_approval_before_seeding(self):
        events=[]
        class Example(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                cls.approval=ci._current.get().approval
                events.append('class_setup')
            @classmethod
            def tearDownClass(cls):events.append('class_teardown')
            def setUp(self):
                ci.authorize(self.approval)
                events.append('seed')
            def test_one(self):events.append('one')
            def test_two(self):events.append('two')
        inventory={'inert':{'Example':['test_one','test_two']}}
        module=SimpleNamespace(Example=Example)
        Example.__module__='inert'
        output=io.StringIO()
        with issued_context() as context, \
                mock.patch.object(runner,'TESTS',inventory), \
                mock.patch.object(runner.importlib,'import_module',return_value=module), \
                mock.patch.dict(sys.modules,{'inert':module}), \
                mock.patch.dict(os.environ,{}),contextlib.redirect_stdout(output):
            code=runner.run_tests(context.binding,{'fixture':'inert-admin','app':'inert-app','worker':'inert-worker'})
        self.assertEqual(code,0,(events,output.getvalue()))
        self.assertEqual(events,['class_setup','seed','one','class_teardown',
                                 'class_setup','seed','two','class_teardown'])
        self.assertTrue(json.loads(output.getvalue())['passed'])

    def test_real_concurrency_case_owns_pid_probe_transaction(self):
        # Real test method and real registration code, deterministic shared
        # tuple-row transport. Models psycopg implicit BEGIN, not a database.
        import threading
        from tests import test_br02_pg_concurrency as live
        from tests.test_br02_pg_registration import _RegistrationConnection
        from psycopg.pq import TransactionStatus
        import postgres_result_store,identity_context,br02_pg_fixtures
        lock=threading.RLock();anchors={};contexts={};connections=[]
        class Connection(_RegistrationConnection):
            def __init__(self):
                super().__init__();self.anchors=anchors;self.contexts=contexts
                self.backend=len(connections)+1;connections.append(self)
            def transaction(self):
                parent=super().transaction();connection=self
                class Transaction:
                    def __enter__(self):
                        lock.acquire();parent.__enter__()
                        connection.info.transaction_status=TransactionStatus.INTRANS
                    def __exit__(self,*exc):
                        try:return parent.__exit__(*exc)
                        finally:
                            connection.info.transaction_status=TransactionStatus.IDLE
                            lock.release()
                return Transaction()
            def execute(self,sql,params=None):
                if sql.lower()=='select pg_backend_pid()':
                    self.info.transaction_status=TransactionStatus.INTRANS
                    return SimpleNamespace(fetchone=lambda:(self.backend,))
                return super().execute(sql,params)
        bundle=br02_pg_fixtures.build_bundle('offline-overlap')
        case=live.ConcurrencyOverlapTests('test_distinct_backends_identical_register_one_creator')
        case.bundle=bundle
        def store():
            return postgres_result_store.PostgresResultStore(Connection(),
                identity=identity_context.from_verified_subject(bundle.raw_subject,bundle.tenant_id),
                fresh_completion_denied=False)
        def count(sql,params):
            rows=contexts if 'result_context' in sql else anchors
            return SimpleNamespace(fetchone=lambda:(len(rows),))
        case.conn=SimpleNamespace(execute=count)
        with mock.patch.object(case,'new_store',side_effect=store), \
                mock.patch('runtime_config.is_production_runtime',return_value=False):
            case.test_distinct_backends_identical_register_one_creator()
        self.assertEqual(len(anchors),1);self.assertEqual(len(contexts),1)
        self.assertTrue(all(c.info.transaction_status==TransactionStatus.IDLE for c in connections))


class RemainingIntegrationRegressionTests(unittest.TestCase):
    """Real fixture/caller paths over inert boundaries, never live PG proof."""

    def test_decision_fixtures_preserve_trusted_job_and_assignment_links(self):
        from tests import test_br02_pg_api as live
        import br02_pg_fixtures
        class Captured(Exception): pass
        for name in ('test_decisions_commit_saved_receipts_and_replay',
                     'test_stale_revision_is_refused'):
            case=live.ProductionApiLiveTests(name)
            case.bundle=br02_pg_fixtures.build_bundle('offline-links')
            store=mock.Mock();store.register_with_context.side_effect=Captured
            case.fixture_store=lambda:store
            with self.assertRaises(Captured):getattr(case,name)()
            args=store.register_with_context.call_args.kwargs
            self.assertEqual(args.get('job_id'),case.bundle.job_id)
            self.assertEqual(args.get('assignment_id'),case.bundle.assignment_id)
            store.close.assert_called_once()

    def test_authentication_fixture_really_uses_an_invalid_token(self):
        from tests import test_br02_pg_api as live
        import br02_pg_fixtures,auth_oidc
        class Captured(Exception): pass
        case=live.ProductionApiLiveTests('test_authentication_precedes_idempotent_replay')
        case.bundle=br02_pg_fixtures.build_bundle('offline-auth')
        case.fixture_store=lambda:mock.Mock();case.production_app=lambda:None
        client=mock.MagicMock();client.__enter__.return_value=client
        client.post.side_effect=Captured;case._client=lambda app:client
        with self.assertRaises(Captured):case.test_authentication_precedes_idempotent_replay()
        header=client.post.call_args.kwargs['headers']['Authorization']
        with self.assertRaises(auth_oidc.OidcError):
            live._synthetic_api_verify_token(header.split(' ',1)[1])
        # A valid but nonmember subject must remain a membership refusal.
        self.assertEqual(live._synthetic_api_verify_token('synthetic-subject:nope'),'nope')



    def test_transaction_boundary_maps_driver_errors_and_preserves_python_faults(self):
        import postgres_store,audit_store,psycopg
        from psycopg.pq import TransactionStatus
        from contextlib import nullcontext
        connection=SimpleNamespace(info=SimpleNamespace(transaction_status=TransactionStatus.IDLE),
            transaction=lambda:nullcontext(),execute=lambda *a:SimpleNamespace(fetchone=lambda:('1',)))
        store=postgres_store.PostgresAuditStore(connection,tenant_id='offline-tenant')
        for error in (psycopg.OperationalError(PAYLOAD),RuntimeError(PAYLOAD),audit_store.AuditStoreError('injected','fixed')):
            with mock.patch.object(connection,'execute',side_effect=error):
                with self.assertRaises(audit_store.AuditStoreError if isinstance(error,psycopg.Error) else type(error)) as caught:
                    with store._transaction():self.fail('unreachable')
            if isinstance(error,psycopg.Error):
                self.assertEqual(caught.exception.code,'store_unusable')
                self.assertIs(caught.exception.__cause__,error)
                self.assertNotIn(PAYLOAD,str(caught.exception))
            else:self.assertIs(caught.exception,error)

    def test_child_failure_stages_are_fixed_and_fail_closed(self):
        from tests import br02_pg_api_child as child
        import runtime_config
        previous=ci._current.get()
        env={runtime_config.RUNTIME_ENV:'production',runtime_config.PG_DSN_ENV:'inert-app'}
        try:
            with child_native(child_payload()):ci.adopt_child(child_env())
            for failing,code in (('guard',70),('application',71),('server_import',72),('server_run',73),('server_exit',73)):
                app=mock.Mock(side_effect=ValueError(PAYLOAD) if failing=='application' else None)
                server=SimpleNamespace(run=mock.Mock(side_effect=SystemExit(1) if failing=='server_exit' else ValueError(PAYLOAD) if failing=='server_run' else None))
                with mock.patch.dict(os.environ,env,clear=True), \
                        mock.patch.object(child,'install_child_loopback_only_block',side_effect=ValueError(PAYLOAD) if failing=='guard' else None), \
                        mock.patch.object(child,'build_child_application',app), \
                        mock.patch.dict(sys.modules,{'uvicorn':None if failing=='server_import' else server}):
                    self.assertEqual(child.child_main(['--serve','--port','8123']),code)
        finally:ci._current.set(previous)
        # No context still refuses before app or server machinery.
        with mock.patch.object(child,'build_child_application') as app,contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(child.child_main(['--serve','--port','8123']),3)
            app.assert_not_called()

    def test_child_exit_diagnostics_never_serialize_unapproved_attributes(self):
        from tests import br02_pg_api_child as child
        test=unittest.FunctionTestCase(lambda:None)
        for stage in (*child.CHILD_FAILURE_STAGES.values(),'child_ready',PAYLOAD):
            result=runner.StrictResult();failure=child._child_failure(stage)
            result.addError(test,(RuntimeError,failure,None))
            detail=result.diagnostics[0]
            self.assertNotIn(PAYLOAD,json.dumps(detail))
            self.assertEqual('child_stage' in detail,stage!=PAYLOAD)
            self.assertFalse(result.passed([test.id()]))

    def test_child_parent_reports_fixed_exit_stage_and_closes_capability_transport(self):
        from tests import br02_pg_api_child as child
        import socket,subprocess
        for code,stage in (*child.CHILD_FAILURE_STAGES.items(),(255,'child_ready')):
            process=mock.Mock(returncode=code);process.poll.return_value=code
            probe=mock.Mock();probe.getsockname.return_value=('127.0.0.1',8123)
            stream=mock.Mock();stream.fileno.return_value=17
            with issued_context() as context, \
                    mock.patch.object(socket,'socket',return_value=probe), \
                    mock.patch.object(ci,'child_transport',return_value=(stream,{'BR02_CI_FD':'17'})), \
                    mock.patch.object(subprocess,'Popen',return_value=process) as spawn:
                with self.assertRaises(RuntimeError) as caught:
                    child.run_child_server(context.approval)
                self.assertEqual(caught.exception.br02_child_stage,stage)
                self.assertNotIn(PAYLOAD,str(caught.exception))
                self.assertEqual(spawn.call_args.kwargs['pass_fds'],(17,))
                self.assertEqual(spawn.call_args.kwargs['stderr'],subprocess.DEVNULL)
                stream.close.assert_called_once();probe.close.assert_called_once()
                process.terminate.assert_not_called()

    def test_partial_child_start_failure_reaps_previously_started_child(self):
        from tests import test_br02_pg_processes as live
        first=mock.Mock(pid=1234)
        case=live.ChildProcessLiveTests('test_concurrent_http_competition_across_independent_pids')
        case.approval=object();case.register_original=lambda:None
        failure=RuntimeError(PAYLOAD)
        with mock.patch.object(live.api_child,'run_child_server',side_effect=[first,failure]):
            with self.assertRaises(RuntimeError) as caught:
                case.test_concurrent_http_competition_across_independent_pids()
        self.assertIs(caught.exception,failure)
        first.stop.assert_called_once()

    def test_direct_event_fixtures_round_trip_strict_evidence_decoder(self):
        from tests import test_br02_pg_transactions as transactions
        from tests import test_postgres_api_pg_contract as adapter
        import br02_pg_fixtures,audit_store
        bundle=br02_pg_fixtures.build_bundle('offline-evidence')
        case=transactions.FaultStageTransactionTests('test_fault_after_event_insert_rolls_back_the_event')
        case.bundle=bundle
        event,_=case.correction_event()
        captured=[]
        class Captured(Exception): pass
        other=adapter.PostgresStoreAdapterLiveContractTests('test_idempotent_commit_correction_replays_saved_receipt')
        other.bundle=bundle;store=mock.Mock()
        def commit(**kwargs):captured.append(kwargs['event']);raise Captured
        store.commit_correction.side_effect=commit;other.fixture_store=lambda:store
        with self.assertRaises(Captured):other.test_idempotent_commit_correction_replays_saved_receipt()
        for event in (event,*captured):
            fields=audit_store._event_fields(event)
            row=(1,'evt','key','digest',fields['event_type'],'1','correct','corrected',
                 '2','3','rework',0,1,None,bundle.canonical_owner,'',
                 json.dumps(fields['authorization_evidence']),'0')
            decoded=audit_store._row_to_event(row)
            self.assertEqual(set(decoded['authorization_evidence']),set(audit_store.EVIDENCE_FIELDS))
        # Do not make the reader accept absent/tampered evidence.
        bad=list(row);bad[-2]='{}'
        with self.assertRaises(audit_store.AuditStoreError):audit_store._row_to_event(bad)

    def test_interruption_proxy_delegates_status_and_owned_close(self):
        from tests import test_br02_pg_transactions as live
        from tests.test_br02_pg_registration import _RegistrationConnection
        import br02_pg_fixtures
        connection=_RegistrationConnection();connection.closed=False
        connection.close=lambda:setattr(connection,'closed',True)
        case=live.FaultStageTransactionTests('test_own_connection_interruption_is_reaped_and_replaced')
        case.bundle=br02_pg_fixtures.build_bundle('offline-proxy');case.dsn='inert'
        case.register_original=lambda:None
        case.store=lambda:SimpleNamespace(snapshot=lambda **kw:{'result_revision':0,'events':[]},close=lambda:None)
        with mock.patch.object(live.psycopg,'connect',return_value=connection):
            case.test_own_connection_interruption_is_reaped_and_replaced()
        self.assertTrue(connection.closed)

    def test_role_probe_uses_an_existing_privileged_role(self):
        from tests import test_br02_pg_rls_grants as live
        import psycopg
        runtime=mock.Mock();admin=mock.Mock()
        def execute(sql,params=None):
            text=sql.as_string() if hasattr(sql,'as_string') else sql
            if text.lower().startswith('set role'):
                if 'postgres' in text:raise psycopg.errors.UndefinedObject('inert missing role')
                raise psycopg.errors.InsufficientPrivilege('inert denied')
            raise psycopg.errors.InsufficientPrivilege('inert denied')
        runtime.execute.side_effect=execute
        admin.execute.return_value=SimpleNamespace(fetchone=lambda:('br02_bootstrap',),
            fetchall=lambda:[('marker_app',False,False,False,False,False),('marker_worker',False,False,False,False,False)])
        # Return a scalar count for the existing inherited-edge assertion.
        def admin_execute(sql,params=None):
            if 'count(*)' in sql:return SimpleNamespace(fetchone=lambda:(0,))
            return admin.execute.return_value
        admin.execute.side_effect=admin_execute
        case=live.RlsFailClosedTests('test_runtime_roles_hold_no_elevation_on_their_own_login')
        case.conn=admin;case.app_connection=runtime
        case.test_runtime_roles_hold_no_elevation_on_their_own_login()

    def test_acl_snapshot_does_not_construct_zero_dimensional_acl_arrays(self):
        from tests import test_br02_pg_rls_grants as live
        import psycopg
        seen=[]
        def execute(sql,params=None):
            seen.append(sql)
            if 'ARRAY[]::pg_catalog.aclitem[]' in sql:
                # PostgreSQL 17 check_acl rejects ARR_NDIM != 1.
                raise psycopg.errors.InvalidParameterValue('inert ACL dimensionality')
            return SimpleNamespace(fetchone=lambda:(False,),fetchall=lambda:[])
        report=live._runtime_privilege_snapshot(SimpleNamespace(execute=execute))
        self.assertIn('column_acls',report)
        self.assertEqual(sum('aclexplode' in sql for sql in seen),4)


class ChildModuleEntrypointRegressionTests(unittest.TestCase):
    """Execute the actual module entrypoint; mock only OS/DB/server edges."""

    def execute_module(self, *, authorized=False, application_error=False,
                       invalid_capability=False, port='8123'):
        import runpy
        from tests import br02_pg_api_child as child
        from api.app import create_app  # import before mocking socket/native OS metadata
        # A stand-in socket type prevents mutation of the strict harness's
        # network blocks. No socket or HTTP listener is created here.
        class Socket:
            def connect(self,*args):raise AssertionError('network forbidden')
            def connect_ex(self,*args):raise AssertionError('network forbidden')
            def sendto(self,*args):raise AssertionError('network forbidden')
        socket_module=SimpleNamespace(socket=Socket)
        server=SimpleNamespace(run=mock.Mock())
        sentinel=object();output=io.StringIO();previous=ci._current.get()
        env={'AI_MARKER_RUNTIME':'production','AI_MARKER_PG_DSN':'inert-app'}
        if authorized or invalid_capability:env.update(child_env())
        original_adopt=ci.adopt_child
        def adopt(environ):
            with child_native(b'bad' if invalid_capability else child_payload()):
                return original_adopt(environ)
        try:
            with contextlib.ExitStack() as stack:
                if authorized or invalid_capability:
                    stack.enter_context(mock.patch.object(ci,'adopt_child',side_effect=adopt))
                stack.enter_context(mock.patch.dict(os.environ,env,clear=True))
                stack.enter_context(mock.patch.object(sys,'argv',[
                    str(ROOT/'tests/br02_pg_api_child.py'),'--serve','--port',port]))
                stack.enter_context(mock.patch.dict(sys.modules,{'socket':socket_module,'uvicorn':server}))
                build=stack.enter_context(mock.patch('api.app.create_app',
                    side_effect=ValueError(PAYLOAD) if application_error else None,
                    return_value=sentinel))
                stack.enter_context(contextlib.redirect_stderr(output))
                stack.enter_context(contextlib.redirect_stdout(output))
                with self.assertRaises(SystemExit) as caught:
                    runpy.run_path(str(ROOT/'tests/br02_pg_api_child.py'),run_name='__main__')
                code=caught.exception.code
                self.assertNotIn(PAYLOAD,output.getvalue())
                if authorized and not invalid_capability and port=='8123':
                    self.assertTrue(ci.active())
                    self.assertTrue(ci._current.get().child)
                    self.assertEqual(ci._current.get().approval.target_dsn,'bootstrap-not-forwarded')
                    with self.assertRaises(ci.CIDenied):ci.child_transport(ci._current.get().approval)
                return code,build,server,sentinel
        finally:ci._current.set(previous)

    def test_module_invocation_refuses_without_child_capability(self):
        code,build,server,_=self.execute_module()
        self.assertEqual(code,3);build.assert_not_called();server.run.assert_not_called()

    def test_module_invocation_adopts_restricted_child_and_serves(self):
        code,build,server,sentinel=self.execute_module(authorized=True)
        self.assertEqual(code,0)
        self.assertEqual(build.call_args.kwargs['allowed_origins'],['https://127.0.0.1:8123'])
        self.assertIsNone(build.call_args.kwargs['engine'])
        server.run.assert_called_once_with(sentinel,host='127.0.0.1',port=8123,
                                          log_level='warning',access_log=False)

    def test_module_invocation_preserves_application_failure_exit(self):
        code,build,server,_=self.execute_module(authorized=True,application_error=True)
        self.assertEqual(code,71);build.assert_called_once();server.run.assert_not_called()

    def test_invalid_capability_and_arguments_do_not_start_server(self):
        code,build,server,_=self.execute_module(invalid_capability=True)
        self.assertEqual(code,3);build.assert_not_called();server.run.assert_not_called()
        code,build,server,_=self.execute_module(authorized=True,port='0')
        self.assertEqual(code,2);build.assert_not_called();server.run.assert_not_called()

    def test_importing_child_module_does_not_execute_entrypoint(self):
        import runpy
        from tests import br02_pg_api_child as child
        with mock.patch.object(child.target,'authorize_execution',side_effect=AssertionError('unexpected authorization')):
            module=runpy.run_path(str(ROOT/'tests/br02_pg_api_child.py'),run_name='offline_child_import')
        self.assertTrue(callable(module['child_main']))

    def test_parent_requires_http_readiness_and_cleans_up_on_timeout(self):
        from tests import br02_pg_api_child as child
        import socket,subprocess
        for status in (200,503):
            process=mock.Mock(returncode=None);process.poll.return_value=None
            clock=mock.Mock(return_value=time.monotonic())
            def elapsed(_seconds):clock.return_value+=16
            def waited(**kwargs):
                process.returncode=0;process.poll.return_value=0;return 0
            process.wait.side_effect=waited
            probe=mock.Mock();probe.getsockname.return_value=('127.0.0.1',8123)
            stream=mock.Mock();stream.fileno.return_value=17
            with issued_context() as context, \
                    mock.patch.object(socket,'socket',return_value=probe), \
                    mock.patch.object(ci,'child_transport',return_value=(stream,{'BR02_CI_FD':'17'})), \
                    mock.patch.object(subprocess,'Popen',return_value=process), \
                    mock.patch.object(child._ChildHandle,'request',return_value=(status,{},b'{"status":"ok"}')), \
                    mock.patch.object(time,'monotonic',clock), \
                    mock.patch.object(time,'sleep',side_effect=elapsed):
                if status==200:
                    handle=child.run_child_server(context.approval)
                    process.terminate.assert_not_called()
                    handle.stop()
                else:
                    with self.assertRaises(TimeoutError):child.run_child_server(context.approval)
                process.terminate.assert_called_once();process.wait.assert_called_once_with(timeout=5)
                stream.close.assert_called_once();probe.close.assert_called_once()

    def test_parent_zero_unknown_and_signal_exit_still_fail_before_readiness(self):
        from tests import br02_pg_api_child as child
        import socket,subprocess
        for code in (0,1,2,255,-15,3,71):
            process=mock.Mock(returncode=code);process.poll.return_value=code
            probe=mock.Mock();probe.getsockname.return_value=('127.0.0.1',8123)
            stream=mock.Mock();stream.fileno.return_value=17
            with issued_context() as context, \
                    mock.patch.object(socket,'socket',return_value=probe), \
                    mock.patch.object(ci,'child_transport',return_value=(stream,{'BR02_CI_FD':'17'})), \
                    mock.patch.object(subprocess,'Popen',return_value=process), \
                    mock.patch.object(child._ChildHandle,'request') as request:
                with self.assertRaises(RuntimeError) as caught:child.run_child_server(context.approval)
                self.assertEqual(caught.exception.br02_child_stage,child.CHILD_FAILURE_STAGES.get(code,'child_ready'))
                self.assertEqual(process.returncode,code)
                request.assert_not_called();stream.close.assert_called_once()

    def test_owned_shutdown_kills_and_waits_after_terminate_timeout(self):
        from tests import br02_pg_api_child as child
        import subprocess
        process=mock.Mock(returncode=-9);process.poll.return_value=None
        process.wait.side_effect=[subprocess.TimeoutExpired('synthetic-child',5),-9]
        handle=child._ChildHandle(process,8123,1)
        self.assertEqual(handle.stop(),-9)
        process.terminate.assert_called_once();process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count,2)


class ChildAuthorizationImportRegressionTests(unittest.TestCase):
    """Use child-equivalent root-only discovery, not the runner's tests path."""

    @contextlib.contextmanager
    def fresh_fixture_imports(self):
        package=importlib.import_module('tests')
        names=('br02_pg_fixtures','tests.br02_pg_fixtures')
        previous_path=list(sys.path)
        previous_attr=getattr(package,'br02_pg_fixtures',None)
        previous_context=ci._current.get()
        try:
            with mock.patch.dict(sys.modules):
                for name in names:sys.modules.pop(name,None)
                if hasattr(package,'br02_pg_fixtures'):delattr(package,'br02_pg_fixtures')
                sys.path[:]=[str(ROOT)]+[p for p in sys.path if Path(p or '.').resolve()!=ROOT/'tests']
                yield
        finally:
            sys.path[:]=previous_path
            ci._current.set(previous_context)
            if previous_attr is None:
                if hasattr(package,'br02_pg_fixtures'):delattr(package,'br02_pg_fixtures')
            else:setattr(package,'br02_pg_fixtures',previous_attr)

    def test_fresh_root_only_child_adopts_and_passes_target_gate(self):
        with self.fresh_fixture_imports():
            with child_native(child_payload()) as closed:ci.adopt_child(child_env())
            closed.assert_called_once_with(17)
            target.authorize_execution(None)
            context=ci._current.get()
            self.assertTrue(context.child)
            self.assertEqual(context.approval.app_dsn,'inert-app')
            self.assertEqual(context.approval.worker_dsn,'')
            self.assertEqual(context.approval.target_dsn,'bootstrap-not-forwarded')
            with self.assertRaises(ci.CIDenied):ci.child_transport(context.approval)
            with self.assertRaises(ci.CIDenied):
                with ci.fixture_context(context.approval):pass

    def test_fresh_root_only_module_reaches_next_startup_stage(self):
        case=ChildModuleEntrypointRegressionTests()
        with self.fresh_fixture_imports():
            code,build,server,sentinel=case.execute_module(authorized=True)
            self.assertEqual(code,0)
            build.assert_called_once()
            server.run.assert_called_once_with(sentinel,host='127.0.0.1',port=8123,
                                              log_level='warning',access_log=False)

    def test_fixture_import_spellings_share_approval_type_in_both_orders(self):
        for first in ('br02_pg_fixtures','tests.br02_pg_fixtures'):
            with self.subTest(first=first),self.fresh_fixture_imports():
                if first=='br02_pg_fixtures':sys.path.insert(0,str(ROOT/'tests'))
                original=importlib.import_module(first)
                package=importlib.import_module('tests.br02_pg_fixtures')
                legacy=importlib.import_module('br02_pg_fixtures')
                self.assertIs(original,package);self.assertIs(package,legacy)
                approval=ci.make_approval(TargetTests().binding(),
                    {'fixture':'inert-admin','app':'inert-app','worker':'inert-worker'})
                self.assertIs(type(approval),legacy.SeedApproval)
                with mock.patch.object(target,'authorize_execution') as gate:
                    self.assertIs(legacy.require_seed_approval(approval),approval)
                    gate.assert_called_once_with(approval)

    def test_root_only_negative_capabilities_still_fail_closed(self):
        cases=[({},child_payload(),{}),
               (child_env(),b'X'*100,{}),
               (child_env(BR02_CI_SHA='b'*40),child_payload(),{}),
               (child_env(),child_payload(expires=time.monotonic()-1),{}),
               (child_env(),child_payload(),{'uid':60001}),
               (child_env(),child_payload(),{'access':os.O_RDWR})]
        wrong=TargetTests().binding();wrong['scope']='stage2'
        cases.append((child_env(),child_payload(wrong),{}))
        for env,payload,attrs in cases:
            with self.subTest(attrs=attrs),self.fresh_fixture_imports():
                with child_native(payload,**attrs),self.assertRaises(ci.CIDenied):ci.adopt_child(env)
                self.assertFalse(ci.active())
                with self.assertRaises(target.TargetRefused):target.authorize_execution(None)


def setUpModule():
    global ci,runner,target
    ci=importlib.import_module('tests.br02_pg_ci_context')
    runner=importlib.import_module('tests.run_br02_pg_ci')
    target=importlib.import_module('br02_pg_target')


def main():
    global ci,runner,target
    def refused(*args,**kwargs):raise AssertionError('OFFLINE_GUARD')
    from tests import run_br02_pg_harness as harness
    real=harness.install_environment_guard()
    try:
        harness.verify_blocks(harness.install_connection_blocks())
        harness.install_offline_process_block()
        with contextlib.ExitStack() as stack:
            for owner,name in ((os,'fork'),(os,'pipe'),(os,'setuid'),(os,'setgid'),(os,'setgroups')):
                stack.enter_context(mock.patch.object(owner,name,side_effect=refused,create=True))
            stack.enter_context(mock.patch('secrets.token_bytes',side_effect=refused))
            stack.enter_context(mock.patch('secrets.token_urlsafe',side_effect=refused))
            stack.enter_context(mock.patch('urllib.request.urlopen',side_effect=refused))
            stack.enter_context(mock.patch('urllib.request.build_opener',side_effect=refused))
            ci=importlib.import_module('tests.br02_pg_ci_context')
            runner=importlib.import_module('tests.run_br02_pg_ci')
            target=importlib.import_module('br02_pg_target')
            result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
            return 0 if result.wasSuccessful() else 1
    finally:harness.restore_environment(real)


if __name__=='__main__':sys.exit(main())
