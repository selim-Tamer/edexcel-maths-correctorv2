"""BR-02-PG-HARNESS offline runner.

Usage:
    python -B tests/run_br02_pg_harness.py --mode offline

Hard guarantees enforced here, BEFORE any test module is imported:
* bytecode writing is disabled (``sys.dont_write_bytecode = True``; also
  pass ``-B`` so the runner itself compiles without writing bytecode);
* outbound network entry points (TCP connect / connect_ex / sendto /
  create_connection) and DNS resolution (getaddrinfo / gethostbyname /
  gethostbyname_ex) are hard-blocked, the PostgreSQL driver entry points
  (module and class connect paths) are hard-blocked when the driver is
  installed, and every installed block is self-verified before any test
  module is imported - so no test import or test body can reach a database
  or resolve a host;
* only the exact module list of this authorized slice is loaded.

Live PostgreSQL mode is REFUSED unconditionally in this slice: it requires an
independently approved disposable target record AND separately authorized
execution. No environment variable (``AI_MARKER_TEST_PG_DSN`` or
``AI_MARKER_PG_DSN``), DSN string, or flag can unlock it.
"""

import sys

sys.dont_write_bytecode = True  # bytecode off even if -B is forgotten

import argparse
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Exact module list of the BR-02-PG-HARNESS approved 18-file scope: all
# approved harness modules are imported (import-safety gate under active
# blocks) and all approved test modules run. No broader discovery exists.
IMPORT_GATE_MODULES = ("br02_pg_target", "br02_pg_fixtures",
                       "br02_pg_api_child")
TEST_MODULES = (
    "tests.test_br02_pg_preflight",
    "tests.test_postgres_result_store_contract",
    "tests.test_postgres_api_pg_contract",
    "tests.test_br02_pg_registration",
    "tests.test_br02_pg_api",
    "tests.test_br02_pg_transactions",
    "tests.test_br02_pg_concurrency",
    "tests.test_br02_pg_processes",
    "tests.test_br02_pg_rls_grants",
    "tests.test_br02_pg_worker_denial",
    "tests.test_br02_pg_rollback",
)

# The ONLY names read from the real environment into the synthetic mapping.
NEUTRAL_ENV_KEYS = (
    "PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT",
    "TEMP", "TMP", "PYTHONUTF8", "PYTHONIOENCODING",
)


class HarnessImportBlocked(RuntimeError):
    """Raised when offline code tries to import a runtime-only module
    (``api.server``) or to spawn an OS process. Carries the stable refusal
    marker so identity never matters."""

    _br02_harness_refusal = True


class _SyntheticEnviron(dict):
    """A test-mutable stand-in for ``os.environ``.

    Seeded ONLY with the explicitly neutral names, each read individually
    from the real mapping (no copy, no items/values/iteration of the real
    environment, so no secret value is ever inspected). The whole suite then
    runs against THIS mapping: ``mock.patch.dict`` and friends copy the
    synthetic map, never the ambient environment, and ambient credentials
    are unreachable by construction.
    """

    def __init__(self, source, keys):
        super().__init__()
        for name in keys:
            value = source.get(name)  # explicitly neutral reads only
            if value is not None:
                self[name] = value


def install_environment_guard():
    """Rebind ``os.environ`` to a synthetic neutral-allowlisted mapping
    BEFORE any test import, by object-reference rebinding. Returns the real
    mapping REFERENCE for the caller's finally-restore; the real environment
    is never copied, iterated, or inspected beyond the neutral names."""
    import os

    real = os.environ  # reference only
    os.environ = _SyntheticEnviron(real, NEUTRAL_ENV_KEYS)
    return real


def restore_environment(real):
    """Restore the ORIGINAL mapping REFERENCE (never a copy)."""
    import os

    os.environ = real


