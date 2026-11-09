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


_jtis=itertools.count()

def issue_ticket(*,claims=None,environ=None):
    value=IdentityTests().claims() if claims is None else dict(claims)
    if claims is None:
        now=int(time.time());value.update(iat=now-1,nbf=now-1,exp=now+120,jti='offline-'+str(next(_jtis)))
    env={'GITHUB_ACTIONS':'true','GITHUB_SHA':'a'*40,'BR02_CI_APPROVED_SHA':'a'*40,
         'GITHUB_RUN_ID':'1234','ACTIONS_ID_TOKEN_REQUEST_URL':'https://vstoken.actions.githubusercontent.com/inert',
         'ACTIONS_ID_TOKEN_REQUEST_TOKEN':'inert-not-a-token'}
    if environ:env.update(environ)
    opener=mock.Mock();opener.open.return_value=io.BytesIO(b'{"value":"inert-signed-token"}')
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
        response=mock.Mock();response.open.return_value=io.BytesIO(b'{"value":"inert"}')
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
        opener=mock.Mock();opener.open.return_value=io.BytesIO(raw)
        if stage=='token_acquisition':opener.open.side_effect=OSError(PAYLOAD)
        value=IdentityTests().claims() if claims is None else claims
        output=io.StringIO()
        with mock.patch.object(ci.urllib.request,'build_opener',return_value=opener), \
                mock.patch.object(ci,'_verify_jwt',side_effect=error,return_value=value), \
                mock.patch.object(ci.time,'time',return_value=1000),contextlib.redirect_stderr(output):
            with self.assertRaises(ci.CIDenied) as caught:ci._authenticated_identity(self.env() if env is None else env)
        event=json.loads(output.getvalue().split('BR02_CI_IDENTITY_DIAGNOSTIC ',1)[1])
        self.assertEqual(event['stage'],stage)
        self.assertEqual(set(event),{'stage','reason','checks'} if stage=='claim_binding' else {'stage','reason'})
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
        opener=mock.Mock();opener.open.return_value=io.BytesIO(b'{"value":"synthetic.jwt.token"}')
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
