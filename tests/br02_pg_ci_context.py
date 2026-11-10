"""CI-only authority: signed GitHub identity, owned Docker target, local capability.

No import-time I/O, environment reads, keys or credentials. Stage 2 is not
recognized here. Environment values never authenticate a run by themselves.
"""
import contextlib
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit, parse_qsl, urlencode, urlunsplit
import urllib.request

REPOSITORY = 'selim-Tamer/edexcel-maths-correctorv2'
REPOSITORY_ID = '1389123218'
OWNER_ID = '329718877'
REF = 'refs/heads/br-02/postgres-result-persistence'
WORKFLOW = REPOSITORY + '/.github/workflows/br02-postgres-integration.yml@' + REF
ISSUER = 'https://token.actions.githubusercontent.com'
AUDIENCE = 'omnigrade-br02-isolated-ci'
REQUEST_HOSTS = frozenset(('vstoken.actions.githubusercontent.com',
    'run-actions-2-azure-eastus.actions.githubusercontent.com'))
IMAGE = 'postgres:17.11-alpine@sha256:3540c0362c809df084ffac0ae69fe794515f9de92692f02178bcdce0ef0302ac'
ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ('postgres_v1.sql','postgres_v2_tenant_rls.sql','postgres_v3_jobs.sql',
    'postgres_v4_documents.sql','postgres_v5_membership_access.sql','postgres_v6_result_narrowing.sql',
    'postgres_v7_assignments.sql','postgres_v8_job_assignment_linkage.sql','postgres_v9_result_context.sql')
SCOPE = 'br02.ci.integration'


class CIDenied(Exception):
    def __init__(self, code):
        self.code = code if type(code) is str and code in {'identity','target','context','expired','child','integrity','isolation','suite','database'} else 'context'
        super().__init__('BR02_CI_' + self.code.upper())


def deny(code): raise CIDenied(code)


def _verify_jwt(token):
    import jwt
    key = jwt.PyJWKClient(ISSUER + '/.well-known/jwks', timeout=10).get_signing_key_from_jwt(token).key
    return jwt.decode(token, key, algorithms=['RS256'], audience=AUDIENCE, issuer=ISSUER,
        options={'require':['iss','aud','exp','iat','nbf','jti','repository','repository_id','repository_owner_id',
                            'actor_id','event_name','run_id','run_attempt','sha','ref','workflow_ref','workflow_sha','runner_environment','repository_visibility']})


def validate_claims(claims, expected, now):
    if type(claims) is not dict or type(expected) is not dict: deny('identity')
    fixed = {'iss':ISSUER,'aud':AUDIENCE,'repository':REPOSITORY,'repository_id':REPOSITORY_ID,
        'repository_owner_id':OWNER_ID,'actor_id':OWNER_ID,'event_name':'workflow_dispatch',
        'ref':REF,'workflow_ref':WORKFLOW,'runner_environment':'github-hosted','run_attempt':'1','repository_visibility':'public'}
    if any(claims.get(k) != v for k,v in fixed.items()): deny('identity')
    sha = claims.get('sha'); run = claims.get('run_id')
    if type(sha) is not str or re.fullmatch('[0-9a-f]{40}',sha) is None \
            or claims.get('workflow_sha') != sha or type(run) is not str or not run.isascii() or not run.isdigit(): deny('identity')
    for k in ('run_id','sha'):
        if claims.get(k) != expected.get(k): deny('identity')
    for k in ('exp','iat','nbf'):
        if type(claims.get(k)) is not int: deny('identity')
    if not claims['nbf'] <= now < claims['exp'] or not 0 <= now - claims['iat'] <= 300: deny('expired')
    if type(claims.get('jti')) is not str or not claims['jti']: deny('identity')
    return {'run_id':run,'sha':sha,'attempt':'1'}


