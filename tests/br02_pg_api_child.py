"""BR-02 live API child-process launcher (source only; never executed in
this package).

LIVE-GATED
----------
Every entry point first passes the unconditional execution gate in
``br02_pg_target`` (``authorize_execution``), which refuses until a future,
independently approved target plus separate execution authorization exists.
No environment variable, DSN string, or flag can unlock anything here.
Importing this module performs no I/O, imports no database driver, imports no
server machinery, spawns no process - and never touches the parent's
production DSN value.

Child process properties (future live path)
-------------------------------------------
* separate allowlisted environment: ONLY the ``CHILD_ENV_ALLOWLIST`` names
  are read from the parent environment - never a wholesale copy, and never
  ``AI_MARKER_PG_DSN`` - so credentials (engine keys, Supabase keys, the
  production DSN) can never leak into the child. The child's DSN comes ONLY
  from the separately approved RESTRICTED runtime binding
  (``SeedApproval.app_dsn`` bound to ``runtime_login``), never from the
  fixture owner's administrative ``target_dsn``;
* restricted runtime handle, never administrative: the approved manifest's
  role inventory is checked against
  ``br02_pg_target.assert_restricted_roles``;
* production policy is forced on (``AI_MARKER_RUNTIME=production``): the
  child exercises the REAL fail-closed production composition with the REAL
  result/audit/membership/job adapters - nothing fake is injected for
  persistence, membership, or provenance. Fresh completion stays denied
  (default production posture);
* DISCLOSED test-auth boundary: the ONLY synthetic component is the token
  verifier (``synthetic_verifier`` below). It is labeled as synthetic, never
  presented as real Google OIDC verification, and it never touches
  persistence, membership, or provenance. The REAL OIDC path (and
  ``api.server.build_application`` with its ``require_capability`` startup
  proof) remains the future operator composition;
* inert synthetic external config: the engine is disabled
  (``AI_MARKER_ENGINE_REAL=0``) and the OIDC audience is a synthetic test
  value, so required-config validation runs without any real secret; TLS is
  never relaxed (nothing touches TLS);
* external-network hard block in the child: outbound attempts to any
  NON-loopback destination raise; loopback (the PG target and the child's
  own HTTP server) stays reachable;
* reusable child CLI: ``python -m tests.br02_pg_api_child --serve --port N``
  gates, hard-blocks, builds the real app, and serves on loopback with a
  readiness endpoint - the parent drives requests against it and owns the
  bounded lifecycle (terminate -> wait -> kill -> wait; owned PID only; no
  generic external argv execution - the argv is constructed internally).
"""

import sys
from pathlib import Path
from typing import Optional

# Import the target module from the tests package for consistent resolution
# whether launched as `-m tests.br02_pg_api_child` or imported directly.
from tests import br02_pg_target as target
import runtime_config

ROOT = Path(__file__).resolve().parents[1]

SYNTHETIC_VERIFIER_DISCLOSURE = (
    "The live API child verifies tokens with a disclosed SYNTHETIC verifier "
    "(explicitly labeled test authentication; token format "
    "'synthetic-subject:<subject>'). It is never presented as real Google "
    "OIDC verification, and it never substitutes for persistence, "
    "membership, or provenance - those stay the real composition. The real "
    "OIDC path is reached only in the future operator composition.")

# The ONLY variable names read from the parent environment into the child.
CHILD_ENV_ALLOWLIST = (
    "PATH",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "PYTHONUTF8",
    "PYTHONIOENCODING",
)

# Controlled values the child always receives regardless of the parent.
CHILD_ENGINE_REAL = "0"  # engine disabled: no external engine calls

# Failure-only CLI codes; the parent serializes only these fixed stage names.
# No stderr, arbitrary exception strings, environment or credentials cross.
CHILD_FAILURE_STAGES = {3: 'child_authorization', 70: 'child_network_guard',
                        71: 'child_application', 72: 'child_server_import',
                        73: 'child_server_run'}


def _child_failure(stage):
    failure = RuntimeError('BR02 child exited before readiness')
    failure.br02_child_stage = stage
    return failure


def _loopback(host: str) -> bool:
    host = (host or "").strip("[]").lower()
    return (host == "localhost" or host.startswith("127.")
            or host == "::1" or host == "0:0:0:0:0:0:0:1")


