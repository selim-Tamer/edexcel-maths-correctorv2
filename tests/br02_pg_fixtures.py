"""BR-02 non-production PostgreSQL fixture seeder (source only; never
executed in this package).

LIVE-GATED, EXPLICITLY NON-PRODUCTION
-------------------------------------
Every entry point refuses until a future, independently supplied
``SeedApproval`` (the attested target manifest plus explicit execution
authorization) is handed in by the caller. Importing this module performs no
I/O and no environment read. Production seeding is prohibited without
exception: ``seed`` requires the caller to pass ``is_production`` explicitly
and refuses anything except exactly ``False`` - the explicit non-production
declaration lives only here.

What it seeds (synthetic provenance-shaped rows, INSERT-only)
-------------------------------------------------------------
* one synthetic tenant + one raw-subject membership (and a second tenant +
  membership for cross-tenant denial cases);
* one synthetic ``pdf_references`` row (server-derived sha256 + storage key);
* one synthetic server-minted ``assignments`` row (``asg_`` + 24 hex);
* one synthetic ``marking_jobs`` row already in ``succeeded`` state, linked
  to the bundle's result identity - inert historical data only. The engine,
  claim, lease, and settle code paths are never invoked;
* the ``results`` / ``result_context`` / ``audit_events`` rows are NOT seeded
  here: the anchor, the immutable original context and the audit anchor are
  created exclusively by the reviewed store adapter
  (``postgres_result_store.register_with_context``) so seeded provenance and
  runtime provenance can never drift.

Constraints honoured by construction
------------------------------------
* unique run/test IDs: every identity is derived from one unique ``run_id``,
  so parallel or repeated runs never collide and no clean-up is needed;
* NO DELETE, NO TRUNCATE, NO ``ON CONFLICT`` reactivation, NO implicit
  migrations: the capability precheck reads the version markers and refuses
  when the target is not already provisioned by the approved operator
  process;
* all statements are fully parameterized; no identifier or value is ever
  interpolated into SQL text.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import audit_store
import br02_pg_target as target

# Preserve one approval type for the parent runner's historical spelling and
# the fresh child's package import, without expanding the child's search path.
import sys as _ci_sys
_ci_sys.modules.setdefault('br02_pg_fixtures', _ci_sys.modules[__name__])
_ci_sys.modules.setdefault('tests.br02_pg_fixtures', _ci_sys.modules[__name__])
if 'tests' in _ci_sys.modules:
    setattr(_ci_sys.modules['tests'], 'br02_pg_fixtures', _ci_sys.modules[__name__])


# Transaction status of a psycopg connection that no other party is using.
_IDLE_TRANSACTION_STATUS = 0


@dataclass(frozen=True)
class SeedApproval:
    """The future authorization object, supplied from outside. Nothing in
    this package creates or invents one; a constructed object is still
    refused today by the unconditional execution gate."""

    approved_manifest: Optional[object] = None
    execution_authorized: bool = False
    approved_by: str = ""
    approval_ref: str = ""
    oidc_audience: str = "synthetic-test-audience"
    max_runs: int = 1
    # The attested target's dedicated test DSN, supplied independently of any
    # environment (the child and the live classes bind provenance to THIS
    # value; the parent process environment is never read for it).
    target_dsn: str = ""
    # The SEPARATELY SUPPLIED restricted runtime login DSNs and their shared
    # login name: the RLS/privilege probes connect as these restricted
    # logins - never as the admin DSN with SET ROLE. Empty today (the future
    # approval supplies them); the probes disclose-skip without them.
    app_dsn: str = ""
    worker_dsn: str = ""
    runtime_login: str = ""
    # The independently attested server identity the stage-2 comparison
    # binds to (never just a hostname): address, port, version prefix, and
    # the synthetic run marker that must equal the approved manifest's
    # run_id binding.
    attested_server_address: str = ""
    attested_server_port: int = 0
    attested_server_version_prefix: str = ""
    run_marker: str = ""
    # Explicit opt-in for the empty-target rollback drill (future only; the
    # populated-target refusals are always exercised, never skipped). The
    # drill runs against its OWN sacrificial disposable target, named here.
    rollback_drill_authorized: bool = False
    rollback_drill_dsn: str = ""


@dataclass(frozen=True)
class BootstrapApproval:
    """Separate stage-0 bootstrap authorization (future only).

    The bootstrap is the ONLY operation allowed to create the synthetic run
    marker, and only under this explicit authorization plus the unconditional
    execution gate. Normal seeding never creates, overwrites, reuses, or
    reactivates a marker. The marker value is the approved manifest's overall
    ``run_id`` - never a random per-test fixture-bundle id.
    """

    approved_manifest: Optional[object] = None
    bootstrap_authorized: bool = False
    approved_by: str = ""
    approval_ref: str = ""
    target_dsn: str = ""
    run_marker: str = ""


def _expected_run_marker(approval: Any) -> str:
    """The approved overall run identity a marker operation must match.

    The manifest is re-verified (digest + frozen binding), and the approval's
    explicit run marker must equal the manifest's ``run_id``. Per-test
    fixture-bundle ids never participate in this binding.
    """
    manifest = target.require_approved_manifest(
        getattr(approval, "approved_manifest", None))
    run_id = manifest.binding.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise target.TargetRefused(
            target.MANIFEST_MISMATCH,
            "the approved manifest run identity binding is missing or blank")
    marker = getattr(approval, "run_marker", "")
    if not isinstance(marker, str) or not marker.strip():
        raise target.TargetRefused(
            target.TARGET_IDENTITY_INCOMPLETE,
            "run_marker is required and must be non-blank")
    if marker != run_id:
        raise target.TargetRefused(
            target.MANIFEST_MISMATCH,
            "approval run_marker does not equal the approved manifest run "
            "identity")
    return marker


def require_seed_approval(approval: Any) -> SeedApproval:
    """Refuse anything that is not a well-formed, explicitly authorized
    seed approval. The final authority is the unconditional execution gate
    in ``br02_pg_target``, which refuses until the future approval path
    exists."""
    if not isinstance(approval, SeedApproval):
        raise target.TargetRefused(
            target.EXECUTION_NOT_AUTHORIZED,
            "seeding requires an explicitly supplied SeedApproval object; "
            "nothing is inferred from DSNs, environment, or flags")
    target.authorize_execution(approval)
    return approval


@dataclass(frozen=True)
class FixtureBundle:
    """One unique, internally consistent set of synthetic fixture
    identities. All values are inert and synthetic."""

    run_id: str
    tenant_id: str
    raw_subject: str
    canonical_owner: str
    peer_subject: str
    peer_canonical_owner: str
    peer_review_key: str
    peer_document_digest: str
    peer_result_id: str
    other_tenant_id: str
    other_subject: str
    result_id: str
    review_key: str
    original_output: dict
    original_digest: str
    original_scores_json: str
    document_digest: str
    storage_key: str
    size_bytes: int
    assignment_id: str
    assignment_idempotency_key: str
    assignment_title: str
    assignment_created_at: int
    job_id: str
    job_idempotency_key: str
    job_status: str
    job_attempts: int
    job_max_attempts: int
    job_created_at: int
    job_updated_at: int


def _digest_hex(seed: str, length: int = 64) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:length]


def build_bundle(run_id: Optional[str] = None) -> FixtureBundle:
    """Build one unique fixture bundle. All identities derive from the
    unique ``run_id``, so bundles from different runs never collide and no
    clean-up statements are ever needed.

    The result identity follows the REAL production contract
    (``decision_core.durable_result_identity``, manual scope):
    ``manual:<document_digest[:16]>:<review_key>``. The seeded succeeded job
    links the SAME identity, so job provenance and result provenance cannot
    drift."""
    run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
    tenant_id = f"tenant-{run_id}"
    raw_subject = f"teacher-{run_id}"
    review_key = f"rk-{run_id}"
    document_digest = _digest_hex(f"synthetic-pdf:{run_id}")
    # Real durable identity contract (manual scope): server-derived document
    # digest prefix + per-run review key.
    result_id = f"manual:{document_digest[:16]}:{review_key}"
    # A SECOND subject in the SAME tenant with its own valid result
    # identity: the owner-privacy proof needs a real peer, not a vacuous
    # single-owner query.
    peer_subject = f"teacher-peer-{run_id}"
    peer_review_key = f"rk-peer-{run_id}"
    peer_document_digest = _digest_hex(f"synthetic-pdf-peer:{run_id}")
    peer_result_id = f"manual:{peer_document_digest[:16]}:{peer_review_key}"
    original_output = {
        "questions": [{"question_number": "1", "awarded_marks": 2,
                       "max_marks": 3, "student_answer": "SYNTHETIC"}]}
    projection = audit_store.original_score_projection(original_output)
    job_created = 1_700_000_000 + int(_digest_hex(f"{run_id}:ts", 8), 16) \
        % 100_000_000
    return FixtureBundle(
        run_id=run_id,
        tenant_id=tenant_id,
        raw_subject=raw_subject,
        canonical_owner=f"google:{raw_subject}",
        peer_subject=peer_subject,
        peer_canonical_owner=f"google:{peer_subject}",
        peer_review_key=peer_review_key,
        peer_document_digest=peer_document_digest,
        peer_result_id=peer_result_id,
        other_tenant_id=f"tenant-other-{run_id}",
        other_subject=f"teacher-other-{run_id}",
        result_id=result_id,
        review_key=review_key,
        original_output=original_output,
        original_digest=audit_store.digest_value(original_output),
        original_scores_json=json.dumps(projection, separators=(",", ":")),
        document_digest=document_digest,
        storage_key=f"synthetic/{run_id}/{document_digest}.pdf",
        size_bytes=1024,
        assignment_id=f"asg_{_digest_hex(f'{run_id}:assignment', 24)}",
        assignment_idempotency_key=f"asg-key-{run_id}",
        assignment_title=f"SYNTHETIC assignment {run_id}",
        assignment_created_at=job_created,
        job_id=f"job-{_digest_hex(f'{run_id}:job', 32)}",
        job_idempotency_key=f"job-key-{run_id}",
        job_status="succeeded",
        job_attempts=1,
        job_max_attempts=3,
        job_created_at=job_created,
        job_updated_at=job_created,
    )


def seed_plan(bundle: FixtureBundle) -> list:
    """The exact INSERT-only statement plan (with tenant RLS bindings), in
    foreign-key order. No DELETE, no TRUNCATE, no ON CONFLICT, no DDL.

    Owner representations follow the real schema exactly:
    * ``pdf_references.owner_subject`` is the RAW membership subject (the
      ``fk_pdf_membership`` FK targets ``tenant_membership.subject``);
    * ``marking_jobs`` / ``assignments`` / the storage object key keep the
      CANONICAL ``google:<subject>`` owner (the result-context trusted-link
      FKs join on the canonical owner);
    * ``marking_jobs`` carries the bundle's ``assignment_id`` link so the
      registered result's trusted job/assignment provenance is consistent.
    """
    return [
        ("select set_config('app.tenant_id', %s, true)",
         (bundle.tenant_id,)),
        ("insert into tenants (tenant_id, status) values (%s, 'active')",
         (bundle.tenant_id,)),
        ("insert into tenant_membership (tenant_id, subject, role, active)"
         " values (%s, %s, 'teacher', TRUE)",
         (bundle.tenant_id, bundle.raw_subject)),
        ("insert into tenant_membership (tenant_id, subject, role, active)"
         " values (%s, %s, 'teacher', TRUE)",
         (bundle.tenant_id, bundle.peer_subject)),
        ("insert into pdf_references (tenant_id, owner_subject,"
         " document_digest, storage_key, size_bytes)"
         " values (%s, %s, %s, %s, %s)",
         (bundle.tenant_id, bundle.raw_subject, bundle.document_digest,
          bundle.storage_key, bundle.size_bytes)),
        ("insert into assignments (tenant_id, owner_subject, assignment_id,"
         " idempotency_key, title, created_at)"
         " values (%s, %s, %s, %s, %s, %s)",
         (bundle.tenant_id, bundle.canonical_owner, bundle.assignment_id,
          bundle.assignment_idempotency_key, bundle.assignment_title,
          bundle.assignment_created_at)),
        ("insert into marking_jobs (tenant_id, owner_subject, job_id,"
         " idempotency_key, document_digest, status, attempts, max_attempts,"
         " lease_until, result_id, error_code, created_at, updated_at,"
         " assignment_id)"
         " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
         (bundle.tenant_id, bundle.canonical_owner, bundle.job_id,
          bundle.job_idempotency_key, bundle.document_digest,
          bundle.job_status, bundle.job_attempts, bundle.job_max_attempts,
          0, bundle.result_id, None, bundle.job_created_at,
          bundle.job_updated_at, bundle.assignment_id)),
        ("select set_config('app.tenant_id', %s, true)",
         (bundle.other_tenant_id,)),
        ("insert into tenants (tenant_id, status) values (%s, 'active')",
         (bundle.other_tenant_id,)),
        ("insert into tenant_membership (tenant_id, subject, role, active)"
         " values (%s, %s, 'teacher', TRUE)",
         (bundle.other_tenant_id, bundle.other_subject)),
    ]


def _require_idle(connection) -> None:
    """Refuse a connection that is already inside a transaction: the seeder
    owns its one transaction end-to-end and never joins a caller's."""
    info = getattr(connection, "info", None)
    status = getattr(info, "transaction_status", None)
    if status is None:
        return  # test double without a status surface
    from psycopg.pq import TransactionStatus  # deferred, driver-only

    if status != TransactionStatus.IDLE:
        raise target.TargetRefused(
            target.INCONSISTENT_TARGET,
            "seeding requires an idle connection; the seeder owns exactly "
            "one transaction and never joins an open one")


