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
