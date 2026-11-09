"""Future owner-dispatched CI only. Never run this script during offline checks."""
import sys
sys.dont_write_bytecode=True
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
import contextlib
import importlib
import io
import json
import os
import subprocess
import time
import unittest
from tests import br02_pg_ci_context as ci

# Exact reviewed concrete live classes. Test methods are pinned below, not
# inferred by broad discovery or by names that silently accept missing tests.
TESTS = {'tests.test_br02_pg_api': {'ProductionApiLiveTests': ['test_healthz_is_public_and_content_free',
                                                       'test_projection_and_history_serve_the_registered_result',
                                                       'test_missing_context_is_404_with_no_lazy_creation',
                                                       'test_decisions_commit_saved_receipts_and_replay',
                                                       'test_stale_revision_is_refused',
                                                       'test_authentication_precedes_idempotent_replay',
                                                       'test_cross_tenant_reads_are_indistinguishable_from_absent',
                                                       'test_production_fresh_completion_is_denied_with_zero_writes',
                                                       'test_resource_cleanup_between_requests']},
 'tests.test_br02_pg_concurrency': {'ConcurrencyOverlapTests': ['test_distinct_backends_identical_register_one_creator',
                                                                'test_concurrent_changed_content_exactly_one_winner',
                                                                'test_competing_reviews_serialize_without_lost_updates',
                                                                'test_consistent_reader_across_writer_commit']},
 'tests.test_br02_pg_processes': {'ChildProcessLiveTests': ['test_one_child_ready_request_and_reap',
                                                            'test_concurrent_http_competition_across_independent_pids',
                                                            'test_actual_restart_reaped_pid_and_saved_receipts']},
 'tests.test_br02_pg_registration': {'RegistrationBothOrNothingTests': ['test_anchor_and_context_are_written_together',
                                                                        'test_changed_original_refused_and_context_absent',
                                                                        'test_conflicting_context_identity_refused_no_second_row',
                                                                        'test_replay_is_idempotent_with_no_second_write',
                                                                        'test_projection_digest_and_immutability',
                                                                        'test_no_initial_review_state']},
 'tests.test_br02_pg_rls_grants': {'RlsFailClosedTests': ['test_tenant_unbound_fails_closed_on_the_restricted_login',
                                                          'test_cross_tenant_is_indistinguishable_from_absent',
                                                          'test_within_tenant_owner_privacy_is_store_level',
                                                          'test_runtime_roles_hold_no_elevation_on_their_own_login',
                                                          'test_worker_membership_still_absent_everywhere',
                                                          'test_stage2_catalog_comparison_reports_zero_diffs',
                                                          'test_no_runtime_grant_changes_across_operations']},
 'tests.test_br02_pg_rollback': {'RollbackRefusalTests': ['test_nonbypass_owner_view_is_hidden_without_binding',
                                                          'test_privileged_populated_refusal_preserves_everything',
                                                          'test_unauthorized_role_refused_even_on_empty_context',
                                                          'test_lock_writer_blocks_the_draft_atomically',
                                                          'test_empty_privileged_success_is_future_only']},
 'tests.test_br02_pg_transactions': {'FaultStageTransactionTests': ['test_fault_before_begin_preserves_original',
                                                                    'test_fault_after_event_insert_rolls_back_the_event',
                                                                    'test_real_database_failure_is_distinguishable_from_python_faults',
                                                                    'test_own_connection_interruption_is_reaped_and_replaced']},
 'tests.test_br02_pg_worker_denial': {'WorkerZeroSideEffectsTests': ['test_denied_worker_run_touches_nothing',
                                                                     'test_production_service_refuses_fresh_registration']},
 'tests.test_postgres_api_pg_contract': {'PostgresStoreAdapterLiveContractTests': ['test_snapshot_readback_is_consistent',
                                                                                   'test_concurrent_identical_register_is_idempotent_no_duplicate',
                                                                                   'test_idempotent_commit_correction_replays_saved_receipt',
                                                                                   'test_fault_before_commit_preserves_committed_original',
                                                                                   'test_cross_tenant_read_is_indistinguishable_from_absent',
                                                                                   'test_runtime_roles_hold_select_insert_only_on_result_context']},
 'tests.test_postgres_result_store_contract': {'PostgresResultContextContractTests': ['test_registration_commits_context_and_anchor_together',
                                                                                      'test_conflicting_original_under_the_same_identity_is_refused',
                                                                                      'test_reads_survive_reopening_a_second_connection',
                                                                                      'test_cross_tenant_lookup_is_indistinguishable_from_absent',
                                                                                      'test_runtime_role_cannot_update_or_delete_context',
                                                                                      'test_rollback_draft_refuses_while_originals_exist']}}