def _check_capability(connection, approval: Any) -> None:
    """Fail closed when the target is not already provisioned AND has not
    been initialized with THIS approved run marker. This is a read-only marker
    check - migrations and marker creation are NEVER performed by normal
    seeding; the target must have been provisioned by the approved operator
    process and initialized by the separate stage-0 bootstrap. Runs INSIDE
    the seeder's owned transaction."""
    rows = dict(connection.execute(
        "select key, value from schema_meta"
        " where key in ('schema_version','result_context_version')"
    ).fetchall())
    if rows.get("schema_version") != "1" \
            or rows.get("result_context_version") != "1":
        raise target.TargetRefused(
            target.MISSING_TARGET,
            "target schema capability missing (schema_version/"
            "result_context_version); the target must be provisioned by the "
            "approved operator process - no implicit migrations are ever "
            "applied here")
    expected_marker = _expected_run_marker(approval)
    marker_row = connection.execute(
        "select value from schema_meta where key = %s",
        (target.STAGE2_RUN_MARKER_KEY,)).fetchone()
    if marker_row is None:
        raise target.TargetRefused(
            target.TARGET_NOT_INITIALIZED,
            "target synthetic run marker missing; normal seed never creates "
            "it - an explicitly authorized stage-0 bootstrap is separate")
    if marker_row[0] != expected_marker:
        raise target.TargetRefused(
            target.INCONSISTENT_TARGET,
            "target synthetic run marker does not match the approved run; "
            "normal seed never reuses or overwrites another run's identity")