def _request_url_checks(value):
    """Failure-only observations; never authorize or return URL components.

    False also means unevaluated when its prerequisite parse/build failed.
    """
    checks=dict.fromkeys(('url_parsed','scheme_allowed','hostname_allowed',
        'username_allowed','password_allowed','port_parsed','port_allowed',
        'query_parsed','audience_query_built','url_built'),False)
    if type(value) is not str: return checks
    try:
        address=urlsplit(value)
        checks['url_parsed']=True
        checks['scheme_allowed']=address.scheme=='https'
        checks['hostname_allowed']=address.hostname in REQUEST_HOSTS
        checks['username_allowed']=not bool(address.username)
        checks['password_allowed']=not bool(address.password)
    except Exception: return checks
    try:
        port=address.port
        checks['port_parsed']=True
        checks['port_allowed']=port in (None,443)
    except Exception: pass
    try:
        query=dict(parse_qsl(address.query)); checks['query_parsed']=True
        query['audience']=AUDIENCE
        encoded=urlencode(query); checks['audience_query_built']=True
        urlunsplit((address.scheme,address.netloc,address.path,encoded,''))
        checks['url_built']=True
    except Exception: pass
    return checks


def _request_hostname_observation(value):
    """Failure-only DNS observation, never endpoint authorization or I/O."""
    result=dict.fromkeys(('value_is_string','value_nonempty','input_length_valid',
        'input_clean','url_parsed','scheme_https','authority_dns_form',
        'userinfo_absent','port_valid','fragment_absent','hostname_present',
        'hostname_length_valid','hostname_syntax_valid','hostname_emitted'),False)
    if type(value) is not str: return result
    result['value_is_string']=True
    result['value_nonempty']=bool(value)
    result['input_length_valid']=0<len(value)<=8192
    if not result['input_length_valid']: return result
    result['input_clean']=not any(ord(c)<=32 or ord(c)==127 or c=='\\' for c in value)
    if not result['input_clean']: return result
    try:
        address=urlsplit(value)
        result['url_parsed']=True
        result['scheme_https']=address.scheme=='https'
        result['authority_dns_form']=address.netloc.isascii() and not any(c in address.netloc for c in '[]%')
        result['userinfo_absent']=address.username is None and address.password is None
        result['fragment_absent']=not bool(address.fragment)
        hostname=address.hostname
        result['hostname_present']=bool(hostname)
        try: result['port_valid']=address.port in (None,443)
        except ValueError: pass
        if not hostname: return result
        hostname=hostname.lower()
        result['hostname_length_valid']=0<len(hostname)<=253
        labels=hostname.split('.')
        result['hostname_syntax_valid']=(hostname.isascii() and len(labels)>=2
            and all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',label) is not None for label in labels)
            and re.search(r'[a-z]',labels[-1]) is not None)
        if all(flag for key,flag in result.items() if key!='hostname_emitted'):
            result['hostname']=hostname
            result['hostname_emitted']=True
    except Exception: pass
    return result