EXCLUDED = (
 'tests.test_br02_pg_rls_grants.RlsFailClosedTests.test_stage2_catalog_comparison_reports_zero_diffs',
 'tests.test_br02_pg_rollback.RollbackRefusalTests.test_empty_privileged_success_is_future_only')


def command(*args):
    try:
        result=subprocess.run(args,cwd=ROOT,env={'PATH':'/usr/bin:/bin','HOME':'/nonexistent'},
            capture_output=True,timeout=15,check=True)
        if len(result.stdout)>262144: ci.deny('integrity')
        return result.stdout.decode('utf8')
    except Exception: raise ci.CIDenied('isolation') from None


def bootstrap(binding,ticket):
    ci.require_ticket(ticket,binding)  # before driver, password generation or SQL
    import psycopg
    from psycopg import sql
    import secrets
    from psycopg.conninfo import make_conninfo
    initial='host=127.0.0.1 port=55432 dbname=br02_ci user=br02_bootstrap password=bootstrap-ci-only connect_timeout=5'
    generated={name:secrets.token_urlsafe(32) for name in ('br02_bootstrap','marker_app_login','marker_worker_login','br02_ci_owner')}
    with psycopg.connect(initial,autocommit=True,options='-c search_path=public -c statement_timeout=15000 -c lock_timeout=3000') as conn:
        row=conn.execute('select current_database(), version(), inet_server_addr()::text, inet_server_port()').fetchone()
        if row[0]!='br02_ci' or not row[1].startswith('PostgreSQL 17.11 ') or row[2]!=binding['server_address'] or row[3]!=5432: ci.deny('target')
        if conn.execute("select count(*) from pg_catalog.pg_tables where schemaname='public'").fetchone()[0]!=0: ci.deny('target')
        for name in ci.MIGRATIONS:
            ci.require_ticket(ticket,binding)
            conn.execute((ROOT/'schema'/name).read_text(encoding='utf8'))
        conn.execute((ROOT/'tests/br02_pg_ci_setup.sql').read_text(encoding='utf8'))
        for role,password in generated.items():
            conn.execute(sql.SQL('ALTER ROLE {} PASSWORD {}').format(sql.Identifier(role),sql.Literal(password)))
        conn.execute('insert into public.schema_meta(key,value) values (%s,%s)',('br02_synthetic_run_id',binding['run_id']))
        shapes=conn.execute("select rolname,rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolreplication from pg_roles where rolname in ('marker_app_login','marker_worker_login','br02_ci_owner')").fetchall()
        if len(shapes)!=3 or any(any(row[1:]) for row in shapes): ci.deny('target')
        elevated=conn.execute("select count(*) from pg_roles p where (p.rolsuper or p.rolbypassrls or p.rolcreatedb or p.rolcreaterole or p.rolreplication) and (pg_has_role('marker_app_login',p.oid,'MEMBER') or pg_has_role('marker_worker_login',p.oid,'MEMBER'))").fetchone()[0]
        if elevated: ci.deny('target')
        binding['system_identifier']=str(conn.execute('select system_identifier from pg_control_system()').fetchone()[0])
        binding['server_version']='17.11'
        binding['restricted_roles']=[{'name':row[0],'superuser':False,'bypassrls':False,
                                    'createdb':False,'createrole':False,'replication':False} for row in shapes]
    return {kind:make_conninfo(host='127.0.0.1',port=55432,dbname='br02_ci',user=role,password=generated[role],
                              connect_timeout=5,options='-c statement_timeout=15000 -c lock_timeout=3000')
            for kind,role in [('fixture','br02_bootstrap'),('app','marker_app_login'),('worker','marker_worker_login'),('owner','br02_ci_owner')]}