def seed(connection, bundle: FixtureBundle, approval: Any, *,
         is_production: bool) -> dict:
    """Seed one unique fixture bundle on the caller's live connection.

    Gating order (before ANY statement reaches the connection):
    1. ``is_production`` must be passed explicitly and must be exactly
       ``False`` - production seeding is prohibited without exception;
    2. the seed approval must be well-formed and explicitly authorized, and
       the unconditional execution gate must allow it (it refuses today);
    3. the connection must be idle - the seeder owns its one transaction;
    4. the capability precheck must pass (no implicit migrations).

    The capability check, the tenant RLS bindings and every INSERT then run
    in ONE owned transaction (``BEGIN`` ... ``COMMIT``): the transaction-local
    tenant binding stays valid for all statements and no synthetic dependency
    is ever partially committed - any failure rolls the whole seed back.
    """
    if is_production is not False:
        raise target.TargetRefused(
            target.EXECUTION_NOT_AUTHORIZED,
            "production seeding is prohibited: this is the explicit "
            "non-production seeder only")
    require_seed_approval(approval)
    _require_idle(connection)
    transaction = getattr(connection, "transaction", None)
    if transaction is None:
        # Test doubles without a transaction surface: capability check only,
        # explicitly recorded as NOT the live path.
        _check_capability(connection, approval)
        return {"run_id": bundle.run_id, "seeded_rows": 0,
                "transaction": "absent-test-double"}
    try:
        with connection.transaction():
            _check_capability(connection, approval)
            for statement, params in seed_plan(bundle):
                connection.execute(statement, params)
    except Exception:
        # The transaction context has rolled back; nothing is partially
        # committed. Re-raise after the rollback completes.
        raise
    finally:
        # Own-connection hygiene: the seeder leaves the connection idle. If
        # anything above somehow leaked a transaction, end it here so the
        # caller's connection is never handed back in a broken state.
        info = getattr(connection, "info", None)
        status = getattr(info, "transaction_status", None)
        if status is not None and status != _IDLE_TRANSACTION_STATUS:
            rollback = getattr(connection, "rollback", None)
            if rollback is not None:
                rollback()
    return {
        "run_id": bundle.run_id,
        "tenant_id": bundle.tenant_id,
        "raw_subject": bundle.raw_subject,
        "peer_subject": bundle.peer_subject,
        "canonical_owner": bundle.canonical_owner,
        "result_id": bundle.result_id,
        "peer_result_id": bundle.peer_result_id,
        "job_id": bundle.job_id,
        "assignment_id": bundle.assignment_id,
        "document_digest": bundle.document_digest,
        "seeded_rows": len(seed_plan(bundle)),
        "transaction": "owned-one-transaction",
    }