def _identity_diagnostic(stage, error=None, claims=None, expected=None, request_url=None):
    # Fixed vocabulary only: never stringify exceptions, tokens or claims.
    allowed={'environment','request_url','request_environment','token_acquisition',
             'token_response','jwt_verification','claim_binding'}
    if stage not in allowed: stage='environment'
    reason='refused'
    if stage=='jwt_verification':
        try:
            import jwt
            for kind,label in ((jwt.InvalidAudienceError,'audience'),
                               (jwt.InvalidIssuerError,'issuer'),
                               (jwt.InvalidSignatureError,'signature'),
                               (jwt.MissingRequiredClaimError,'missing_claim'),
                               (jwt.ExpiredSignatureError,'expired'),
                               (jwt.ImmatureSignatureError,'not_yet_valid'),
                               (jwt.PyJWKClientError,'signing_key'),
                               (jwt.InvalidTokenError,'invalid_token')):
                if isinstance(error,kind): reason=label;break
        except Exception: reason='verification_unavailable'
    elif isinstance(error,KeyError): reason='missing_field'
    # Only equality/shape booleans for verified claims, never observed values.
    # This mirrors policy for diagnostics only and cannot affect issuance.
    try:
        event={'stage':stage,'reason':reason}
        if stage=='request_url':
            event['url_checks']=_request_url_checks(request_url)
            event['hostname_observation']=_request_hostname_observation(request_url)
        if stage=='claim_binding' and type(claims) is dict and type(expected) is dict:
            fixed={'iss':ISSUER,'aud':AUDIENCE,'repository':REPOSITORY,'repository_id':REPOSITORY_ID,
                   'repository_owner_id':OWNER_ID,'actor_id':OWNER_ID,'event_name':'workflow_dispatch',
                   'ref':REF,'workflow_ref':WORKFLOW,'runner_environment':'github-hosted',
                   'run_attempt':'1','repository_visibility':'public'}
            checks={key:claims.get(key)==value for key,value in fixed.items()}
            checks.update(sha_matches_environment=claims.get('sha')==expected.get('sha'),
                          run_id_matches_environment=claims.get('run_id')==expected.get('run_id'),
                          workflow_sha_matches_sha=claims.get('workflow_sha')==claims.get('sha'),
                          sha_shape=type(claims.get('sha')) is str and re.fullmatch('[0-9a-f]{40}',claims['sha']) is not None,
                          run_id_shape=type(claims.get('run_id')) is str and claims['run_id'].isascii() and claims['run_id'].isdigit(),
                          timestamp_types=all(type(claims.get(k)) is int for k in ('exp','iat','nbf')),
                          jti_present=type(claims.get('jti')) is str and bool(claims['jti']))
            event['checks']=checks
            if isinstance(error,CIDenied) and error.code=='expired':event['reason']='expired'
        import sys
        print('BR02_CI_IDENTITY_DIAGNOSTIC '+json.dumps(event,sort_keys=True),file=sys.stderr)
    except Exception: pass  # A failed log sink never changes refusal.