def install_offline_process_block() -> list:
    """Refuse OS process spawns and ``api.server`` imports in offline mode.
    Tests that need process semantics must mock them explicitly. Idempotent;
    makes no attempt itself."""
    import builtins
    import importlib.abc
    import os
    import subprocess

    blocked = []

    def _refuse(*_args, **_kwargs):
        raise HarnessImportBlocked(
            "offline OS process spawn refused by the BR-02 PG harness; "
            "process behavior is covered by the gated live path only")

    if not getattr(subprocess.Popen, "_br02_harness_block", False):
        subprocess.Popen = _refuse
        blocked.append("subprocess.Popen")
    if not getattr(os.system, "_br02_harness_block", False):
        os.system = _refuse
        blocked.append("os.system")
    if not getattr(os.popen, "_br02_harness_block", False):
        os.popen = _refuse
        blocked.append("os.popen")

    class _RuntimeImportBlocker(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "api.server":
                raise HarnessImportBlocked(
                    "api.server imports are refused offline: the startup "
                    "entrypoint runs only behind the live gate")
            return None

    if not any(isinstance(f, _RuntimeImportBlocker)
               for f in sys.meta_path):
        sys.meta_path.insert(0, _RuntimeImportBlocker())
        blocked.append("import:api.server")
    del builtins
    return blocked


class HarnessConnectionBlocked(RuntimeError):
    """Raised instead of any real network, DNS, or database attempt.

    Carries a stable marker attribute so refusals are recognizable even when
    this module is loaded twice (as ``__main__`` and as an imported module):
    matching is done on the marker, never on class identity.
    """

    _br02_harness_refusal = True


def _wrap(owner, name, factory):
    """Install one blocking wrapper (idempotent per entry point)."""
    current = getattr(owner, name, None)
    if current is not None and getattr(current, "_br02_harness_block", False):
        return False
    wrapper = factory()
    wrapper._br02_harness_block = True
    setattr(owner, name, wrapper)
    return True


def install_connection_blocks() -> dict:
    """Hard-block outbound network, DNS, and PostgreSQL driver entry points.

    Idempotent. Performs no network, DNS, or database attempt itself: every
    installed wrapper raises before any syscall that could transmit data or
    resolve a host.
    """
    import socket

    blocked = []

    def _socket_method(name, message):
        def _blocked(self, *args, **kwargs):
            raise HarnessConnectionBlocked(message)
        return _blocked

    socket_message = "network reach blocked by BR-02 PG harness (offline only)"
    dns_message = "DNS resolution blocked by BR-02 PG harness (offline only)"

    if _wrap(socket.socket, "connect",
             lambda: _socket_method("connect", socket_message)):
        blocked.append("socket.connect")
    if _wrap(socket.socket, "connect_ex",
             lambda: _socket_method("connect_ex", socket_message)):
        blocked.append("socket.connect_ex")
    if _wrap(socket.socket, "sendto",
             lambda: _socket_method("sendto", socket_message)):
        blocked.append("socket.sendto")

    def _blocked_create_connection(address, *args, **kwargs):
        raise HarnessConnectionBlocked(socket_message)
    if _wrap(socket, "create_connection", lambda: _blocked_create_connection):
        blocked.append("socket.create_connection")

    def _blocked_getaddrinfo(host, port, *args, **kwargs):
        raise HarnessConnectionBlocked(dns_message)
    if _wrap(socket, "getaddrinfo", lambda: _blocked_getaddrinfo):
        blocked.append("socket.getaddrinfo")

    def _blocked_gethostbyname(host):
        raise HarnessConnectionBlocked(dns_message)
    if _wrap(socket, "gethostbyname", lambda: _blocked_gethostbyname):
        blocked.append("socket.gethostbyname")

    def _blocked_gethostbyname_ex(host):
        raise HarnessConnectionBlocked(dns_message)
    if _wrap(socket, "gethostbyname_ex", lambda: _blocked_gethostbyname_ex):
        blocked.append("socket.gethostbyname_ex")

    status = {"socket": "blocked" if blocked else "already-blocked",
              "blocked": blocked, "psycopg": "absent"}

    try:
        import psycopg
    except ImportError:
        return status

    pg_message = ("PostgreSQL driver reach blocked by BR-02 PG harness "
                  "(offline only)")

    def _blocked_psycopg_connect(*args, **kwargs):
        raise HarnessConnectionBlocked(pg_message)

    driver_blocked = []
    if _wrap(psycopg, "connect", lambda: _blocked_psycopg_connect):
        driver_blocked.append("psycopg.connect")
    for cls_name in ("Connection", "AsyncConnection"):
        cls = getattr(psycopg, cls_name, None)
        if cls is not None and hasattr(cls, "connect"):
            if _wrap(cls, "connect", lambda: _blocked_psycopg_connect):
                driver_blocked.append(f"psycopg.{cls_name}.connect")
    status["psycopg"] = "blocked" if driver_blocked else "already-blocked"
    status["blocked"] = blocked + driver_blocked
    return status


def verify_blocks(status: dict) -> None:
    """Self-verify every installed block refuses, before any test import.

    Pure probes: each probed entry point raises ``HarnessConnectionBlocked``
    before any data is transmitted or host resolved, so this makes no real
    attempt; a probe that ever succeeded would mean the block failed.
    Methods are called unbound with ``self=None``, so no socket object is
    ever created.
    """
    import socket

    loopback = ("127.0.0.1", 5432)

    def _expect_blocked(label, probe):
        try:
            probe()
        except RuntimeError as exc:
            if getattr(exc, "_br02_harness_refusal", False) is True:
                return
            raise
        raise AssertionError(f"{label} block did not engage")

    _expect_blocked("socket.connect",
                    lambda: socket.socket.connect(None, loopback))
    _expect_blocked("socket.connect_ex",
                    lambda: socket.socket.connect_ex(None, loopback))
    _expect_blocked("socket.sendto",
                    lambda: socket.socket.sendto(None, b"x", loopback))
    _expect_blocked("socket.create_connection",
                    lambda: socket.create_connection(loopback))
    _expect_blocked("socket.getaddrinfo",
                    lambda: socket.getaddrinfo("127.0.0.1", 5432))
    _expect_blocked("socket.gethostbyname",
                    lambda: socket.gethostbyname("127.0.0.1"))
    _expect_blocked("socket.gethostbyname_ex",
                    lambda: socket.gethostbyname_ex("127.0.0.1"))

    if status.get("psycopg") in ("blocked", "already-blocked"):
        import psycopg

        _expect_blocked("psycopg.connect",
                        lambda: psycopg.connect("host=127.0.0.1"))
        for cls_name in ("Connection", "AsyncConnection"):
            cls = getattr(psycopg, cls_name, None)
            if cls is not None and hasattr(cls, "connect"):
                _expect_blocked(
                    f"psycopg.{cls_name}.connect",
                    lambda cls=cls: cls.connect("host=127.0.0.1"))


def _refuse_live(mode: str) -> int:
    print(
        f"REFUSED: harness mode '{mode}' is not available. Live PostgreSQL "
        "execution requires an independently approved disposable target "
        "record AND separately authorized execution; AI_MARKER_TEST_PG_DSN, "
        "AI_MARKER_PG_DSN, environment variables, DSN strings, or flags "
        "never unlock it.",
        file=sys.stderr,
    )
    return 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="BR-02 PG harness runner (offline only in this slice)")
    parser.add_argument(
        "--mode",
        default="offline",
        help="only 'offline' is available in this slice; anything else is "
             "refused")
    args = parser.parse_args(argv)
    if args.mode != "offline":
        return _refuse_live(args.mode)

    # ENV SAFETY FIRST: rebind os.environ to the synthetic neutral mapping
    # BEFORE any test import, so mock internals and module-level reads can
    # never copy or inspect ambient secret values. The original mapping
    # REFERENCE is restored in the finally below.
    real_environ = install_environment_guard()
    try:
        block_status = install_connection_blocks()
        verify_blocks(block_status)
        process_blocks = install_offline_process_block()

        # Import-safety gate: harness modules must import cleanly while
        # every network, DNS, driver, process, and runtime-import entry
        # point is already blocked.
        for module_name in IMPORT_GATE_MODULES:
            __import__(module_name)

        loader = unittest.TestLoader()
        suite = loader.loadTestsFromNames(TEST_MODULES)
        print(
            f"BR-02 PG harness: mode=offline bytecode_off={sys.dont_write_bytecode} "
            f"blocks={block_status.get('socket')}/{block_status.get('psycopg')} "
            f"entries={len(block_status.get('blocked', []))} "
            f"process_blocks={len(process_blocks)} "
            f"env_guard=synthetic-neutral modules={len(TEST_MODULES)}"
        )
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1
    finally:
        restore_environment(real_environ)


if __name__ == "__main__":
    sys.exit(main())