def bootstrap_run_marker(connection, approval: Any, *,
                         is_production: bool) -> dict:
    """Stage-0 SOURCE-ONLY bootstrap of the synthetic run marker (future).

    This routine is NEVER called automatically by test setup or by
    :func:`seed`. It requires, in order, exactly ``False`` non-production,
    an explicit :class:`BootstrapApproval` with ``bootstrap_authorized`` set,
    an approved manifest whose overall ``run_id`` matches the approval's
    run marker, the unconditional execution gate (which refuses today), an
    idle connection, and the seeder's one owned transaction.

    If a marker already exists it must be EXACTLY this approved run's marker;
    the routine then verifies it and writes nothing. A marker belonging to
    another run is refused. There is no UPDATE, no ON CONFLICT, no
    reactivation, and no overwrite path.
    """
    if is_production is not False:
        raise target.TargetRefused(
            target.EXECUTION_NOT_AUTHORIZED,
            "production bootstrap is prohibited: this is the explicit "
            "non-production stage-0 bootstrap only")
    if not isinstance(approval, BootstrapApproval):
        raise target.TargetRefused(
            target.EXECUTION_NOT_AUTHORIZED,
            "bootstrap requires an explicitly supplied BootstrapApproval "
            "object; nothing is inferred from DSNs, environment, or flags")
    if approval.bootstrap_authorized is not True:
        raise target.TargetRefused(
            target.EXECUTION_NOT_AUTHORIZED,
            "bootstrap requires the separate explicit bootstrap_authorized "
            "approval")
    target.authorize_execution(approval)  # unconditional gate
    marker = _expected_run_marker(approval)
    _require_idle(connection)
    transaction = getattr(connection, "transaction", None)
    if transaction is None:
        raise target.TargetRefused(
            target.TARGET_NOT_INITIALIZED,
            "bootstrap requires a connection that owns a real transaction; "
            "a bare test double cannot create the marker")
    action = "created"
    try:
        with connection.transaction():
            row = connection.execute(
                "select value from schema_meta where key = %s",
                (target.STAGE2_RUN_MARKER_KEY,)).fetchone()
            if row is not None:
                if row[0] != marker:
                    raise target.TargetRefused(
                        target.INCONSISTENT_TARGET,
                        "an existing synthetic run marker belongs to another "
                        "run; bootstrap never overwrites, reuses, or "
                        "reactivates it")
                action = "verified-existing"
            else:
                # Stage-0 creation ONLY: a plain INSERT. No ON CONFLICT, no
                # UPDATE, no reactivation; a racing insert fails closed.
                connection.execute(
                    "insert into schema_meta (key, value) values (%s, %s)",
                    (target.STAGE2_RUN_MARKER_KEY, marker))
    finally:
        info = getattr(connection, "info", None)
        status = getattr(info, "transaction_status", None)
        if status is not None and status != _IDLE_TRANSACTION_STATUS:
            rollback = getattr(connection, "rollback", None)
            if rollback is not None:
                rollback()
    return {
        "run_marker": marker,
        "action": action,
        "transaction": "owned-one-transaction",
    }