def install_child_loopback_only_block() -> dict:
    """Hard-block every outbound attempt to a NON-loopback destination while
    keeping loopback reachable (the PG target and the child's own HTTP
    server). Idempotent; makes no attempt itself."""
    import socket

    blocked = []

    def _guard(address):
        host = address[0] if isinstance(address, tuple) else ""
        if not _loopback(str(host)):
            raise target.TargetRefused(
                target.PRODUCTION_DSN_FALLBACK,
                "child external network reach blocked: only loopback "
                "destinations are reachable from the BR-02 API child")

    current = getattr(socket.socket.connect, "_br02_child_block", False)
    if not current:
        original_connect = socket.socket.connect

        def _guarded_connect(self, address, *args, **kwargs):
            _guard(address)
            return original_connect(self, address, *args, **kwargs)
        _guarded_connect._br02_child_block = True
        socket.socket.connect = _guarded_connect
        blocked.append("socket.connect")

        def _guarded_connect_ex(self, address, *args, **kwargs):
            _guard(address)
            return original_connect(self, address, *args, **kwargs) \
                if hasattr(socket.socket, "connect_ex") else None
        _guarded_connect_ex._br02_child_block = True
        socket.socket.connect_ex = _guarded_connect_ex
        blocked.append("socket.connect_ex")

        def _guarded_sendto(self, data, address, *args, **kwargs):
            _guard(address)
            raise target.TargetRefused(
                target.PRODUCTION_DSN_FALLBACK,
                "child external datagram send blocked: loopback only")
        _guarded_sendto._br02_child_block = True
        socket.socket.sendto = _guarded_sendto
        blocked.append("socket.sendto")
    return {"socket": "loopback-only" if blocked else "already",
            "blocked": blocked}


def _bind_restricted_app_dsn(approval, dsn: str) -> str:
    """Return the approved RESTRICTED runtime app DSN, or refuse.

    The runtime handle is the separately supplied ``approval.app_dsn`` bound
    to ``approval.runtime_login`` and to the manifest's restricted role
    inventory. The administrative ``target_dsn`` is never used, and an
    ``app_dsn`` equal to it is refused as misbound. An approval that lacks
    the restricted binding is refused outright: there is no legacy,
    permissive, or administrative fallback - not even for offline helpers.
    """
    manifest = getattr(approval, "approved_manifest", None)
    binding = getattr(manifest, "binding", None) or {}
    target.assert_restricted_roles(binding.get("role_inventory"))
    app_dsn = getattr(approval, "app_dsn", "")
    runtime_login = getattr(approval, "runtime_login", "")
    admin_dsn = getattr(approval, "target_dsn", "")
    if not isinstance(app_dsn, str) or not app_dsn.strip():
        raise target.TargetRefused(
            target.TARGET_IDENTITY_INCOMPLETE,
            "the child requires the separately approved restricted app_dsn "
            "for the runtime login; the administrative target DSN is never "
            "used")
    if not isinstance(runtime_login, str) or not runtime_login.strip():
        raise target.TargetRefused(
            target.TARGET_IDENTITY_INCOMPLETE,
            "the child requires the attested runtime_login bound to app_dsn")
    if not isinstance(dsn, str) or not dsn.strip():
        raise target.TargetRefused(
            target.PRODUCTION_DSN_FALLBACK,
            "the child needs the approved restricted runtime app DSN")
    if dsn != app_dsn:
        raise target.TargetRefused(
            target.PRODUCTION_DSN_FALLBACK,
            "the child DSN must be the approved restricted app_dsn, supplied "
            "independently of the parent environment")
    if isinstance(admin_dsn, str) and admin_dsn.strip() \
            and app_dsn == admin_dsn:
        raise target.TargetRefused(
            target.INCONSISTENT_TARGET,
            "the child runtime handle must never be the administrative "
            "target_dsn")
    return app_dsn


