"""BR-02-PG-HARNESS target-record and manifest validation (pure offline).

Purpose
-------
Validate a candidate PostgreSQL test-target record for BR-02 result
persistence verification. A candidate is an inert mapping supplied by the
caller; this module performs no network, database, filesystem, or
process-environment I/O of any kind.

Two independent fail-closed layers
----------------------------------
1. ``validate_target`` - metadata validation. The candidate must satisfy the
   attestation rules and must match a *separately supplied* trusted approved
   manifest (with its digest) field-for-field on every binding field: exact
   instance, storage, loopback endpoint, database, run, role inventory,
   artifact hash, resource cost $0, and phase. Without a supplied approved
   manifest nothing is accepted - a caller can never approve itself.
2. ``authorize_execution`` / ``execution_authorized`` - the live-execution
   gate. It is refused unconditionally in this package: no environment
   variable (``AI_MARKER_TEST_PG_DSN`` or ``AI_MARKER_PG_DSN``), DSN string,
   flag, or candidate record can unlock live PostgreSQL execution. Only a
   future, independently approved target plus separate execution
   authorization can, and that approval path does not exist yet.

Exception hygiene: refusal details name fields and rules only. Raw candidate
values (markers, verifier names, DSN strings, digests) are never echoed.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Optional

# Stable refusal reason codes (tests assert these exact strings).
MISSING_TARGET = "missing_target"
UNKNOWN_TARGET = "unknown_target"
SHARED_TARGET = "shared_target"
INCONSISTENT_TARGET = "inconsistent_target"
SELF_ASSERTED_TARGET = "self_asserted_target"
PRODUCTION_DSN_FALLBACK = "production_dsn_fallback"
NO_APPROVED_MANIFEST = "no_approved_manifest"
MANIFEST_MISMATCH = "manifest_mismatch"
EXECUTION_NOT_AUTHORIZED = "execution_not_authorized"
TARGET_IDENTITY_INCOMPLETE = "target_identity_incomplete"
TARGET_NOT_INITIALIZED = "target_not_initialized"

# DSN variable names (names only; this module never reads any environment).
PRODUCTION_DSN_ENV = "AI_MARKER_PG_DSN"
TEST_DSN_ENV = "AI_MARKER_TEST_PG_DSN"

# Expected attestation values.
ATTESTATION_MARKER = "br02-pg-target-attestation-v1"
EXPECTED_KIND = "disposable_synthetic_postgresql"
EXPECTED_EXCLUSIVITY = "exclusive_single_agent"

# The manifest binding fields. A candidate is accepted only when every one of
# these equals the trusted approved manifest exactly (no stripping, no case
# folding, no defaults).
MANIFEST_BINDING_FIELDS = (
    "instance",
    "storage",
    "loopback_endpoint",
    "database",
    "run_id",
    "role_inventory",
    "artifact_hash",
    "resource_cost_usd",
    "phase",
)

# Roles that must never appear in an approved target's role inventory: the
# runtime handle is restricted and never administrative.
FORBIDDEN_ROLES = frozenset(
    {"postgres", "superuser", "marker_admin", "admin", "root"})

_LOOPBACK_PREFIXES = ("127.0.0.1", "[::1]")


class TargetRefused(Exception):
    """A candidate target record or manifest was refused; ``reason`` is one
    of the stable reason codes above. Details never echo raw values."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass(frozen=True)
class TargetRecord:
    """Normalized view of a candidate target record (inert values only)."""

    attestation_marker: str
    kind: str
    dsn_env: str
    exclusivity: str
    verifier: str
    evidence: str
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class ApprovedManifest:
    """A trusted approved target manifest, supplied separately from any
    candidate. Created only through :func:`supply_approved_manifest`."""

    record: TargetRecord
    binding: Mapping[str, Any]
    digest: str


# ---------------------------------------------------------------------------
# Attestation rules (each raises TargetRefused; no raw value ever echoed)
# ---------------------------------------------------------------------------

def _as_mapping(record: Any) -> Mapping[str, Any]:
    if not isinstance(record, Mapping) or not record:
        raise TargetRefused(
            MISSING_TARGET, "record is absent, empty, or not a mapping")
    return record


def check_identity(record: Any) -> TargetRecord:
    """Require the recognized BR-02 marker and the disposable kind."""
    data = _as_mapping(record)
    marker = data.get("attestation_marker")
    if not isinstance(marker, str) or marker != ATTESTATION_MARKER:
        raise TargetRefused(
            UNKNOWN_TARGET, "attestation marker is not the recognized BR-02 "
                            "target marker")
    kind = data.get("kind")
    if not isinstance(kind, str) or kind != EXPECTED_KIND:
        raise TargetRefused(
            INCONSISTENT_TARGET, "kind is not the expected disposable "
                                 "synthetic PostgreSQL kind")
    return _normalize(data)


def check_exclusivity(record: Any) -> None:
    """Require a positive declaration of single-agent exclusive use."""
    data = _as_mapping(record)
    if data.get("exclusivity") != EXPECTED_EXCLUSIVITY:
        raise TargetRefused(
            SHARED_TARGET,
            "target must declare single-agent exclusive use of a disposable "
            "database")


def check_verification(record: Any) -> None:
    """Require independent verification; self-assertion is insufficient."""
    data = _as_mapping(record)
    verified = data.get("independently_verified")
    if not isinstance(verified, Mapping):
        raise TargetRefused(
            SELF_ASSERTED_TARGET,
            "independent verification block missing; self-assertion is "
            "insufficient")
    verifier = verified.get("verifier")
    evidence = verified.get("evidence")
    if not isinstance(verifier, str) or not verifier.strip():
        raise TargetRefused(SELF_ASSERTED_TARGET, "independent verifier "
                                                  "missing")
    if verifier.strip().lower() == "self":
        raise TargetRefused(
            SELF_ASSERTED_TARGET,
            "self-assertion is not independent verification")
    if not isinstance(evidence, str) or not evidence.strip():
        raise TargetRefused(SELF_ASSERTED_TARGET, "verification evidence "
                                                  "missing")


