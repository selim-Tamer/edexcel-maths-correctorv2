"""Inactive Stage-2 authorization boundary; imports perform no I/O.

There is deliberately no authority implementation, registration function,
environment setting, receipt-file loader, driver import or connector argument.
Only a separately reviewed host authority could make execute_stage2 usable.
Python monkeypatching in offline tests is not an authority deployment.
"""

import ast
import hashlib
import json
import math
import sys
import time
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


OPERATION = "br02.stage2.identity_catalog.readonly"
ROOT = Path(__file__).parent.parent
ENTRY_ARTIFACTS = ("tests/br02_pg_authorization.py", "tests/br02_pg_target.py")
MAX_SECONDS = 60
MAX_APPROVAL_WINDOW = 300
_dispatch = ContextVar("br02_stage2_dispatch", default=None)


class AuthorizationDenied(Exception):
    """Sanitized failure; never includes authority data or connection errors."""

    def __init__(self, reason):
        allowed = {
            "authority_unavailable", "dispatch_substitution", "artifact_path",
            "dynamic_dependency", "unsupported_dependency", "ambiguous_dependency",
            "unsupported_package_dependency", "missing_evidence", "operation_or_revocation",
            "attempt_limit", "duration_limit", "validity_window", "expired_or_not_yet_valid",
            "target_substitution", "target_identity", "manifest_substitution",
            "manifest_binding", "artifact_manifest", "deadline", "non_readonly_statement",
            "missing_reference", "consumption_refused", "post_consumption_change",
            "connected_identity_or_privilege", "statement_timeout",
            "evidence_or_dispatch_failure", "connection_cleanup_failure",
        }
        super().__init__(reason if type(reason) is str and reason in allowed
                         else "authorization_denied")


@dataclass(frozen=True)
class Binding:
    instance: str
    storage: str
    endpoint: str
    address: str
    port: int
    database: str
    restricted_login: str
    run_id: str
    # Exact attested version text, not a caller-selected broad prefix.
    server_version: str


@dataclass(frozen=True)
class Receipt:
    authorization_id: str
    owner_approval_ref: str
    target_review_ref: str
    manifest_digest: str
    artifact_digest: str
    artifacts: tuple
    binding: Binding
    operation: str
    not_before: int
    expires_at: int
    max_seconds: int
    max_attempts: int
    revoked: bool = False


@dataclass(frozen=True)
class ReviewedTarget:
    review_ref: str
    manifest_json: str
    manifest_digest: str
    binding: Binding


@dataclass(frozen=True)
class Permit:
    """Operation-bound dispatch value, never accepted as caller authority."""

    authorization_id: str
    evidence_digest: str
    binding: Binding
    operation: str
    deadline: float
    max_seconds: int
    expires_at: int


class TrustedAuthority(Protocol):
    """Future host contract, NOT an implemented trust provider.

    lookup authenticates explicit owner approval; reviewed_target obtains
    separately reviewed evidence. consume must durably/atomically verify
    current approval, revocation, expiry AND evidence digest and transition
    unused->consumed exactly once across threads/processes/restarts. An
    uncertain result burns the attempt; no refund or automatic retry.

    open_connection must bind the permit to independently verified instance
    and storage, exact endpoint/database/direct restricted login, and set
    server-enforced read-only/statement/connection time limits, a pinned
    pg_catalog, public search_path and a host-enforced total deadline
    (including blocked opens/queries/fetches/cleanup). No production
    DSN fallback, credential exposure, retry or alternate endpoint is allowed.
    Any future implementation and runtime/stdlib trust need separate review.
    """

    def lookup(self, reference: str) -> Receipt: ...
    def reviewed_target(self, reference: str) -> ReviewedTarget: ...
    def consume(self, receipt: Receipt, evidence_digest: str) -> bool: ...
    def open_connection(self, permit: Permit): ...


def _trusted_authority() -> TrustedAuthority:
    # No activation through ordinary repository configuration or caller data.
    raise AuthorizationDenied("authority_unavailable")


def _claim_stage2_dispatch(connection, approval, manifest):
    """Claim exactly one identity-bound entry; never a process-wide enable."""
    current = _dispatch.get()
    if current is None:
        return False
    handle, expected_approval, expected_manifest, deadline = current
    if (connection is not handle or approval is not expected_approval
            or manifest is not expected_manifest):
        _deny("dispatch_substitution")
    if time.monotonic() >= deadline:
        _deny("deadline")
    _dispatch.set(None)
    return True


def _deny(reason):
    raise AuthorizationDenied(reason)