def build_suite(modules, inventory):
    tests=[]
    for module_name, classes in inventory.items():
        module=modules[module_name]
        for cls_name,names in classes.items():
            cls=getattr(module,cls_name,None)
            if cls is None: ci.deny('suite')
            actual=set(unittest.defaultTestLoader.getTestCaseNames(cls))
            if actual!=set(names): ci.deny('suite')
            tests.extend(cls(name) for name in names if module_name+'.'+cls_name+'.'+name not in EXCLUDED)
    if not tests: ci.deny('suite')
    return tests


class StrictResult(unittest.TestResult):
    def __init__(self): super().__init__(); self.outcomes=[]
    def addSuccess(self,test): self.outcomes.append((test.id(),'pass'))
    def addError(self,test,err): self.outcomes.append((test.id(),'error')); self.errors.append((test.id(),'sanitized'))
    def addFailure(self,test,err): self.outcomes.append((test.id(),'failure')); self.failures.append((test.id(),'sanitized'))
    def addSkip(self,test,reason): self.outcomes.append((test.id(),'unexpected_skip')); self.skipped.append((test.id(),'sanitized'))
    def addExpectedFailure(self,test,err): self.addFailure(test,err)
    def addUnexpectedSuccess(self,test): self.failures.append((test.id(),'unexpected_success'))
    def passed(self,expected):
        return not self.errors and not self.failures and not self.skipped and len(self.outcomes)==len(expected) \
            and {n for n,status in self.outcomes}==set(expected) and all(status=='pass' for n,status in self.outcomes)


def runtime_probe_fixture(test, app_dsn):
    """Keep runtime UPDATE/DELETE probes restricted even across rollback.

    Existing methods use SET ROLE inside a transaction, then roll it back.
    Their session login must itself remain restricted after that rollback.
    Seed using the original fixture connection, then change only this probe's
    transport; all existing assertions and method bodies remain unchanged.
    """
    if test._testMethodName not in ('test_runtime_role_cannot_update_or_delete_context',
                                   'test_runtime_roles_hold_select_insert_only_on_result_context'):
        return test
    original=test.setUp
    def setup():
        original()
        test.dsn=app_dsn
    test.setUp=setup
    return test


def run_tests(binding,dsns):
    modules={name:importlib.import_module(name) for name in TESTS}
    # These are the pre-existing future approval supply hooks. Their result is
    # the authenticated context's exact object; no gate is mocked or replaced.
    for module in modules.values():
        module._live_approval=lambda *args:ci._current.get().approval
        if hasattr(module,'_live_runtime_binding'):
            module._live_runtime_binding=lambda:(ci._current.get().approval.app_dsn,'marker_app_login')
    selected=build_suite(modules,TESTS)
    result=StrictResult(); expected=[test.id() for test in selected]
    original=ci._current.get()
    for test in selected:
        runtime_probe_fixture(test,dsns['app'])
        # One owner-specific FORCE-RLS proof requires a non-BYPASS table owner;
        # privileged rollback-refusal methods retain the bootstrap fixture DSN.
        fixture=dsns['owner'] if test.id().endswith('.test_nonbypass_owner_view_is_hidden_without_binding') else dsns['fixture']
        approval=ci.make_approval(binding,dict(dsns,fixture=fixture))
        os.environ['AI_MARKER_TEST_PG_DSN']=fixture
        with ci.fixture_context(approval):
            with open(os.devnull,'w') as sink,contextlib.redirect_stdout(sink),contextlib.redirect_stderr(sink):
                # A one-test suite guarantees class connections are closed before
                # switching fixture roles. Assertions/method bodies are reused.
                unittest.TestSuite([test]).run(result)
    print(json.dumps({'tests':result.outcomes,'excluded':EXCLUDED,'passed':result.passed(expected)},separators=(',',':')))
    return 0 if result.passed(expected) else 1