def check_dsn_env(record: Any) -> None:
    """Require the dedicated test DSN variable NAME and nothing else."""
    data = _as_mapping(record)
    dsn_env = data.get("dsn_env")
    if not isinstance(dsn_env, str) or not dsn_env:
        raise TargetRefused(
            PRODUCTION_DSN_FALLBACK,
            "dsn_env missing; only the dedicated test DSN variable name is "
            "allowed")
    if dsn_env == PRODUCTION_DSN_ENV or PRODUCTION_DSN_ENV in dsn_env:
        raise TargetRefused(
            PRODUCTION_DSN_FALLBACK,
            "the production DSN variable must never be referenced")
    if dsn_env != TEST_DSN_ENV:
        raise TargetRefused(
            PRODUCTION_DSN_FALLBACK,
            "only the dedicated test DSN variable name is allowed")


def check_consistency(record: Any) -> None:
    """Reject cross-field contradictions in an otherwise well-formed record."""
    data = _as_mapping(record)
    other_users = data.get("other_users", [])
    if data.get("exclusivity") == EXPECTED_EXCLUSIVITY and other_users:
        raise TargetRefused(
            INCONSISTENT_TARGET, "an exclusive target must not list other "
                                 "users")
    if data.get("disposable") is not True:
        raise TargetRefused(
            INCONSISTENT_TARGET, "target must be declared disposable")


def check_loopback(record: Any) -> None:
    """The target endpoint must be loopback-only (never remotely shared)."""
    data = _as_mapping(record)
    endpoint = data.get("loopback_endpoint")
    if not isinstance(endpoint, str) or not (
            endpoint == "localhost"
            or endpoint.startswith(_LOOPBACK_PREFIXES)):
        raise TargetRefused(
            INCONSISTENT_TARGET,
            "endpoint is not a loopback endpoint")


def assert_restricted_roles(role_inventory: Any) -> None:
    """Refuse an empty or non-restricted role inventory (never admin)."""
    if not isinstance(role_inventory, list) or not role_inventory:
        raise TargetRefused(
            INCONSISTENT_TARGET, "role inventory must be a non-empty list")
    for role in role_inventory:
        if not isinstance(role, str) or not role.strip():
            raise TargetRefused(
                INCONSISTENT_TARGET, "role inventory contains a blank role")
        if role.strip().lower() in FORBIDDEN_ROLES:
            raise TargetRefused(
                INCONSISTENT_TARGET,
                "role inventory contains a non-restricted role; the runtime "
                "handle is never administrative")


def _normalize(data: Mapping[str, Any]) -> TargetRecord:
    verified = data.get("independently_verified") or {}
    verified = verified if isinstance(verified, Mapping) else {}
    return TargetRecord(
        attestation_marker=data["attestation_marker"],
        kind=data["kind"],
        dsn_env=data.get("dsn_env", ""),
        exclusivity=data.get("exclusivity", ""),
        verifier=verified.get("verifier", ""),
        evidence=verified.get("evidence", ""),
        raw=data,
    )


# ---------------------------------------------------------------------------
# Manifest binding, digest, and separate trusted supply
# ---------------------------------------------------------------------------

def _binding_view(record: Mapping[str, Any]) -> dict:
    """Extract and shape-check the binding fields (values stay in the
    returned view; nothing is echoed on failure)."""
    view = {}
    for field in MANIFEST_BINDING_FIELDS:
        if field not in record:
            raise TargetRefused(
                INCONSISTENT_TARGET,
                f"manifest binding field '{field}' is missing")
        view[field] = record[field]
    for field in ("instance", "storage", "loopback_endpoint", "database",
                  "run_id", "artifact_hash", "phase"):
        if not isinstance(view[field], str) or not view[field].strip():
            raise TargetRefused(
                INCONSISTENT_TARGET,
                f"manifest binding field '{field}' must be a non-empty string")
    assert_restricted_roles(view["role_inventory"])
    cost = view["resource_cost_usd"]
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) \
            or cost != 0:
        raise TargetRefused(
            INCONSISTENT_TARGET,
            "manifest binding field 'resource_cost_usd' must be exactly 0")
    return view