def build_child_environ(parent_environ, approval, dsn: str) -> dict:
    """Build the child's allowlisted environment. Refuses unless the
    execution gate allows the supplied approval (it refuses today).

    The DSN is bound to the separately approved RESTRICTED ``app_dsn``
    carried by the approval - the parent environment is read ONLY for the
    neutral allowlist names, never for any DSN, so no production credential
    value is ever read here."""
    target.authorize_execution(approval)
    dsn = _bind_restricted_app_dsn(approval, dsn)
    child = {}
    for name in CHILD_ENV_ALLOWLIST:
        value = parent_environ.get(name)
        if value is not None:
            child[name] = value
    child[runtime_config.RUNTIME_ENV] = runtime_config.RUNTIME_PRODUCTION
    child[runtime_config.PG_DSN_ENV] = dsn
    child[runtime_config.ENGINE_REAL_ENV] = CHILD_ENGINE_REAL
    child[runtime_config.OIDC_AUDIENCE_ENV] = getattr(
        approval, "oidc_audience", "synthetic-test-audience")
    # The child imports this module as tests.br02_pg_api_child: give it the
    # repo root explicitly, derived from this file - never from the parent.
    child["PYTHONPATH"] = str(ROOT)
    return child


def synthetic_verifier(token: str) -> str:
    """The disclosed synthetic test verifier. Accepts ONLY tokens shaped
    ``synthetic-subject:<subject>``; everything else is refused. It never
    performs network I/O and never mints persistence rights."""
    if isinstance(token, str) and token.startswith("synthetic-subject:"):
        subject = token.split(":", 1)[1].strip()
        if subject and len(subject) <= 128 and all(
                part.isalnum() for part in subject.replace("-", "a")
                .replace("_", "a").split(".")):
            return subject
    raise ValueError("synthetic_verifier_refused")


def build_child_application(environ: Optional[dict] = None, *,
                            approval=None):
    """Compose the REAL production application for the child. The API
    machinery import happens here - strictly behind the execution gate -
    never at module import. The ONLY synthetic boundary is the disclosed
    token verifier; persistence, membership, and provenance stay real."""
    target.authorize_execution(approval)
    import auth_oidc
    import os
    from api.app import create_app  # delayed behind the gate

    def verify_token(token: str) -> str:
        try:
            return synthetic_verifier(token)
        except ValueError:
            raise auth_oidc.OidcError("synthetic_verifier_refused") from None

    environ = dict(environ or {})
    port = int(environ.get("BR02_CHILD_PORT") or "8000")
    runtime = environ.get(runtime_config.RUNTIME_ENV)
    dsn = environ.get(runtime_config.PG_DSN_ENV)
    if runtime != runtime_config.RUNTIME_PRODUCTION or not dsn:
        raise target.TargetRefused(
            target.PRODUCTION_DSN_FALLBACK,
            "the child application needs the production runtime and the "
            "approved restricted app DSN in its process environment")
    if approval is not None:
        # In-process callers can re-verify the binding. The child CLI has no
        # approval object (it receives only the parent-built allowlisted
        # environment); the parent already enforced this binding in
        # ``build_child_environ`` before spawning, and no approval is
        # invented here.
        _bind_restricted_app_dsn(approval, dsn)
    # The child process keeps the production runtime and the restricted
    # handle in its OWN process environment for its whole lifetime:
    # create_app classifies production at build time and the adapters
    # re-read the environment per request.
    os.environ[runtime_config.RUNTIME_ENV] = runtime_config.RUNTIME_PRODUCTION
    os.environ[runtime_config.PG_DSN_ENV] = dsn
    return create_app(
        verify_token=verify_token,
        engine=None,
        # CORS is a browser policy: the synthetic https origin need not match
        # the child's loopback HTTP transport, and TLS rules stay strict.
        allowed_origins=[f"https://127.0.0.1:{port}"],
    )