def _sha(value):
    return (type(value) is str and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


def artifact_digest(entries):
    """Canonical digest; approval provenance comes only from the authority."""
    return hashlib.sha256(json.dumps(
        entries, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def _source(path):
    candidate = ROOT / path
    if candidate.is_symlink() or any(p.is_symlink() for p in candidate.parents):
        _deny("artifact_path")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(ROOT) or not resolved.is_file():
        _deny("artifact_path")
    return resolved.read_bytes()


def _artifact_inventory():
    """Discover the transitive local Python imports without importing them.

    All local source bytes are hashed. Unsupported/dynamic imports and
    external non-stdlib dependencies fail closed. Stdlib/interpreter identity
    is a host trust prerequisite, not falsely represented as source coverage.
    """
    pending, found = list(ENTRY_ARTIFACTS), {}
    while pending:
        path = pending.pop()
        if path in found:
            continue
        source = _source(path)
        found[path] = hashlib.sha256(source).hexdigest()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else \
                    node.func.attr if isinstance(node.func, ast.Attribute) else ""
                if name in {"__import__", "import_module", "exec", "eval"}:
                    _deny("dynamic_dependency")
            if isinstance(node, ast.ImportFrom):
                if node.level or not node.module:
                    _deny("unsupported_dependency")
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            else:
                continue
            for name in names:
                relative = name.replace(".", "/")
                candidates = [p for p in (relative + ".py",
                                           "tests/" + relative + ".py")
                              if (ROOT / p).is_file()]
                if any((ROOT / prefix / relative).is_dir()
                       for prefix in ("", "tests")):
                    _deny("unsupported_package_dependency")
                if candidates:
                    if len(candidates) != 1:
                        _deny("ambiguous_dependency")
                    pending.extend(candidates)
                elif name.split(".")[0] not in sys.stdlib_module_names:
                    # Packages and unknown dependencies need a new review;
                    # never silently omit their __init__ or imported members.
                    _deny("unsupported_dependency")
    return tuple(sorted(found.items()))


def _validate(receipt, reviewed):
    import br02_pg_target as target

    if type(receipt) is not Receipt or type(reviewed) is not ReviewedTarget:
        _deny("missing_evidence")
    for value in (receipt.authorization_id, receipt.owner_approval_ref,
                  receipt.target_review_ref, reviewed.review_ref):
        if type(value) is not str or not value.strip():
            _deny("missing_evidence")
    if receipt.revoked is not False or receipt.operation != OPERATION:
        _deny("operation_or_revocation")
    if type(receipt.max_attempts) is not int or receipt.max_attempts != 1:
        _deny("attempt_limit")
    if (type(receipt.max_seconds) is not int
            or not 1 <= receipt.max_seconds <= MAX_SECONDS):
        _deny("duration_limit")
    if (type(receipt.not_before) is not int or type(receipt.expires_at) is not int
            or not 0 < receipt.expires_at - receipt.not_before <= MAX_APPROVAL_WINDOW):
        _deny("validity_window")
    now = time.time()
    if not math.isfinite(now) or not receipt.not_before <= now < receipt.expires_at:
        _deny("expired_or_not_yet_valid")
    binding = receipt.binding
    if (type(binding) is not Binding or type(reviewed.binding) is not Binding
            or binding != reviewed.binding
            or receipt.target_review_ref != reviewed.review_ref):
        _deny("target_substitution")
    for value in (binding.instance, binding.storage, binding.endpoint,
                  binding.address, binding.database, binding.restricted_login,
                  binding.run_id, binding.server_version):
        if type(value) is not str or not value.strip():
            _deny("target_identity")
    if (binding.address not in ("127.0.0.1", "::1")
            or type(binding.port) is not int or not 1 <= binding.port <= 65535
            or binding.endpoint != (f"127.0.0.1:{binding.port}"
                                    if binding.address == "127.0.0.1"
                                    else f"[::1]:{binding.port}")
            or not binding.server_version.startswith("PostgreSQL 17.")):
        _deny("target_identity")
    if (not _sha(receipt.manifest_digest)
            or receipt.manifest_digest != reviewed.manifest_digest):
        _deny("manifest_substitution")
    manifest = target.supply_approved_manifest(
        json.loads(reviewed.manifest_json), reviewed.manifest_digest)
    bound = manifest.binding
    expected = {"instance": binding.instance, "storage": binding.storage,
                "loopback_endpoint": binding.endpoint, "database": binding.database,
                "run_id": binding.run_id, "phase": OPERATION,
                "role_inventory": [binding.restricted_login],
                "artifact_hash": receipt.artifact_digest}
    if any(bound[k] != v for k, v in expected.items()):
        _deny("manifest_binding")
    inventory = _artifact_inventory()
    if (type(receipt.artifacts) is not tuple or receipt.artifacts != inventory
            or not _sha(receipt.artifact_digest)
            or receipt.artifact_digest != artifact_digest(inventory)):
        _deny("artifact_manifest")
    evidence = hashlib.sha256(json.dumps(
        {"receipt": _receipt_data(receipt), "target": reviewed.manifest_json},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    deadline = time.monotonic() + min(receipt.expires_at - now, receipt.max_seconds)
    return manifest, deadline, evidence


def _receipt_data(receipt):
    from dataclasses import asdict
    return asdict(receipt)


class _BoundConnection:
    def __init__(self, connection, deadline, expires_at):
        self.connection, self.deadline, self.expires_at = connection, deadline, expires_at

    def execute(self, sql, params=None):
        if time.monotonic() >= self.deadline or time.time() >= self.expires_at:
            _deny("deadline")
        # Only the reviewed fixed comparison/probe code can reach this object.
        if not sql.lstrip().lower().startswith("select ") or ";" in sql:
            _deny("non_readonly_statement")
        return self.connection.execute(sql, params) if params is not None \
            else self.connection.execute(sql)


_IDENTITY_PROBE = (
    "select inet_server_addr()::text, inet_server_port(), version(),"
    " current_database(), session_user, current_user,"
    " current_setting('transaction_read_only'),"
    " current_setting('statement_timeout'),"
    " exists (select 1 from pg_catalog.pg_roles r"
    " where pg_has_role(session_user, r.oid, 'MEMBER')"
    " and (r.rolsuper or r.rolbypassrls or r.rolcreatedb"
    " or r.rolcreaterole or r.rolreplication)),"
    " current_setting('search_path')"
)


def execute_stage2(approval_reference):
    """Only public dispatch: reference locates evidence, never approves it.

    The fixed authority is unavailable. No caller can provide an authority,
    connection, approval object, artifact list, clock or permit to this API.
    """
    connection = None
    try:
        authority = _trusted_authority()  # fails BEFORE evidence or file I/O
        if type(approval_reference) is not str or not approval_reference.strip():
            _deny("missing_reference")
        receipt = authority.lookup(approval_reference)
        reviewed = authority.reviewed_target(receipt.target_review_ref)
        manifest, deadline, evidence = _validate(receipt, reviewed)
        # Atomic external consume is the final authority, not local validation.
        if authority.consume(receipt, evidence) is not True:
            _deny("consumption_refused")
        # Recheck artifacts, validity and revocation in the authority at consume;
        # also fail closed on local source change before opening a connection.
        if (_artifact_inventory() != receipt.artifacts or time.monotonic() >= deadline
                or time.time() >= receipt.expires_at):
            _deny("post_consumption_change")
        permit = Permit(receipt.authorization_id, evidence, receipt.binding,
                        OPERATION, deadline, receipt.max_seconds, receipt.expires_at)
        connection = authority.open_connection(permit)
        bound = _BoundConnection(connection, deadline, receipt.expires_at)
        row = bound.execute(_IDENTITY_PROBE).fetchone()
        b = receipt.binding
        if (tuple(row[:6]) != (b.address, b.port, b.server_version, b.database,
                              b.restricted_login, b.restricted_login)
                or row[6] != "on" or row[8] is not False
                or row[9] != "pg_catalog, public"):
            _deny("connected_identity_or_privilege")
        # PostgreSQL renders this setting with units; allow only exact bounded
        # millisecond/second forms, never a zero/unlimited or ambiguous value.
        limit = row[7]
        if type(limit) is not str:
            _deny("statement_timeout")
        if limit.endswith("ms") and limit[:-2].isdigit():
            milliseconds = int(limit[:-2])
        elif limit.endswith("s") and limit[:-1].isdigit():
            milliseconds = int(limit[:-1]) * 1000
        else:
            _deny("statement_timeout")
        if not 0 < milliseconds <= receipt.max_seconds * 1000:
            _deny("statement_timeout")
        import br02_pg_target as target
        from types import SimpleNamespace
        approval = SimpleNamespace(
            approved_manifest=manifest, attested_server_address=b.address,
            attested_server_port=b.port, attested_server_version_prefix=b.server_version,
            runtime_login=b.restricted_login, run_marker=b.run_id)
        token = _dispatch.set((bound, approval, manifest, deadline))
        try:
            report = target.stage2_run_catalog_comparison(bound, approval, manifest)
        finally:
            _dispatch.reset(token)
        if time.monotonic() >= deadline or time.time() >= receipt.expires_at:
            _deny("deadline")
        return report
    except AuthorizationDenied as error:
        # Re-sanitize even a mutated/subclassed exception from a future host.
        raise AuthorizationDenied(str(error)) from None
    except Exception:
        raise AuthorizationDenied("evidence_or_dispatch_failure") from None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                raise AuthorizationDenied("connection_cleanup_failure") from None