def source_snapshot(sha):
    """Approved Git objects only: readable immutable source for the test UID.

    Do not widen permissions on /home/runner or copy ignored credentials.
    """
    import tempfile,tarfile,shutil
    from pathlib import PurePosixPath
    result=subprocess.run(['/usr/bin/git','archive','--format=tar',sha],cwd=ROOT,
        env={'PATH':'/usr/bin:/bin'},capture_output=True,check=True,timeout=15)
    if len(result.stdout)>64*1024**2: ci.deny('integrity')
    folder=Path(tempfile.mkdtemp(prefix='br02-ci-source-',dir='/tmp'))
    try:
        folder.chmod(0o755)
        with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
            for item in archive.getmembers():
                name=PurePosixPath(item.name)
                if name.is_absolute() or '..' in name.parts or '\\' in item.name \
                        or not (item.isfile() or item.isdir()): ci.deny('integrity')
                path=folder.joinpath(*name.parts)
                if not path.is_relative_to(folder): ci.deny('integrity')
                if item.isdir(): path.mkdir(parents=True,exist_ok=True);path.chmod(0o755)
                else:
                    path.parent.mkdir(parents=True,exist_ok=True)
                    with archive.extractfile(item) as source,path.open('xb') as destination:
                        shutil.copyfileobj(source,destination)
                    path.chmod(0o444)
        for path in folder.rglob('*'):
            if path.is_dir():path.chmod(0o755)
        return folder
    except BaseException:
        shutil.rmtree(folder);raise


def isolated_worker(binding,dsns,source=None):
    if not ci.active(): ci.deny('context')
    import pwd
    uid=60001
    try: pwd.getpwuid(uid)
    except KeyError: pass
    else: ci.deny('isolation')
    # Fail if the reserved ephemeral UID is already used by any process.
    for proc in Path('/proc').iterdir():
        if proc.name.isdigit():
            try:
                if proc.stat().st_uid==uid: ci.deny('isolation')
            except FileNotFoundError: pass
    chain='BR02_'+binding['run_id'][-16:]
    attached=False; ipv6=False; child=None
    command('/usr/sbin/iptables','-N',chain)
    try:
        command('/usr/sbin/iptables','-A',chain,'-d','127.0.0.0/8','-j','ACCEPT')
        command('/usr/sbin/iptables','-A',chain,'-d',binding['server_address'],'-p','tcp','--dport','5432','-j','ACCEPT')
        command('/usr/sbin/iptables','-A',chain,'-j','REJECT')
        command('/usr/sbin/iptables','-I','OUTPUT','1','-m','owner','--uid-owner',str(uid),'-j',chain); attached=True
        # Reject IPv6 for the test UID too; GitHub runner traffic uses another UID.
        command('/usr/sbin/ip6tables','-I','OUTPUT','1','-m','owner','--uid-owner',str(uid),'-j','REJECT')
        ipv6=True
        child=os.fork()
        if child==0:
            try:
                os.setsid()  # parent owns this group, including API grandchildren
                os.setgroups([]); os.setgid(uid); os.setuid(uid)
                if source is not None:
                    os.chdir(source);sys.path[:0]=[str(source),str(source/'tests')]
                    # The package was loaded by the issuer, before fork.
                    # Route subsequent test/API imports to identical Git bytes.
                    sys.modules['tests'].__path__=[str(source/'tests')]
                # No GitHub bearer, OIDC request token or runner ambient values
                # enter the test worker. Only synthetic transports are installed.
                os.environ.clear(); os.environ.update(PATH='/usr/bin:/bin',HOME='/nonexistent',TMPDIR='/tmp')
                code=run_tests(binding,dsns)
                sys.stdout.flush(); os._exit(code)
            except BaseException:
                print('BR02_CI_SUITE',flush=True); os._exit(2)
        deadline=min(time.monotonic()+480,ci._current.get().expires)
        while time.monotonic()<deadline:
            pid,status=os.waitpid(child,os.WNOHANG)
            if pid:
                import signal
                group=child
                child=None
                try: os.killpg(group,signal.SIGKILL)
                except ProcessLookupError: pass
                else: ci.deny('isolation')  # a surviving owned child is failure
                return os.waitstatus_to_exitcode(status)
            time.sleep(.05)
        ci.deny('expired')
    finally:
        failed=False
        if child:
            import signal
            try:
                try: os.killpg(child,signal.SIGKILL)
                except ProcessLookupError: os.kill(child,signal.SIGKILL)
                os.waitpid(child,0)
            except BaseException: failed=True
        # Only this job's tagged rules are removed, never shared firewall state.
        cleanup=[]
        if attached: cleanup.append(('/usr/sbin/iptables','-D','OUTPUT','-m','owner','--uid-owner',str(uid),'-j',chain))
        if ipv6: cleanup.append(('/usr/sbin/ip6tables','-D','OUTPUT','-m','owner','--uid-owner',str(uid),'-j','REJECT'))
        cleanup.extend([('/usr/sbin/iptables','-F',chain),('/usr/sbin/iptables','-X',chain)])
        for args in cleanup:
            try: command(*args)
            except BaseException: failed=True
        if failed: ci.deny('isolation')