def child_main(argv=None) -> int:
    """The child CLI: ``python -m tests.br02_pg_api_child --serve --port N``.

    Order of operations: gate -> argv -> external-network hard block -> real
    app composition -> loopback HTTP serving. The gate runs before any server
    import and before any OS resource is touched."""
    import os
    from tests import br02_pg_ci_context as ci
    if 'BR02_CI_FD' in os.environ:
        try:
            ci.adopt_child(os.environ)
        except ci.CIDenied:
            print('BR02 child refused: invalid_ci_context', file=sys.stderr)
            return 3
    try:
        target.authorize_execution(None)
    except target.TargetRefused as exc:
        print(f"BR02 child refused: {exc.reason}", file=sys.stderr)
        return 3

    import argparse

    parser = argparse.ArgumentParser(
        prog="tests.br02_pg_api_child",
        description="BR-02 live API child (gated; future live path only)")
    parser.add_argument("--serve", action="store_true",
                        help="serve the real production composition on "
                             "loopback")
    parser.add_argument("--port", type=int, default=8000,
                        help="loopback port to serve on")
    args = parser.parse_args(argv)
    if not args.serve or args.port <= 0 or args.port > 65535:
        parser.print_usage(sys.stderr)
        return 2

    try:
        install_child_loopback_only_block()
    except Exception:
        return 70
    import os

    environ = dict(os.environ)
    environ["BR02_CHILD_PORT"] = str(args.port)
    approval = ci._current.get().approval if ci.active() else None
    try:
        app = build_child_application(environ, approval=approval)
    except Exception:
        return 71

    try:
        import uvicorn  # delayed behind the gate
    except Exception:
        return 72

    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port,
                    log_level="warning", access_log=False)
    except (Exception, SystemExit):
        return 73
    return 0


class _ChildHandle:
    """One owned child server process: bounded request path and a strict
    terminate -> wait -> kill -> wait shutdown. Owned PID only."""

    def __init__(self, process, port: int, timeout_seconds: float):
        self._process = process
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        self._timeout_seconds = timeout_seconds

    @property
    def pid(self) -> int:
        return self._process.pid

    def request(self, method: str, path: str, headers=None, body: bytes = b"",
                timeout: Optional[float] = None):
        """One bounded loopback HTTP request against the child."""
        import http.client

        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port,
            timeout=timeout or self._timeout_seconds)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), \
                response.read()
        finally:
            connection.close()

    def stop(self) -> int:
        """Bounded owned-PID shutdown: terminate -> wait -> kill -> wait."""
        process = self._process
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except Exception:
                process.kill()
                process.wait(timeout=5)
        return process.returncode

    # Context-manager form: the child is ALWAYS reaped.
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.stop()
        return False


def run_child_server(approval, *, ready_timeout_seconds: float = 15.0,
                     timeout_seconds: float = 60.0) -> _ChildHandle:
    """Spawn ONE owned child server and wait for readiness (future live path
    only). The gate runs before any OS process is created; the argv is
    constructed internally - no generic external argv execution."""
    target.authorize_execution(approval)
    import http.client
    import os
    import socket
    import subprocess
    import time

    # Read the parent environment ONLY through the allowlisted builder, and
    # bind the child to the approved RESTRICTED runtime handle - never the
    # administrative target DSN.
    environ = build_child_environ(
        os.environ, approval,
        getattr(approval, "app_dsn", ""))

    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    finally:
        probe.close()

    argv = [sys.executable, "-B", "-m", "tests.br02_pg_api_child",
            "--serve", "--port", str(port)]
    from tests import br02_pg_ci_context as ci
    if ci.active():
        stream, extra = ci.child_transport(approval)
        try:
            environ.update(extra)
            process = subprocess.Popen(argv, env=environ, cwd=str(ROOT),
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,pass_fds=(stream.fileno(),))
        finally:
            stream.close()
    else:
        process = subprocess.Popen(argv, env=environ, cwd=str(ROOT),
                                   stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    handle = _ChildHandle(process, port, timeout_seconds)
    deadline = time.monotonic() + ready_timeout_seconds
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise _child_failure(CHILD_FAILURE_STAGES.get(
                    process.returncode, 'child_ready'))
            try:
                status, _headers, _body = handle.request(
                    "GET", "/v1/healthz", timeout=1.0)
                if status == 200:
                    return handle
            except OSError:
                pass
            time.sleep(0.1)
        raise TimeoutError("BR02 child did not become ready in time")
    except Exception:
        handle.stop()
        raise


def launch_child(child_environ: dict, argv, timeout_seconds: float, *,
                 approval) -> dict:
    """Deprecated generic runner - retained ONLY as a refusal: generic
    external argv execution is not permitted. Use :func:`run_child_server`,
    whose argv is constructed internally and whose lifecycle is owned."""
    target.authorize_execution(approval)
    raise target.TargetRefused(
        target.EXECUTION_NOT_AUTHORIZED,
        "generic external argv execution is refused; use run_child_server "
        "(owned, internally constructed argv, bounded lifecycle)")