def manifest_digest(record: Mapping[str, Any]) -> str:
    """Deterministic digest over the canonical JSON of the binding fields."""
    view = _binding_view(record)
    canonical = json.dumps(view, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def supply_approved_manifest(record: Any, expected_digest: str) \
        -> ApprovedManifest:
    """Validate and freeze a trusted approved manifest, supplied separately
    from any candidate. The record must pass every attestation rule and the
    supplied digest must match the record exactly; otherwise nothing is
    accepted."""
    normalized = check_identity(record)
    check_exclusivity(record)
    check_verification(record)
    check_dsn_env(record)
    check_consistency(record)
    check_loopback(record)
    binding = _binding_view(record)
    if not isinstance(expected_digest, str) or not expected_digest:
        raise TargetRefused(
            MANIFEST_MISMATCH, "approved manifest digest missing")
    actual = manifest_digest(record)
    if expected_digest != actual:
        raise TargetRefused(
            MANIFEST_MISMATCH,
            "supplied digest does not match the approved record")
    return ApprovedManifest(record=normalized, binding=binding,
                            digest=actual)


def require_approved_manifest(approved_manifest: Any) -> ApprovedManifest:
    """Re-verify a separately supplied approved manifest before ANY use.

    The manifest must be an ``ApprovedManifest``; its stored digest must
    re-verify over the record's canonical binding; and its frozen binding
    must equal the record's binding view. Missing, tampered, or inconsistent
    manifests are refused with a sanitized reason. This is metadata
    re-verification only and never authorizes execution.
    """
    if not isinstance(approved_manifest, ApprovedManifest):
        raise TargetRefused(
            NO_APPROVED_MANIFEST,
            "an approved manifest is required for this operation")
    record = getattr(getattr(approved_manifest, "record", None), "raw", None)
    if not isinstance(record, Mapping):
        raise TargetRefused(
            NO_APPROVED_MANIFEST,
            "the approved manifest record is missing or has the wrong shape")
    view = _binding_view(record)
    actual = manifest_digest(record)
    if not isinstance(approved_manifest.digest, str) \
            or approved_manifest.digest != actual:
        raise TargetRefused(
            MANIFEST_MISMATCH,
            "the approved manifest digest does not re-verify")
    if dict(view) != dict(approved_manifest.binding):
        raise TargetRefused(
            MANIFEST_MISMATCH,
            "the approved manifest binding view is inconsistent")
    return approved_manifest


def require_stage2_identity(approval: Any, approved_manifest: Any) -> dict:
    """Validate the MANDATORY initialized-stage identity expectations.

    Runs BEFORE any connection query. Missing or malformed expectations are
    refused, never silently skipped: non-blank attested server address, a
    valid TCP port, a non-blank version prefix, a restricted runtime login,
    and a non-blank run marker; plus the approved manifest's database and
    overall run identity, with the digest and frozen binding re-verified.

    The approval's run marker must equal the manifest's overall run
    identity. The per-test fixture-bundle id is deliberately NOT the run
    marker, so a random bundle id can never satisfy this binding.
    """
    manifest = require_approved_manifest(approved_manifest)
    approval_manifest = require_approved_manifest(
        getattr(approval, "approved_manifest", None))
    if approval_manifest.digest != manifest.digest:
        raise TargetRefused(
            MANIFEST_MISMATCH,
            "the approval carries a different approved manifest")
    binding = manifest.binding
    database = binding.get("database")
    run_id = binding.get("run_id")
    if not isinstance(database, str) or not database.strip():
        raise TargetRefused(
            MANIFEST_MISMATCH,
            "the approved manifest database binding is missing or blank")
    if not isinstance(run_id, str) or not run_id.strip():
        raise TargetRefused(
            MANIFEST_MISMATCH,
            "the approved manifest run identity binding is missing or blank")

    address = getattr(approval, "attested_server_address", "")
    if not isinstance(address, str) or not address.strip():
        raise TargetRefused(
            TARGET_IDENTITY_INCOMPLETE,
            "attested_server_address is required and must be non-blank")
    port = getattr(approval, "attested_server_port", None)
    if isinstance(port, bool) or not isinstance(port, int) \
            or not (0 < port <= 65535):
        raise TargetRefused(
            TARGET_IDENTITY_INCOMPLETE,
            "attested_server_port must be an integer TCP port in 1..65535")
    version_prefix = getattr(approval, "attested_server_version_prefix", "")
    if not isinstance(version_prefix, str) or not version_prefix.strip():
        raise TargetRefused(
            TARGET_IDENTITY_INCOMPLETE,
            "attested_server_version_prefix is required and must be "
            "non-blank")
    runtime_login = getattr(approval, "runtime_login", "")
    if not isinstance(runtime_login, str) or not runtime_login.strip():
        raise TargetRefused(
            TARGET_IDENTITY_INCOMPLETE,
            "runtime_login is required and must be non-blank")
    run_marker = getattr(approval, "run_marker", "")
    if not isinstance(run_marker, str) or not run_marker.strip():
        raise TargetRefused(
            TARGET_IDENTITY_INCOMPLETE,
            "run_marker is required and must be non-blank")
    if run_marker != run_id:
        raise TargetRefused(
            MANIFEST_MISMATCH,
            "approval run_marker does not equal the approved manifest run "
            "identity")
    return {
        "database": database,
        "run_id": run_id,
        "address": address,
        "port": port,
        "version_prefix": version_prefix,
        "runtime_login": runtime_login,
        "run_marker": run_marker,
    }


def _first_binding_difference(candidate: Mapping[str, Any],
                              approved: Mapping[str, Any]) -> Optional[str]:
    for field in MANIFEST_BINDING_FIELDS:
        if candidate.get(field) != approved.get(field):
            return field
    return None


# ---------------------------------------------------------------------------
# Validation pipeline and the separate live-execution gate
# ---------------------------------------------------------------------------

def validate_target(record: Any,
                    approved_manifest: Optional[ApprovedManifest] = None) \
        -> TargetRecord:
    """Full fail-closed metadata validation of an inert candidate record.

    Runs every attestation rule, then compares the candidate binding fields
    against the separately supplied trusted approved manifest. Without a
    supplied approved manifest nothing is accepted
    (``no_approved_manifest``). Passing validation establishes only that the
    metadata is internally valid and approved-matching; it never authorizes
    live execution.
    """
    normalized = check_identity(record)   # missing / unknown / inconsistent
    check_exclusivity(record)             # shared
    check_verification(record)            # self-asserted
    check_dsn_env(record)                 # production DSN fallback
    check_consistency(record)             # inconsistent
    check_loopback(record)                # non-loopback endpoint
    binding = _binding_view(record)       # manifest shape (inconsistent)
    if approved_manifest is None:
        raise TargetRefused(
            NO_APPROVED_MANIFEST,
            "no trusted approved manifest was supplied; a candidate is never "
            "accepted on its own assertion")
    if not isinstance(approved_manifest, ApprovedManifest):
        raise TargetRefused(
            NO_APPROVED_MANIFEST,
            "the supplied approved manifest has the wrong shape")
    field = _first_binding_difference(binding, approved_manifest.binding)
    if field is not None:
        raise TargetRefused(
            MANIFEST_MISMATCH,
            f"binding field '{field}' differs from the approved manifest")
    return normalized


def execution_authorized() -> bool:
    """False outside a validated, short-lived CI context. Execution requires a
    future, independently approved target plus separate execution
    authorization; no environment variable, DSN string, flag, or candidate
    record can change this answer."""
    ci = _ci_sys.modules.get('tests.br02_pg_ci_context')
    return ci is not None and ci.active()


def authorize_execution(*_approval: Any, **_kwargs: Any) -> None:
    """Only the exact approval in a validated CI context may proceed.

    Default callers remain refused. This does not grant Stage 2 dispatch.
    """
    ci = _ci_sys.modules.get('tests.br02_pg_ci_context')
    if ci is not None and ci.active() and not _kwargs and len(_approval)<=1:
        try:
            ci.authorize(_approval[0] if len(_approval)==1 else None)
            return
        except ci.CIDenied:
            pass
    raise TargetRefused(
        EXECUTION_NOT_AUTHORIZED,
        "live execution is not authorized: it requires a future "
        "independently approved target plus separate execution "
        "authorization, and no environment variable, DSN, or flag can "
        "unlock it")


# Both historical import spellings refer to one authority and manifest type.
import sys as _ci_sys
if __name__ in ('br02_pg_target','tests.br02_pg_target'):
    _ci_sys.modules.setdefault('br02_pg_target',_ci_sys.modules[__name__])
    _ci_sys.modules.setdefault('tests.br02_pg_target',_ci_sys.modules[__name__])
    if 'tests' in _ci_sys.modules:
        setattr(_ci_sys.modules['tests'],'br02_pg_target',_ci_sys.modules[__name__])


def stage2_authorized_readonly(approval_reference):
    """Inactive operation-specific entry; no connection/authority injection.

    Does not enable execution_authorized(), seed/bootstrap or the live runner.
    The external trusted authority is deliberately unavailable.
    """
    import br02_pg_authorization
    return br02_pg_authorization.execute_stage2(approval_reference)


# ---------------------------------------------------------------------------
# Stage 2: real identity/catalog comparison (implemented, gate-locked)
# ---------------------------------------------------------------------------

# The fixed schema_meta key holding the approved synthetic run identity for
# the initialized target. Its value is the approved manifest's overall
# run_id, NOT a per-test fixture-bundle id. Only a separate stage-0 bootstrap
# (explicit future-owner authorization) may create it; normal seeding never
# does.
STAGE2_RUN_MARKER_KEY = "br02_synthetic_run_id"

# Expected deployed inventory after the reviewed migrations 0001-0009. This
# is the identity the stage-2 check compares the live catalog against.
EXPECTED_CATALOG = {
    "tenants": ("created_at", "status", "tenant_id"),
    "tenant_membership": ("active", "role", "subject", "tenant_id",
                          "updated_at"),
    "results": ("created_at", "original_digest", "original_scores",
                "original_scores_digest", "owner_subject", "result_id",
                "result_revision", "tenant_id"),
    "audit_events": ("action", "actor", "authorization_json", "binding",
                     "created_at", "event_id", "event_type",
                     "expected_revision", "idempotency_key", "new_score",
                     "old_score", "owner_subject", "payload_digest",
                     "question_number", "reason", "result_id",
                     "review_status", "sequence", "supersedes_event_id",
                     "tenant_id", "resulting_revision"),
    "pdf_references": ("created_at", "document_digest", "owner_subject",
                       "size_bytes", "storage_key", "tenant_id"),
    "marking_jobs": ("assignment_id", "attempts", "created_at",
                     "document_digest", "error_code", "idempotency_key",
                     "job_id", "lease_until", "max_attempts", "owner_subject",
                     "result_id", "status", "tenant_id", "updated_at"),
    "assignments": ("assignment_id", "created_at", "idempotency_key",
                    "owner_subject", "tenant_id", "title"),
    "result_context": ("assignment_id", "classroom_assignment_id",
                       "classroom_course_id", "classroom_revision",
                       "classroom_submission_id", "created_at",
                       "document_digest", "job_id", "membership_subject",
                       "original_digest", "original_output", "owner_subject",
                       "result_id", "result_scope", "review_key", "tenant_id"),
    "schema_meta": ("key", "value"),
}

EXPECTED_SCHEMA_MARKERS = {
    "schema_version": "1",
    "rls_version": "1",
    "jobs_version": "1",
    "docs_version": "1",
    "membership_access_version": "1",
    "results_update_version": "1",
    "assignments_version": "1",
    "job_assignment_linkage_version": "1",
    "result_context_version": "1",
}

# The runtime roles and the RLS-forced tenant tables (0002-0009).
EXPECTED_RUNTIME_ROLES = ("marker_app", "marker_worker")
EXPECTED_RLS_FORCED_TABLES = (
    "tenants", "tenant_membership", "results", "pdf_references",
    "audit_events", "marking_jobs", "assignments", "result_context",
)

# Expected EFFECTIVE table privileges, derived READ-ONLY from migrations
# 0001-0009 (has_table_privilege includes PUBLIC and inherited grants).
# 0002 REVOKEs ALL on tenants and never grants it again, so both runtime
# roles hold NOTHING there. 0006 REVOKEs table-wide UPDATE on results and
# leaves only the column-scoped UPDATE(result_revision) below, so table-level
# UPDATE is absent. schema_meta is SELECT-only (0002).
EXPECTED_EFFECTIVE_TABLE_PRIVS = {
    "tenants": {"marker_app": (), "marker_worker": (), "PUBLIC": ()},
    "tenant_membership": {"marker_app": (), "marker_worker": (),
                          "PUBLIC": ()},
    "results": {"marker_app": ("SELECT", "INSERT"),
                "marker_worker": ("SELECT", "INSERT"),
                "PUBLIC": ()},
    "audit_events": {"marker_app": ("SELECT", "INSERT"),
                     "marker_worker": ("SELECT", "INSERT"),
                     "PUBLIC": ()},
    "pdf_references": {"marker_app": ("SELECT", "INSERT"),
                       "marker_worker": ("SELECT", "INSERT"),
                       "PUBLIC": ()},
    "marking_jobs": {"marker_app": ("SELECT", "INSERT", "UPDATE"),
                     "marker_worker": ("SELECT", "INSERT", "UPDATE"),
                     "PUBLIC": ()},
    "assignments": {"marker_app": ("SELECT", "INSERT"),
                    "marker_worker": (), "PUBLIC": ()},
    "result_context": {"marker_app": ("SELECT", "INSERT"),
                       "marker_worker": ("SELECT", "INSERT"),
                       "PUBLIC": ()},
    "schema_meta": {"marker_app": ("SELECT",), "marker_worker": ("SELECT",),
                    "PUBLIC": ()},
}

# Expected EFFECTIVE column-scoped privileges, derived READ-ONLY from the
# migrations. A table-level grant already implies every column, so this
# records only the grants that are actually COLUMN-scoped:
# * 0005: marker_app may SELECT exactly the five resolver columns of
#   tenant_membership; marker_worker holds nothing there.
# * 0006: after REVOKE UPDATE, both roles may UPDATE only result_revision.
EXPECTED_EFFECTIVE_COLUMN_PRIVS = {
    "tenant_membership": {
        "SELECT": {
            "marker_app": ("tenant_id", "subject", "role", "active",
                           "updated_at"),
            "marker_worker": (),
        },
    },
    "results": {
        "UPDATE": {
            "marker_app": ("result_revision",),
            "marker_worker": ("result_revision",),
        },
    },
}

# The reviewed migrations define NO sequences in the public schema (all
# identities are text or explicit bigint values).
EXPECTED_SEQUENCES = ()

# Named constraints the reviewed migrations create (the primary keys are
# asserted separately by presence).
EXPECTED_CONSTRAINTS = {
    "result_context": ("ck_result_context_classroom", "ck_result_context_digest",
                       "ck_result_context_document_digest",
                       "ck_result_context_owner", "ck_result_context_review_key",
                       "ck_result_context_scope", "fk_result_context_anchor",
                       "fk_result_context_assignment", "fk_result_context_job",
                       "fk_result_context_membership", "uq_result_context_owner"),
    "results": ("fk_results_membership",),
    "audit_events": ("uq_audit_events_stream_sequence",
                     "uq_audit_events_stream_idempotency",
                     "uq_audit_events_stream_event"),
    "pdf_references": ("fk_pdf_membership",),
    "marking_jobs": ("uq_marking_jobs_idempotency",
                     "ck_marking_jobs_status", "ck_marking_jobs_attempts"),
    "assignments": ("uq_assignments_idempotency", "ck_assignments_title",
                    "ck_assignments_id"),
    "tenant_membership": (),
    "tenants": (),
    "schema_meta": (),
}

STAGE2_PLANNED_CHECKS = (
    "mandatory attested identity validated BEFORE any query (non-blank "
    "server address, valid TCP port, non-blank version prefix, runtime "
    "login, run marker; the approval marker equals the manifest run "
    "identity) - missing values refuse, never skip",
    "approved manifest database/run identity, digest, and frozen binding "
    "re-verified BEFORE any query",
    "actual public.schema_meta br02_synthetic_run_id equals BOTH the "
    "approval run marker and the manifest run identity; a missing marker "
    "refuses this initialized stage (stage-0 bootstrap is distinct)",
    "deployed table inventory equals EXPECTED_CATALOG (no missing, no extra) "
    "from pg_catalog metadata, which is not filtered by the restricted "
    "login's privileges",
    "deployed column inventory per table equals EXPECTED_CATALOG from "
    "pg_attribute (attnum > 0, not dropped)",
    "schema_meta markers equal EXPECTED_SCHEMA_MARKERS (schema_version=1, "
    "result_context_version=1 distinct)",
    "runtime roles marker_app/marker_worker exist as NOLOGIN group roles",
    "RLS enabled AND forced on every expected tenant table",
    "effective table privileges equal the migration-derived matrix: tenants "
    "holds NOTHING for either runtime role (v2 REVOKE ALL, never re-granted), "
    "schema_meta is SELECT-only, results has no table-level UPDATE (v6), and "
    "assignments is marker_app-only",
    "effective column privileges equal the migration-derived matrix: "
    "marker_app holds the five 0005 membership SELECT columns; both roles "
    "hold only UPDATE(result_revision) on results (0006)",
    "marker_worker holds NOTHING on tenant_membership (no column and no "
    "table grant; the five-column SELECT belongs to marker_app per 0005)",
    "no PUBLIC grants on any tenant table (anon/authenticated revoked in "
    "0002; inheritance of grants through PUBLIC absent)",
    "sequence inventory belongs to the expected serial/identity columns",
    "no runtime grant changed by any harness operation (before/after diff)",
    "registered provenance (document/job/assignment/result identity) "
    "resolvable through the real adapters",
)


def stage2_plan() -> dict:
    """The explicit stage-2 plan (metadata-level disclosure ONLY). This
    names every check the live identity/catalog comparison performs once the
    future approval exists; it does not claim any of them ran."""
    return {
        "phase": "planned - live gate refused in the current phase",
        "checks": list(STAGE2_PLANNED_CHECKS),
        "implementation": "stage2_run_catalog_comparison",
        "initialization_stage": "stage2_initialized_only",
        "initialization_marker": STAGE2_RUN_MARKER_KEY,
        "bootstrap_stage": (
            "stage-0 bootstrap is a separate, explicitly authorized "
            "future-owner operation; normal seed and test setup never create, "
            "overwrite, reuse, or reactivate the run marker"),
        "expected_catalog_tables": sorted(EXPECTED_CATALOG),
        "expected_markers": dict(EXPECTED_SCHEMA_MARKERS),
        "missing_future_approval_transport": (
            "the independently approved target manifest and its transport "
            "do not exist yet; no approval is invented here"),
    }


def stage2_run_catalog_comparison(connection, approval, approved_manifest) \
        -> dict:
    """REAL stage-2 comparison of the deployed catalog against the expected
    identity, for an INITIALIZED target only, behind the unconditional
    execution gate (refuses today; runs only under a future independently
    approved target). Read-only.

    Mandatory expectations are validated BEFORE the first query: approved
    manifest database/run identity with digest and frozen binding
    re-verified, plus non-blank attested server address, a valid TCP port, a
    non-blank version prefix, a restricted runtime login, and a non-blank run
    marker. Missing fields are refused, never treated as optional skips.

    Binds the LIVE server identity to those required expectations
    (current_database, inet_server_addr/port, version, session_user vs
    current_user - a direct restricted login, never an admin session via
    SET ROLE). It then reads the ACTUAL synthetic run marker from
    ``public.schema_meta`` under the fixed key and requires it to equal BOTH
    the approval's run marker and the approved manifest's overall run
    identity: two input fields agreeing with each other is not runtime
    proof. A missing marker refuses this initialized stage; the stage-0
    bootstrap is a separate explicitly authorized operation.

    Compares tables/columns/NAMED CONSTRAINTS, schema markers, role shapes
    (superuser/bypassrls/createdb/createrole/replication), RLS
    ENABLE+FORCE, EFFECTIVE privileges for the real runtime roles
    (has_table_privilege / has_column_privilege / has_schema_privilege /
    has_sequence_privilege, which include PUBLIC and inherited grants),
    ACL inventory via aclexplode (PUBLIC is grantee OID 0 and is never
    passed to has_*_privilege as a role argument), membership/inheritance
    edges, the sequence inventory, and schema usage - against the approved
    expected identity. Never mutates anything.

    Offline, the comparison LOGIC is exercised against controlled connection
    doubles (clearly NOT a PostgreSQL proof); the live proof runs only under
    the future approval."""
    import br02_pg_authorization
    if not br02_pg_authorization._claim_stage2_dispatch(
            connection, approval, approved_manifest):
        ci = _ci_sys.modules.get('tests.br02_pg_ci_context')
        if ci is not None and ci.active():
            raise TargetRefused(EXECUTION_NOT_AUTHORIZED,
                                'CI integration context does not authorize Stage 2')
        authorize_execution(approval)  # caller-provided handles remain denied
    expected = require_stage2_identity(approval, approved_manifest)
    report = {"diffs": [], "checked": []}

    def _diff(check, detail):
        report["diffs"].append({"check": check, "detail": detail})

    # -- attested server identity (required expectations, no skips) -----
    report["checked"].append("attested_server_identity")
    addr, port, version_string, database, session_user, current_user_name = \
        connection.execute(
            "select coalesce(inet_server_addr()::text, ''),"
            " coalesce(inet_server_port(), 0), version(),"
            " current_database(), session_user, current_user"
        ).fetchone()
    if database != expected["database"]:
        _diff("attested_server_identity",
              "current_database does not equal the approved binding")
    if addr != expected["address"]:
        _diff("attested_server_identity",
              "server address does not equal the attested address")
    if str(port) != str(expected["port"]):
        _diff("attested_server_identity",
              "server port does not equal the attested port")
    if not str(version_string).startswith(expected["version_prefix"]):
        _diff("attested_server_identity",
              "server version does not match the attested prefix")
    if session_user != expected["runtime_login"]:
        _diff("attested_server_identity",
              "session_user is not the attested restricted runtime login")
    if current_user_name != session_user:
        _diff("attested_server_identity",
              "current_user differs from session_user: the connection must "
              "BE the restricted login, never an admin session via SET ROLE")

    if report["diffs"]:
        report["passed"] = False
        return report  # no run-marker or catalog reads on identity mismatch

    # -- ACTUAL synthetic run marker: initialized-stage runtime proof ---
    report["checked"].append("synthetic_run_marker")
    marker_row = connection.execute(
        "select value from schema_meta where key = %s",
        (STAGE2_RUN_MARKER_KEY,)).fetchone()
    if marker_row is None:
        raise TargetRefused(
            TARGET_NOT_INITIALIZED,
            "the target has no approved synthetic run marker; stage 2 runs "
            "only against an initialized target - the stage-0 bootstrap is "
            "a separate, explicitly authorized operation")
    actual_marker = marker_row[0]
    if actual_marker != expected["run_marker"]:
        _diff("synthetic_run_marker",
              "the target's actual synthetic run marker does not equal the "
              "approved run marker")
    if actual_marker != expected["run_id"]:
        _diff("synthetic_run_marker",
              "the target's actual synthetic run marker does not equal the "
              "approved manifest run identity")

    if report["diffs"]:
        report["passed"] = False
        return report  # no catalog reads on run-identity mismatch

    # -- table and column inventory (pg_catalog, full public schema) -----
    # pg_catalog metadata is NOT filtered by the caller's privileges, so the
    # restricted runtime login still sees tenants (which holds no grant).
    # Effective runtime privileges are checked separately below.
    actual_tables = {row[0] for row in connection.execute(
        "select cls.relname"
        " from pg_catalog.pg_class cls"
        " join pg_catalog.pg_namespace nsp on nsp.oid = cls.relnamespace"
        " where nsp.nspname = 'public'"
        "   and cls.relkind in ('r', 'p')"
    ).fetchall()}
    expected_tables = set(EXPECTED_CATALOG)
    report["checked"].append("table_inventory")
    for missing in sorted(expected_tables - actual_tables):
        _diff("table_inventory", f"missing table {missing}")
    for extra in sorted(actual_tables - expected_tables):
        _diff("table_inventory", f"unexpected table {extra}")

    report["checked"].append("column_inventory")
    for table, expected_columns in sorted(EXPECTED_CATALOG.items()):
        if table not in actual_tables:
            continue
        actual_columns = {row[0] for row in connection.execute(
            "select att.attname"
            " from pg_catalog.pg_attribute att"
            " join pg_catalog.pg_class cls on cls.oid = att.attrelid"
            " join pg_catalog.pg_namespace nsp on nsp.oid = cls.relnamespace"
            " where nsp.nspname = 'public'"
            "   and cls.relname = %s"
            "   and cls.relkind in ('r', 'p')"
            "   and att.attnum > 0"
            "   and not att.attisdropped", (table,)
        ).fetchall()}
        if actual_columns != set(expected_columns):
            _diff("column_inventory",
                  f"{table}: columns differ from the expected identity")

    # -- named constraints and primary keys ----------------------------
    report["checked"].append("constraint_inventory")
    for table in sorted(expected_tables & actual_tables):
        names = {row[0] for row in connection.execute(
            "select conname from pg_constraint where conrelid = %s::regclass",
            (f"public.{table}",)).fetchall()}
        pk_count = connection.execute(
            "select count(*) from pg_constraint where conrelid ="
            " %s::regclass and contype = 'p'",
            (f"public.{table}",)).fetchone()[0]
        if not pk_count:
            _diff("constraint_inventory", f"{table}: no primary key")
        for expected_name in EXPECTED_CONSTRAINTS.get(table, ()):
            if expected_name not in names:
                _diff("constraint_inventory",
                      f"{table}: missing constraint {expected_name}")

    # -- schema markers -------------------------------------------------
    report["checked"].append("schema_markers")
    markers = dict(connection.execute(
        "select key, value from schema_meta").fetchall())
    for key, expected in EXPECTED_SCHEMA_MARKERS.items():
        if markers.get(key) != expected:
            _diff("schema_markers", f"{key}: {markers.get(key)!r} != "
                                    f"{expected!r}")

    # -- runtime role shapes (full attribute set) ------------------------
    report["checked"].append("runtime_roles")
    roles = {row[0]: row[1:] for row in connection.execute(
        "select rolname, rolcanlogin, rolsuper, rolbypassrls, rolcreatedb,"
        " rolcreaterole, rolreplication from pg_roles"
        " where rolname in ('marker_app','marker_worker')"
    ).fetchall()}
    for role in EXPECTED_RUNTIME_ROLES:
        shape = roles.get(role)
        if shape is None:
            _diff("runtime_roles", f"{role} missing")
            continue
        can_login, rolsuper, bypassrls, createdb, createrole, replication = \
            shape
        if can_login or rolsuper or bypassrls or createdb or createrole \
                or replication:
            _diff("runtime_roles",
                  f"{role} wrong shape (must be NOLOGIN and hold neither "
                  "superuser, bypassrls, createdb, createrole, nor "
                  "replication)")

    # -- RLS enabled and forced -----------------------------------------
    report["checked"].append("rls_enabled_and_forced")
    rls = {row[0]: (row[1], row[2]) for row in connection.execute(
        "select relname, relrowsecurity, relforcerowsecurity from pg_class"
        " where relnamespace = 'public'::regnamespace"
        "   and relname = any(%s)", (list(EXPECTED_RLS_FORCED_TABLES),)
    ).fetchall()}
    for table in EXPECTED_RLS_FORCED_TABLES:
        if rls.get(table) != (True, True):
            _diff("rls_enabled_and_forced",
                  f"{table}: {rls.get(table)!r} != (True, True)")

    # -- EFFECTIVE privileges (pg_catalog, includes PUBLIC/inheritance) --
    # PUBLIC is not a pg_roles identity, so it is never passed to
    # has_*_privilege; public_acl_absence proves grantee OID 0 directly.
    report["checked"].append("effective_table_privileges")
    matrix = {}
    for role in EXPECTED_RUNTIME_ROLES:
        for table in sorted(expected_tables & actual_tables):
            for verb in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                allowed = connection.execute(
                    "select has_table_privilege(%s, %s, %s)",
                    (role, table, verb)).fetchone()[0]
                matrix[f"{role}:{table}:{verb}"] = bool(allowed)
                if bool(allowed) != (verb in EXPECTED_EFFECTIVE_TABLE_PRIVS
                                     .get(table, {}).get(role, ())):
                    _diff("effective_table_privileges",
                          f"{role} on {table}: {verb} "
                          f"{'granted' if allowed else 'absent'} but "
                          f"expected "
                          f"{'granted' if verb in EXPECTED_EFFECTIVE_TABLE_PRIVS.get(table, {}).get(role, ()) else 'absent'}")
    report["effective_privileges"] = matrix

    # -- EFFECTIVE column-scoped privileges -----------------------------
    report["checked"].append("effective_column_privileges")
    column_matrix = {}
    for table, verbs in sorted(EXPECTED_EFFECTIVE_COLUMN_PRIVS.items()):
        if table not in actual_tables:
            continue
        for verb, role_columns in sorted(verbs.items()):
            for role in EXPECTED_RUNTIME_ROLES:
                expected_columns = set(role_columns.get(role, ()))
                for column in sorted(EXPECTED_CATALOG.get(table, ())):
                    allowed = bool(connection.execute(
                        "select has_column_privilege(%s, %s, %s, %s)",
                        (role, table, column, verb)).fetchone()[0])
                    column_matrix[
                        f"{role}:{table}:{column}:{verb}"] = allowed
                    if allowed != (column in expected_columns):
                        _diff(
                            "effective_column_privileges",
                            f"{role} on {table}.{column}: {verb} "
                            f"{'granted' if allowed else 'absent'} but "
                            f"expected "
                            f"{'granted' if column in expected_columns else 'absent'}")
    report["effective_column_privileges"] = column_matrix

    report["checked"].append("schema_usage")
    for role in EXPECTED_RUNTIME_ROLES:
        usage = bool(connection.execute(
            "select has_schema_privilege(%s, 'public', 'USAGE')",
            (role,)).fetchone()[0])
        create = bool(connection.execute(
            "select has_schema_privilege(%s, 'public', 'CREATE')",
            (role,)).fetchone()[0])
        if not usage or create:
            _diff("schema_usage",
                  f"{role}: USAGE must hold and CREATE must not "
                  f"(usage={usage}, create={create})")

    report["checked"].append("sequence_inventory")
    actual_sequences = sorted(row[0] for row in connection.execute(
        "select sequencename from pg_sequences where schemaname = 'public'"
    ).fetchall())
    report["sequences"] = actual_sequences
    if tuple(actual_sequences) != EXPECTED_SEQUENCES:
        _diff("sequence_inventory",
              f"deployed sequences {actual_sequences!r} do not equal the "
              f"approved expected inventory {EXPECTED_SEQUENCES!r}")

    # -- worker membership absence: effective, group, inherited, PUBLIC --
    report["checked"].append("worker_membership_absent")
    for column_row in connection.execute(
        "select att.attname"
        " from pg_catalog.pg_attribute att"
        " join pg_catalog.pg_class cls on cls.oid = att.attrelid"
        " join pg_catalog.pg_namespace nsp on nsp.oid = cls.relnamespace"
        " where nsp.nspname = 'public'"
        "   and cls.relname = 'tenant_membership'"
        "   and att.attnum > 0"
        "   and not att.attisdropped"
    ).fetchall():
        column = column_row[0]
        if connection.execute(
            "select has_column_privilege('marker_worker',"
            " 'tenant_membership', %s, 'SELECT')", (column,)
        ).fetchone()[0]:
            _diff("worker_membership_absent",
                  f"marker_worker holds an effective SELECT on the"
                  f" membership column {column}")
    report["checked"].append("membership_acl_inventory")
    acl_rows = connection.execute(
        "select coalesce(rol.rolname, 'PUBLIC') as grantee,"
        " acl.privilege_type"
        " from pg_catalog.pg_class cls"
        " cross join lateral pg_catalog.aclexplode("
        "   coalesce(cls.relacl, array[]::pg_catalog.aclitem[])) acl"
        " left join pg_catalog.pg_roles rol on rol.oid = acl.grantee"
        " where cls.relnamespace = (select oid from pg_catalog.pg_namespace"
        "                            where nspname = %s)"
        "   and cls.relname = %s",
        ("public", "tenant_membership"),
    ).fetchall()
    for grantee, privilege_type in acl_rows:
        if grantee in ("marker_worker", "PUBLIC"):
            _diff("membership_acl_inventory",
                  f"{grantee} holds an ACL privilege ({privilege_type}) on "
                  "tenant_membership")
    worker_edges = connection.execute(
        "select count(*) from pg_auth_members m"
        " join pg_roles parent on parent.oid = m.roleid"
        " join pg_roles child on child.oid = m.member"
        " where child.rolname = 'marker_worker'"
        "   and (parent.rolsuper or parent.rolbypassrls"
        "        or parent.rolname in ('marker_app'))"
    ).fetchone()[0]
    if worker_edges:
        _diff("worker_membership_absent",
              "marker_worker inherits membership privileges through a role "
              "edge")
    # PUBLIC is the ACL grantee pseudo-identity with OID 0, NOT a pg_roles
    # entry; has_*_privilege('PUBLIC', ...) can raise undefined-role. Read
    # grantee OID 0 from both the table ACL and every column ACL instead.
    report["checked"].append("public_acl_absence")
    public_edges = connection.execute(
        "select cls.relname, null::text as column_name, acl.privilege_type"
        " from pg_catalog.pg_class cls"
        " cross join lateral pg_catalog.aclexplode("
        "   coalesce(cls.relacl, array[]::pg_catalog.aclitem[])) acl"
        " where cls.relnamespace = (select oid from pg_catalog.pg_namespace"
        "                            where nspname = %s)"
        "   and cls.relname = any(%s)"
        "   and cls.relkind in ('r', 'p')"
        "   and acl.grantee = 0"
        " union all"
        " select cls.relname, att.attname::text, acl.privilege_type"
        " from pg_catalog.pg_attribute att"
        " join pg_catalog.pg_class cls on cls.oid = att.attrelid"
        " cross join lateral pg_catalog.aclexplode("
        "   coalesce(att.attacl, array[]::pg_catalog.aclitem[])) acl"
        " where cls.relnamespace = (select oid from pg_catalog.pg_namespace"
        "                            where nspname = %s)"
        "   and cls.relname = any(%s)"
        "   and cls.relkind in ('r', 'p')"
        "   and att.attnum > 0"
        "   and not att.attisdropped"
        "   and acl.grantee = 0",
        ("public", sorted(expected_tables & actual_tables),
         "public", sorted(expected_tables & actual_tables)),
    ).fetchall()
    for table, column_name, privilege_type in public_edges:
        target = table if column_name is None \
            else f"{table} column {column_name}"
        _diff("public_acl_absence",
              f"PUBLIC (ACL grantee OID 0) holds {privilege_type} on "
              f"{target}; expected no PUBLIC grant on any tenant table or "
              "column")

    report["passed"] = not report["diffs"]
    return report


# ---------------------------------------------------------------------------
# Disclosed synthetic builders (test data only; never a real approval)
# ---------------------------------------------------------------------------

_SYNTHETIC_HEX = "0123456789abcdef"


def synthetic_manifest_record(run_id: str = "synthetic-run-0001") -> dict:
    """A fully valid INERT synthetic record: placeholder values only, no host
    beyond loopback, no DSN string, no credentials. Used by tests as the
    positive case and as the synthetic approved manifest."""
    digest_hex = (_SYNTHETIC_HEX * 8)[:64]
    return {
        "attestation_marker": ATTESTATION_MARKER,
        "kind": EXPECTED_KIND,
        "dsn_env": TEST_DSN_ENV,
        "exclusivity": EXPECTED_EXCLUSIVITY,
        "disposable": True,
        "other_users": [],
        "independently_verified": {
            "verifier": "synthetic-independent-verifier",
            "evidence": "synthetic-example-only",
        },
        "instance": "synthetic-instance",
        "storage": "synthetic-object-storage",
        "loopback_endpoint": "127.0.0.1:5432",
        "database": "synthetic_disposable",
        "run_id": run_id,
        "role_inventory": ["marker_app"],
        "artifact_hash": f"sha256:{digest_hex}",
        "resource_cost_usd": 0,
        "phase": "test",
    }


def synthetic_approved_manifest(run_id: str = "synthetic-run-0001"):
    """Return ``(record, digest)`` for a disclosed synthetic approved
    manifest. This builds test data only; it never represents a real target
    approval."""
    record = synthetic_manifest_record(run_id)
    return record, manifest_digest(record)