def main():
    try:
        if len(sys.argv)!=1: ci.deny('context')
        if os.name!='posix' or sys.platform!='linux' or os.geteuid()!=0: ci.deny('isolation')
        identity,ticket=ci.authenticate(os.environ)
        if command('/usr/bin/git','rev-parse','HEAD').strip()!=identity['sha'] \
                or command('/usr/bin/git','status','--porcelain','--untracked-files=all').strip(): ci.deny('integrity')
        container=os.environ.get('BR02_CI_CONTAINER','')
        if len(container)!=64 or any(c not in '0123456789abcdef' for c in container): ci.deny('target')
        infos=json.loads(command('/usr/bin/docker','inspect',container))
        if len(infos)!=1 or infos[0]['Id']!=container: ci.deny('target')
        binding=ci.validate_target(infos[0],identity)
        dsns=bootstrap(binding,ticket); approval=ci.make_approval(binding,dsns)
        import hashlib
        print(json.dumps({'identity':binding,'image':ci.IMAGE,'migrations':[
            {'name':name,'sha256':hashlib.sha256((ROOT/'schema'/name).read_bytes()).hexdigest()}
            for name in ci.MIGRATIONS]},separators=(',',':')),flush=True)
        source=source_snapshot(identity['sha'])
        try:
            with ci.installed(approval,binding,ticket=ticket):
                result=isolated_worker(binding,dsns,source)
        finally:
            import shutil
            # Only this function's exclusively created, root-owned snapshot.
            if source.parent!=Path('/tmp') or not source.name.startswith('br02-ci-source-'):ci.deny('isolation')
            shutil.rmtree(source)
        print(json.dumps({'owned_process_and_firewall_cleanup':'confirmed',
                          'service_storage_cleanup':'GitHub job teardown required'}),flush=True)
        return result
    except ci.CIDenied as error:
        print(str(ci.CIDenied(error.code if type(error) is ci.CIDenied else 'context'))); return 2
    except BaseException:
        print('BR02_CI_FAILED'); return 2


if __name__=='__main__': sys.exit(main())