def _authenticated_identity(environ):
    stage='environment'; claims=None; expected=None; request_url=None
    try:
        if environ.get('GITHUB_ACTIONS') != 'true' or environ.get('BR02_CI_APPROVED_SHA') != environ.get('GITHUB_SHA'): deny('identity')
        stage='request_url'
        request_url=environ['ACTIONS_ID_TOKEN_REQUEST_URL']
        address = urlsplit(request_url)
        if address.scheme != 'https' or address.hostname not in REQUEST_HOSTS \
                or address.username or address.password or address.port not in (None,443): deny('identity')
        query = dict(parse_qsl(address.query)); query['audience'] = AUDIENCE
        url = urlunsplit((address.scheme,address.netloc,address.path,urlencode(query),''))
        stage='request_environment'
        request = urllib.request.Request(url, headers={'Authorization':'Bearer '+environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']})
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs): deny('identity')
        stage='token_acquisition'
        opener=urllib.request.build_opener(NoRedirect())
        with opener.open(request, timeout=10) as response:
            raw = response.read(32769)
        stage='token_response'
        if len(raw)>32768: deny('identity')
        token=json.loads(raw)['value']
        stage='jwt_verification'
        claims = _verify_jwt(token)
        stage='claim_binding'
        expected={'run_id':environ['GITHUB_RUN_ID'],'sha':environ['GITHUB_SHA']}
        identity=validate_claims(claims, expected, time.time())
        return identity,claims['jti']
    except CIDenied as error:
        _identity_diagnostic(stage,error,claims,expected,request_url)
        raise
    except Exception as error:
        _identity_diagnostic(stage,error,claims,expected,request_url)
        raise CIDenied('identity') from None


def validate_target(info, identity):
    try:
        if type(info) is not dict or re.fullmatch('[0-9a-f]{64}',info['Id']) is None: deny('target')
        if info['Config']['Image'] != IMAGE or not info['State']['Running']: deny('target')
        bindings = info['NetworkSettings']['Ports']['5432/tcp']
        if bindings != [{'HostIp':'127.0.0.1','HostPort':'55432'}]: deny('target')
        networks = info['NetworkSettings']['Networks']
        if len(networks)!=1: deny('target')
        ip = next(iter(networks.values()))['IPAddress']
        import ipaddress
        if not ipaddress.ip_address(ip).is_private or ':' in ip: deny('target')
        mounts=info['Mounts']
        if len(mounts)!=1 or mounts[0]['Type']!='volume' or mounts[0]['Destination']!='/var/lib/postgresql/data': deny('target')
        volume=mounts[0]['Name']
        if re.fullmatch('[0-9a-f]{64}',volume) is None: deny('target')
        return {'instance':info['Id'],'storage':volume,'server_address':ip,'run_id':identity['run_id'],
                'sha':identity['sha'],'attempt':identity['attempt'],'repository':REPOSITORY,
                'workflow':WORKFLOW,'scope':SCOPE,'database':'br02_ci','endpoint':'127.0.0.1:55432'}
    except CIDenied: raise
    except Exception: raise CIDenied('target') from None


@dataclass(frozen=True)
class Context:
    approval: object
    binding: dict
    expires: float
    child: bool = False
    capability_fd: int = -1


def _fingerprint(value):
    # Detect mutation of every authorization-bearing field, not just target IDs.
    approval=dict(vars(value.approval))
    manifest=approval.pop('approved_manifest')
    approval['manifest']={'binding':dict(manifest.binding),'digest':manifest.digest}
    data={'binding':value.binding,'approval':approval,'expires':value.expires,
          'child':value.child,'fd':value.capability_fd}
    return hashlib.sha256(json.dumps(data,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).digest()


def _register(value):
    # Retained as a refusal for old callers. Public Context construction and
    # caller-computable fingerprints are never issuer provenance.
    deny('context')


def _target_key(binding):
    try:
        import ipaddress
        fixed={'repository':REPOSITORY,'workflow':WORKFLOW,'scope':SCOPE,'attempt':'1',
               'database':'br02_ci','endpoint':'127.0.0.1:55432'}
        if type(binding) is not dict or any(binding.get(k)!=v for k,v in fixed.items()): deny('target')
        for k,size in (('instance',64),('storage',64),('sha',40)):
            if type(binding.get(k)) is not str or re.fullmatch('[0-9a-f]{'+str(size)+'}',binding[k]) is None: deny('target')
        run=binding['run_id']
        if type(run) is not str or not run.isascii() or not run.isdigit(): deny('target')
        ip=ipaddress.ip_address(binding['server_address'])
        if ip.version!=4 or not ip.is_private: deny('target')
        return tuple((k,binding[k]) for k in sorted((*fixed,'instance','storage','sha','run_id','server_address')))
    except CIDenied: raise
    except Exception: raise CIDenied('target') from None


def _check_approval(approval,binding):
    import br02_pg_target as target
    manifest=target.require_approved_manifest(approval.approved_manifest)
    expected={'instance':binding['instance'],'storage':binding['storage'],'run_id':binding['run_id'],
              'loopback_endpoint':binding['endpoint'],'database':binding['database'],
              'artifact_hash':binding['sha'],'phase':SCOPE}
    if any(manifest.binding.get(k)!=v for k,v in expected.items()) or approval.run_marker!=binding['run_id'] \
            or approval.attested_server_address!=binding['server_address'] \
            or approval.attested_server_port!=5432 or approval.attested_server_version_prefix!='PostgreSQL 17.11' \
            or approval.execution_authorized is not True or approval.rollback_drill_authorized \
            or approval.rollback_drill_dsn: deny('context')


def make_approval(binding, dsns):
    import br02_pg_target as target
    import br02_pg_fixtures as fixtures
    record,_=target.synthetic_approved_manifest()
    record.update(instance=binding['instance'],storage=binding['storage'],loopback_endpoint=binding['endpoint'],
        database=binding['database'],run_id=binding['run_id'],role_inventory=['marker_app_login','marker_worker_login'],
        artifact_hash=binding['sha'],phase='br02.ci.integration',resource_cost_usd=0)
    record['independently_verified']={'verifier':'GitHub signed OIDC + owned Docker inspection','evidence':binding['sha']}
    manifest=target.supply_approved_manifest(record,target.manifest_digest(record))
    return fixtures.SeedApproval(approved_manifest=manifest,execution_authorized=True,approved_by='GitHub owner workflow',
        approval_ref=binding['run_id'],target_dsn=dsns['fixture'],app_dsn=dsns['app'],worker_dsn=dsns['worker'],
        runtime_login='marker_app_login',run_marker=binding['run_id'],attested_server_address=binding['server_address'],
        attested_server_port=5432,attested_server_version_prefix='PostgreSQL 17.11')


def _read_child(environ):
    # The inherited anonymous FD is the capability. Environment claims alone
    # never issue authority. Only the restricted child context is restored.
    try:
        fd=int(environ.pop('BR02_CI_FD'))
        if fd<=2: deny('child')
        import stat,fcntl
        try:
            info=os.fstat(fd)
            if info.st_uid!=0 or info.st_nlink!=0 or not stat.S_ISREG(info.st_mode) \
                    or fcntl.fcntl(fd,fcntl.F_GETFL)&os.O_ACCMODE != os.O_RDONLY: deny('child')
            raw=os.pread(fd,32769,0)
        finally: os.close(fd)
        if len(raw)<65 or len(raw)>32768 or not hmac.compare_digest(raw[32:64],hmac.digest(raw[:32],raw[64:],'sha256')): deny('child')
        value=json.loads(raw[64:])
        if type(value) is not dict or set(value)!={'binding','app','expires'}: deny('child')
        binding=value['binding']; expiry=value['expires']
        _target_key(binding)
        for field,key in (('BR02_CI_RUN_ID','run_id'),('BR02_CI_SHA','sha'),('BR02_CI_ATTEMPT','attempt')):
            if environ.pop(field,None)!=binding[key]: deny('child')
        if type(value['app']) is not str or not value['app']: deny('child')
        if type(expiry) not in (float,int) or not 0<expiry-time.monotonic()<=480: deny('expired')
        # Parent-created FD must be an anonymous regular file, and this child
        # must be sandboxed under the CI test UID rather than root/runner.
        if os.name!='posix' or os.getuid()!=60001: deny('child')
        approval=make_approval(binding,{'fixture':'bootstrap-not-forwarded','app':value['app'],'worker':''})
        return Context(approval,binding,expiry,True)
    except CIDenied: raise
    except Exception: raise CIDenied('child') from None



def _authority():
    """One issuer-owned registry, inaccessible to Context/registration callers.

    This is an API provenance boundary within trusted Python source, not a
    sandbox against code replacement, closure introspection or a hostile root.
    Only authenticated issuance and OS-validated inherited capabilities insert
    entries; a public registration/fingerprint operation cannot insert them.
    """
    current=ContextVar('br02_ci_only_context',default=None)
    issued={}; live={}; seen=set()

    def authenticate(environ):
        identity,jti=_authenticated_identity(environ)
        if jti in seen or len(seen)>=1024: deny('identity')
        seen.add(jti)
        ticket=object()
        issued[ticket]={'identity':dict(identity),'expires':time.monotonic()+300,'target':None}
        return dict(identity),ticket

    def require_ticket(ticket,binding):
        record=issued.get(ticket)
        if record is None or time.monotonic()>=record['expires'] or type(binding) is not dict: deny('context')
        identity=record['identity']
        if any(binding.get(k)!=identity[k] for k in ('run_id','sha','attempt')): deny('context')
        key=_target_key(binding)
        if record['target'] is None: record['target']=key
        elif record['target']!=key: deny('target')
        if os.name!='posix' or os.geteuid()!=0: deny('isolation')

    def active():
        value=current.get()
        record=live.get(id(value))
        try:
            return type(value) is Context and record is not None and record[0] is value \
                and record[1]==_fingerprint(value) and time.monotonic()<value.expires
        except Exception: return False

    def authorize(approval=None):
        if not active(): deny('context')
        value=current.get()
        if approval is None:
            if not value.child: deny('context')
            return
        if approval is not value.approval: deny('context')
        _check_approval(approval,value.binding)

    def _child_capability(binding, app_dsn, expiry):
        import secrets
        key=secrets.token_bytes(32)
        # Only restricted credentials cross this boundary. No bootstrap DSN.
        data={'binding':dict(binding),'app':app_dsn,'expires':expiry}
        raw=json.dumps(data,sort_keys=True,separators=(',',':')).encode()
        signed=hmac.digest(key,raw,'sha256')
        payload=key+signed+raw
        if len(payload)>32768: deny('child')
        import tempfile
        with tempfile.TemporaryFile() as stream:
            stream.write(payload); stream.flush()
            # Root-owned, unlinked, read-only descriptor; no writable descriptor is
            # inherited by the unprivileged worker or API grandchildren.
            return os.open('/proc/self/fd/'+str(stream.fileno()),os.O_RDONLY)

    @contextlib.contextmanager
    def installed(approval,binding,*,ticket=None,seconds=480):
        require_ticket(ticket,binding)
        if type(seconds) not in (int,float) or not 0<seconds<=480: deny('context')
        _check_approval(approval,binding)
        # Consume before creating transport: failed issuance cannot replay.
        issued.pop(ticket)
        expiry=time.monotonic()+seconds
        try: fd=_child_capability(binding,approval.app_dsn,expiry)
        except CIDenied: raise
        except Exception: raise CIDenied('child') from None
        value=Context(approval,dict(binding),expiry,False,fd)
        token=None
        try:
            live[id(value)]=(value,_fingerprint(value))
            token=current.set(value)
            yield value
        finally:
            if token is not None: current.reset(token)
            live.pop(id(value),None)
            os.close(fd)

    @contextlib.contextmanager
    def fixture_context(approval):
        if not active() or current.get().child: deny('context')
        parent=current.get()
        _check_approval(approval,parent.binding)
        if approval.app_dsn!=parent.approval.app_dsn or approval.worker_dsn!=parent.approval.worker_dsn \
                or approval.runtime_login!=parent.approval.runtime_login: deny('context')
        value=Context(approval,dict(parent.binding),parent.expires,False,parent.capability_fd)
        live[id(value)]=(value,_fingerprint(value)); token=current.set(value)
        try: yield
        finally:
            current.reset(token); live.pop(id(value),None)

    def child_transport(approval):
        authorize(approval)
        if current.get().child: deny('child')
        fd=os.dup(current.get().capability_fd)
        try: stream=os.fdopen(fd,'rb')
        except Exception:
            os.close(fd)
            raise CIDenied('child') from None
        binding=current.get().binding
        return stream,{'BR02_CI_FD':str(fd),'BR02_CI_RUN_ID':binding['run_id'],
                       'BR02_CI_SHA':binding['sha'],'BR02_CI_ATTEMPT':binding['attempt']}

    def adopt_child(environ):
        # No caller-constructed Context or approval is accepted here. Root-owned
        # anonymous read-only transport plus the sandbox UID establish provenance.
        if current.get() is not None: deny('child')
        value=_read_child(environ)
        _check_approval(value.approval,value.binding)
        live[id(value)]=(value,_fingerprint(value)); current.set(value)

    return current,authenticate,require_ticket,active,authorize,installed,fixture_context,child_transport,adopt_child


(_current,authenticate,require_ticket,active,authorize,installed,
 fixture_context,child_transport,adopt_child)=_authority()
del _authority

# Whichever spelling is imported first owns the single authority registry.
import sys as _sys
_sys.modules.setdefault('br02_pg_ci_context',_sys.modules[__name__])
_sys.modules.setdefault('tests.br02_pg_ci_context',_sys.modules[__name__])
