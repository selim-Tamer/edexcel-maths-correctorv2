# BR-02 PostgreSQL Test Harness — Specification & Implementation

This document describes the complete BR-02 PostgreSQL test harness as implemented for the AI Maths Marker project. The harness enforces **strict offline-only validation** with hard blocks on network, database, process, and import access before any test execution.

## Harness Overview

**Branch:** `br-02/postgres-result-persistence`  
**Base commit:** `73fd02c0efa14e3427490ad381f7e8715298597b` (merged PR #81)  
**Runner:** `tests/run_br02_pg_harness.py --mode offline`

### Key Properties

| Property | Implementation |
|----------|---------------|
| **Bytecode disabled** | `python -B` (no `.pyc` files) |
| **Network blocks** | Socket/DNS/driver probes raise `TargetRefused` |
| **Process blocks** | `subprocess`/`multiprocessing` spawns refused |
| **Import blocks** | `api.server`, `psycopg` (live path) blocked at import |
| **Environment guard** | Synthetic-neutral mapping; credential lookups fail |
| **Execution gate** | `authorize_execution()` refuses unconditionally |
| **18-file allowlist** | 2 schema docs + 2 repaired test files + 4 support modules + 9 test modules + 1 docs file (see below) |

---

## 18-File Allowlist

This is the original harness allowlist. The separately authorized inactive
Stage-2 gate amendment changes only `tests/br02_pg_authorization.py` (new),
`tests/br02_pg_target.py`, `tests/test_br02_pg_preflight.py` and this document.
It does not expand live-execution authority.

The authoritative IR-reviewed BR-02-PG-HARNESS-01 scope contains 18 unique file paths. Below are listed every unique path exactly once, with the runner and documentation file included per the specification.

### 2 Schema Documentation Files

| File | Description |
|------|-------------|
| `schema/MIGRATIONS.md` | Migration reference documentation; removed stale implication about v9 granting worker membership reads (section 4.6, 0009 verification) |
| `schema/postgres_v9_result_context_rollback.sql` | Draft rollback SQL: elevated role check, ACCESS EXCLUSIVE lock before populated check, empty-only removal, no widening/disabling |

### 2 Repaired Test Files

| File | Description |
|------|-------------|
| `tests/test_postgres_result_store_contract.py` | Immutable-safe fixture tests: unique synthetic identities, retain immutable rows, preserve anchors/memberships, no DELETE/TRUNCATE/RLS disable/reactivation |
| `tests/test_postgres_api_pg_contract.py` | Same immutable-safe fixture requirements; static wiring + live store adapter layers |

### 4 Support Modules (New)

| File | Specification Section | Purpose |
|------|----------------------|---------|
| `tests/br02_pg_target.py` | §5, §6 | Target identity, execution gate, manifest validation, refusal codes |
| `tests/br02_pg_fixtures.py` | §7 | Non-production fixture seeder: unique run IDs, INSERT-only plan, capability precheck, bootstrap marker |
| `tests/br02_pg_api_child.py` | §6 | Live API child launcher: allowlisted env, restricted runtime, forced production policy, synthetic verifier disclosure, loopback-only network block |
| `tests/run_br02_pg_harness.py` | Runner | BR-02-PG-HARNESS offline runner: enforces blocks, import gate, and test execution under offline mode |

### 9 Test Modules

| File | Specification Section | Purpose |
|------|----------------------|---------|
| `tests/test_br02_pg_preflight.py` | §5 | Positive identity, adversarial refusal matrix, manifest schema, execution gate, block behavior, environment guard, runner behavior, seed markers, Stage-2 binding |
| `tests/test_br02_pg_api.py` | §9 | Production API persistence contract (startup entrypoint + live API tests) |
| `tests/test_br02_pg_registration.py` | §10 | Registration both-or-nothing contract |
| `tests/test_br02_pg_transactions.py` | §11 | Transaction integrity under fault injection |
| `tests/test_br02_pg_concurrency.py` | §12 | Concurrency with barrier-synchronized overlap, REPEATABLE READ reader |
| `tests/test_br02_pg_processes.py` | §13 | Owned child-process coverage (readiness, bounded requests, strict reaping) |
| `tests/test_br02_pg_rls_grants.py` | §14 | RLS and least-privilege grants (runtime login DSNs, tenant-unbound fails closed, within-tenant owner privacy) |
| `tests/test_br02_pg_worker_denial.py` | §15 | Worker fresh-completion denial (offline REAL behavior + PG-gated zero-side-effect proof) |
| `tests/test_br02_pg_rollback.py` | §16 | Guarded rollback draft behavior (offline structural + PG-gated live) |

### 1 Documentation File

| File | Description |
|------|-------------|
| `docs/BR02_PG_HARNESS.md` | This specification document, describing the complete BR-02 PostgreSQL test harness, its properties, allowlist, and operation |

> **Total: 18 unique file paths** — 2 schema docs + 2 repaired test files + 4 support modules + 9 test modules + 1 docs file. The runner `tests/run_br02_pg_harness.py` and documentation `docs/BR02_PG_HARNESS.md` are included in the 18-file scope as specified.

---

## Preflight Tests (`test_br02_pg_preflight.py`)

### Positive Identity Validation
- `PositiveIdentityTests`: Sample record passes every rule; validation completes against approved manifest; validation is pure/repeatable; reads no environment

### Adversarial Refusal Matrix
- `RefusalMatrixTests`: Missing record, unknown record, shared record, self-asserted record, inconsistent record, production DSN fallback; refusals never echo raw values

### Manifest Schema
- `ManifestTests`: Digest is deterministic and sensitive; each binding field bound exactly; supply refuses tampered digest; supply refuses invalid approved record; synthetic approved manifest validates end-to-end; validation without supplied manifest refuses; wrongly shaped manifest refuses

### Execution Gate
- `ExecutionGateTests`: `authorize_execution` refuses unconditionally; execution never authorized; valid candidate with approved manifest still cannot execute

### Block Behavior
- `BlockBehaviorTests`: DNS resolution blocked; TCP connect paths blocked; UDP sendto blocked; Postgres driver paths blocked when installed; installation is idempotent

### Environment Guard
- `EnvironmentGuardTests`: API server import refused offline; credential lookups fail under guard; guard reads only neutral names and never copies; offline process spawns refused; restore rebinds original reference

### Runner Behavior
- `RunnerBehaviorTests`: Exact module contract; live mode refuses with exit 2; live refusal precedes block installation and imports; runner reads no DSN or credential environment

### Seed Marker Sources
- `SeedMarkerSourceTests`: Refuses missing marker without creating it; refuses another run's marker
- `BootstrapMarkerSourceTests`: Creates missing marker once with plain insert; refuses another run's marker without overwrite; requires separate authorization and exact non-production; verifies exact existing marker without writing

### Stage-2 Identity Binding
- `Stage2IdentityBindingSourceTests`: Approval must carry approved manifest; input marker must equal manifest run identity; matching identity and actual marker pass logic; missing actual marker refuses initialized stage; missing/invalid identity refuses before any query; missing/tampered manifest refuses before any query; wrong actual marker is a diff; wrong server/database/current_user are diffs

---

## Live API Child Launcher (`br02_pg_api_child.py`)

### Child Process Properties
1. **Separate allowlisted environment** - Only `CHILD_ENV_ALLOWLIST` names read from parent; never wholesale copy; never `AI_MARKER_PG_DSN`
2. **Restricted runtime handle** - Approved manifest's role inventory checked against `assert_restricted_roles`; never administrative
3. **Production policy forced on** - `AI_MARKER_RUNTIME=production`; exercises REAL fail-closed production composition with REAL result/audit/membership/job adapters; fresh completion stays denied (default production posture)
4. **Disclosed test-auth boundary** - ONLY synthetic component is token verifier (`synthetic-subject:<subject>`); labeled as synthetic; never touches persistence/membership/provenance
5. **Inert synthetic external config** - Engine disabled (`AI_MARKER_ENGINE_REAL=0`); OIDC audience is synthetic test value; TLS never relaxed
6. **External-network hard block** - Outbound to non-loopback destinations raises; loopback (PG target + child HTTP) stays reachable
7. **Reusable child CLI** - `python -m tests.br02_pg_api_child --serve --port N` gates, hard-blocks, builds real app, serves on loopback with readiness endpoint

### Key Functions
- `build_child_environ(parent_environ, approval, dsn)` - Builds allowlisted environment
- `build_child_application(environ, approval)` - Composes REAL production application
- `run_child_server(approval, ready_timeout_seconds, timeout_seconds)` - Spawns owned child server
- `synthetic_verifier(token)` - Disclosed synthetic test verifier

---

## Immutable-Safe Fixture Requirements

**Critical Rule:** Test fixtures MUST preserve all immutable rows (results, audit_events, result_context). No DELETE, TRUNCATE, RLS disable, or ON CONFLICT reactivation is permitted.

### Enforcement Points
1. **Unique synthetic identities** - Every identity derived from unique `run_id` (UUID-based)
2. **Retain immutable rows** - Seeded rows and runtime-written rows never deleted
3. **Preserve anchors/memberships** - `results` anchor, `audit_events` chain, `tenant_membership` rows retained
4. **No DELETE/TRUNCATE** - Seeder plan is INSERT-only with RLS bindings
4. **No RLS disable/reactivation** - Policies remain FORCE ENABLED; no security policy changes
5. **Capability precheck refuses without implicit migrations** - Target must be pre-provisioned by approved operator

### Fixture Bundle Identity Derivation
```python
run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
tenant_id = f"tenant-{run_id}"
raw_subject = f"teacher-{run_id}"
canonical_owner = f"google:{raw_subject}"
document_digest = sha256(f"synthetic-pdf:{run_id}")
result_id = f"manual:{document_digest[:16]}:rk-{run_id}"  # Real durable identity contract
assignment_id = f"asg_{sha256(f'{run_id}:assignment')[:24]}"
job_id = f"job-{sha256(f'{run_id}:job')[:32]}"
```

---

## Migration SQL Invariants (Tested Statically)

### `postgres_v9_result_context.sql` Must Have:
- `CREATE TABLE IF NOT EXISTS result_context` with composite PK `(tenant_id, membership_subject, result_id)`
- Composite FK to `results (tenant_id, owner_subject, result_id)`
- Composite FK to `tenant_membership (tenant_id, subject)`
- FK to `marking_jobs` and `assignments` (trusted links)
- Unique constraint on `(tenant_id, owner_subject, result_id)`
- `ENABLE ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY`
- `tenant_isolation` policy using `current_setting('app.tenant_id', true)`
- Immutability trigger: `BEFORE UPDATE OR DELETE ON result_context` raising exception
- `GRANT SELECT, INSERT ON result_context TO marker_app, marker_worker`
- `result_context_version = '1'` capability marker (distinct from `schema_version = '1'`)
- `owner_subject = 'google:' || membership_subject` check constraint

### `postgres_v9_result_context_rollback.sql` Must Have:
- `SET LOCAL search_path TO pg_catalog, public` (pinned namespace order)
- Elevated role check: `rolsuper OR rolbypassrls` from `pg_catalog.pg_roles` BEFORE any DDL
- Expected state validation: table exists AND is ordinary table (relkind 'r') AND capability marker exists AND equals '1'; any deviation refuses
- `LOCK TABLE public.result_context IN ACCESS EXCLUSIVE MODE` BEFORE populated check
- Populated check refuses: "result_context is populated: rollback refused to preserve original results"
- Empty-only removal: DROP TRIGGER, DROP FUNCTION, DROP POLICY, DROP TABLE, DELETE marker
- No CASCADE, no SECURITY DEFINER, no DISABLE RLS, no DISABLE TRIGGER, no GRANT

---

## Fail-Closed Composition Gates

### Production App Refuses Without PostgreSQL DSN
```python
env = {RUNTIME_ENV: RUNTIME_PRODUCTION, PG_DSN_ENV: "", ALLOWED_ORIGINS_ENV: "https://marker.example", ...}
with mock.patch.dict("os.environ", env, clear=True):
    with self.assertRaises(RuntimeConfigError) as caught:
        create_app(verify_token=lambda token: SUBJECT, allowed_origins=["https://marker.example"])
    self.assertEqual(caught.exception.code, "store_unusable")
```

### Service Translation Rules
- Canonical owner (`google:<subject>`) → membership subject (raw) for storage FKs
- Foreign canonical owner refused with `tenant_membership_required`
- Verified actor required for corrections (actor must equal verified owner)

### Worker Path Denial
- Fresh completion denied by default in production
- Explicit `fresh_completion_denied=False` cannot weaken production policy
- Worker startup refuses production composition
- Worker run-one refuses before acquisition

---

## Running the Harness

### Offline Mode (Default, No PostgreSQL Required)
```bash
cd C:\Users\user\OpenBot\Shared\BR02_BACKEND_WORKTREE
python -B tests/run_br02_pg_harness.py --mode offline
```

**Expected Output:**
```
test_dns_resolution_is_blocked ... ok
test_installation_is_idempotent ... ok
...
test_authorize_execution_refuses_unconditionally ... ok
test_execution_is_never_authorized ... ok
...
Ran 184 tests
OK (skipped=10)
BR-02 PG harness: mode=offline bytecode_off=True blocks=blocked/blocked entries=10 process_blocks=4 env_guard=synthetic-neutral modules=11
```

For the reviewed inactive-gate slice this means 184 successful executed
methods and 10 separate live-class setup skips, not 174 successful methods.
Counts are evidence for that exact slice, not a fixed acceptance threshold.

### Live Mode (Inactive: Always Refused)
```bash
# This command exits 2; it cannot activate PostgreSQL execution.
python -B tests/run_br02_pg_harness.py --mode live
```

**Live classes skip with explicit reason when gates not satisfied** - this is the honest gate, never a pass. No DSN, environment flag, receipt object or command-line option unlocks this runner.

### Bounded Stage-2 Authorization Contract (Inactive)

The only scoped operation is `br02.stage2.identity_catalog.readonly`.
`br02_pg_target.stage2_authorized_readonly(approval_reference)` delegates to
`br02_pg_authorization.execute_stage2`. Its fixed `_trusted_authority()` hook
unconditionally raises `authority_unavailable`, before evidence/file I/O or any
connection attempt. There is no authority registration function, configurable
provider, receipt-file loader, environment activation or caller connector.
`execution_authorized()` remains false; generic `authorize_execution()` still
refuses. Seed, bootstrap, migration, rollback, worker, API/process and broader
integration paths remain disabled. This implementation authorizes none of them.

The future trusted-host interface must supply authenticated explicit owner
approval and independently reviewed target evidence from outside ordinary
source configuration. An approval reference locates evidence; it never approves
it. The receipt binds owner/review references, a unique authorization ID, exact
operation, exact target-manifest digest, artifact inventory/digest, instance and
storage identity, loopback address/port, database, direct restricted login,
synthetic run ID and exact PostgreSQL 17 version text. The reviewed manifest
must independently agree, including zero resource cost and operation phase.

Local validation rejects absent/inconsistent/substituted evidence, revoked or
expired/not-yet-valid approval, more than one attempt, validity windows over
300 seconds and operation durations outside 1-60 seconds. These are ceilings;
the owner-approved limits may be shorter. Receipt objects are immutable values,
not bearer credentials accepted by an execution API.

Source inventory starts at `tests/br02_pg_authorization.py` and
`tests/br02_pg_target.py` and follows local Python imports transitively without
importing them. Every discovered source byte is SHA-256 hashed. The expected
inventory is exactly sorted, without missing/extra/duplicate entries; its digest
is SHA-256 of the compact JSON sequence of path/hash pairs. Unknown third-party,
package, relative or dynamic imports fail closed. Symlink paths are refused.
The current executable source closure is exactly those two files; the offline
runner, test fixtures and documentation are not operation code dependencies.
The entire closure is rechecked after consumption and before opening.

Only the trusted external authority may atomically and durably consume an
authorization against its complete evidence digest, current expiry and
revocation state. Consumption must precede opening and be single-use across
threads, processes and restarts. False/unknown/error results deny; failed opens
or queries do not restore an attempt. The local implementation has no persisted
receipt or consumption store. Offline tests use only inert, lock-protected
in-memory authority doubles; they do not establish durable authority behavior.

After consumption, the authority's connector must enforce exact instance/storage
binding, no alternate endpoint or retries, a direct non-elevated login, read-only
transactions, pinned `pg_catalog, public` search path and server/host-enforced
connection, statement and total-operation deadlines. The dispatcher verifies
server address/port, exact version, database, session/current login, read-only
setting, search path, bounded statement timeout and absence of elevated direct
or inherited role membership before catalog work. The existing comparator
checks the actual synthetic run marker before proceeding; identity or marker
differences stop further catalog reads and return a failed report. Missing
markers refuse. Connections close on success, mismatch and failure.

The one-entry dispatch permission is bound by object identity to the internally
created guarded connection, approval and manifest in a context-local scope; it
is cleared after use. It never enables a global live flag. Generic supplied
connections remain denied. Monotonic duration and wall-clock expiry checks
surround dispatch and queries. Blocking calls and cleanup additionally require
the future authority's host-enforced total deadline; offline checks do not
prove those runtime properties.

Trust boundary: source digests are integrity checks, not signatures or proof of
owner consent. The host must authenticate approval/review evidence and protect
its authority, clock, runtime/stdlib, imported code and source snapshot against
substitution. Arbitrary mutation/monkeypatching inside the trusted Python
process is outside this boundary. No claim is made that ordinary Python
visibility protects an authority from a hostile process owner. Instance/storage
identity needs independent host evidence, not merely an agreeing database name.
Establishing the authority or a launch path requires separate reviewed approval.

Independent acceptance must inspect all four changed files; trace every
connection path, artifact dependency and operation boundary; inspect the
negative and concurrent-consumption tests; and verify default denial, cleanup,
error redaction and preservation hashes. Formal IR/ECC acceptance and real
PostgreSQL testing remain separate. No external authority, target, credentials,
driver installation or live launch is supplied by this package.

---

## ECC Review Gates (Required Before Production)

| Reviewer | Focus |
|----------|-------|
| `ecc:code-reviewer` | Python implementation correctness, PEP 8, type hints, security |
| `ecc:database-reviewer` | PostgreSQL schema, RLS, grants, migration/rollback SQL, privilege narrowing |
| `ecc:security-reviewer` | Fail-closed gates, identity mapping, credential isolation, network blocks, execution authorization |

### Review Invocation
```bash
# Code review
python -m ecc.code_reviewer --target tests/test_postgres_result_store_contract.py --level high

# Database review
python -m ecc.database_reviewer --target schema/postgres_v9_result_context.sql --level high

# Security review
python -m ecc.security_reviewer --target api/app.py --level high
```

---

## Architecture Decision Records

### Why Separate `result_context` Table?
- 0001 stores only original DIGEST + marks projection in `results`
- Complete original marking output lives in development ingest registry (not production-usable)
- BR-02 stores complete original + authoritative context in separate immutable table
- Atomic with existing audit anchor row (`results`)

### Why Composite FKs?
- `results.owner_subject` = membership subject (existing FK representation)
- `result_context` records raw membership subject AND canonical `google:<subject>` owner
- No reader has to guess one from the other
- Composite anchor FK makes context row without `results` row impossible
- Composite membership FK keeps ownership in existing membership table

### Why SELECT + INSERT Only for Runtime Roles?
- Originals are immutable and history is never trimmed
- Retention remains separate elevated-role decision
- Trigger refuses UPDATE/DELETE at row level as defense in depth

### Why No Worker Membership Grant?
- Owner direction required for any privilege expansion (stop condition 17.6)
- Worker ownership resolution must be resolved outside this schema
- Two alternatives (not implemented): store `membership_subject` on `marking_jobs`, or resolve at API layer and pass out-of-band

### Why Capability Marker Distinct from `schema_version`?
- Adapter fails closed without it
- `schema_version` stays '1'; v9 capability recorded separately
- Exactly like earlier numbered migrations (rls_version, jobs_version, etc.)

---

## Regression Test Coverage (Per AGENTS.md)

| Regression | Test Coverage |
|------------|---------------|
| Q77 conflicting list with full marks | `test_conflicting_original_under_the_same_identity_is_refused` |
| Q25 incorrect arithmetic | Arithmetic rounding tests in `test_arithmetic_rounding.py` |
| Q21 intermediary vs final fractions | `test_full_validators_accept_rounding_but_keep_wrong_final_and_marks` |
| Q47 scientific notation | `test_approximate_operators_and_integer_rounding` |
| Labels Q1/Sum of 5 | Label validation in arithmetic tests |
| Q6 open-ended interval explanations | Audit reliability tests |
| Q49 self-corrections and contradictory totals | `test_idempotent_commit_correction_replays_saved_receipt`, `test_fault_before_commit_preserves_committed_original` |

---

## GitHub Workflow Integration

`.github/workflows/marking-tests.yml` runs offline checks on each PR/push and daily. On failures on main or scheduled runs it opens/updates a GitHub issue. The workflow executes:

```yaml
- name: Run BR-02 offline harness
  run: python -B tests/run_br02_pg_harness.py --mode offline
```

---

## Next Steps (Post-Beta Deployment)

After beta, the architecture shifts to:
- **Frontend:** React/Vite on Cloudflare Pages
- **Backend:** Google Cloud Run (FastAPI)
- **Database:** Supabase (PostgreSQL)
- **Storage:** Cloudflare R2 (student PDFs)

The current harness ensures the PostgreSQL persistence layer is fully validated before any cloud deployment. The live PostgreSQL tests remain gated until an authorized operator provisions a disposable synthetic database and supplies the independent seed approval.


## Isolated GitHub Actions CI amendment (inactive until publication and explicit dispatch)

The historical offline runner remains offline-only. The separate `tests/run_br02_pg_ci.py` entrypoint is the only CI execution route. This implementation does not activate `br02_pg_authorization._trusted_authority()` or allow a CI context to call the live Stage 2 catalog comparison. Default gate refusal remains covered by the existing harness.

The owner approved an implementation-only permission amendment: the single integration job has `contents: read` plus `id-token: write`. OIDC is used only for signed GitHub run identity; no cloud credential exchange is implemented. No workflow publication, token issuance, database execution or Stage 2 operation is approved by that amendment.

### Future execution boundary

The workflow is manual-only, its execution input defaults false, and the job additionally requires the repository owner, the BR-02 branch, run attempt 1 and the explicitly approved commit equal to the workflow commit. Registration on main cannot satisfy the BR-02 branch guard. The registration commit may still trigger existing repository workflows; publication requires separate owner approval of those consequences.

The privileged bootstrap verifies GitHub RS256 OIDC signature and fixed issuer/audience, repository/owner/actor IDs, public visibility, workflow/ref/commit, run/attempt, hosted-runner type and token time bounds. Environment flags alone are never authority. The issuer ticket is single-use, expires after five minutes and binds the run/commit before a maximum eight-minute local context can be installed. The worktree must be clean and at the approved commit.

The service is PostgreSQL 17.11 Alpine, pinned by registry index digest `sha256:3540c0362c809df084ffac0ae69fe794515f9de92692f02178bcdce0ef0302ac` (Linux/amd64 metadata verified during implementation). Only `127.0.0.1:55432` is published. Bootstrap validates the exact service container/image, one private network, one anonymous PostgreSQL data volume, actual server address/version/database and initially empty public schema. Host loopback transport and container bridge server address are distinct identity fields. The database name is `br02_ci`; run identity and storage/container IDs make each invocation distinct.

The fixed bootstrap-only service password is intentionally synthetic and confined to the fresh private runner. It is replaced with a random temporary password before test execution. App, worker and non-bypass fixture-owner passwords are generated only in future authorized CI. No credentials are generated in offline checks. No repository/production secret is used, and PostgreSQL Docker logging is disabled to avoid retaining SQL/password text. Raw errors, credentials and DSNs never enter the test result report.

Migrations 0001 through 0009 are applied explicitly in their existing order using `search_path=public`; their bytes are not modified. The setup SQL preserves group-role grants, creates direct restricted login roles, revokes public schema creation and transfers test tables to a separate non-bypass fixture owner for FORCE-RLS owner proof. Privileged rollback-refusal tests retain the bootstrap fixture connection; the non-bypass owner test uses the owner connection. App/worker connections never inherit bootstrap privileges. Existing runtime UPDATE/DELETE contract methods use a restricted session login after fixture setup, so rolling back their SET ROLE transaction cannot restore a privileged login. Their assertions remain unchanged.

The worker uses reserved UID 60001 only if absent from account/process inventories. Per-UID IPv4 rules allow loopback and the approved service address/port, with other traffic rejected; IPv6 is rejected. GitHub agent/root traffic is not affected. A root-owned read-only snapshot is created from the approved Git commit only (no ignored files or credentials). The worker reads this snapshot rather than widening access to the runner home. The worker drops groups/GID/UID, clears the runner environment and inherits only authenticated in-memory approval plus a root-owned, anonymous, read-only, time-limited child capability. API grandchildren inherit a duplicate descriptor, containing restricted app credentials only; no bootstrap credential crosses that boundary. Child descriptors are checked for issuer ownership, unlink/read-only status, integrity and expiry. Root and the approved source are trust prerequisites; an administrator able to replace Python code or memory is outside this boundary.

All test processes belong to an owned session/process group. Existing child readiness/request/shutdown bounds remain. Parent worker duration is capped at 480 seconds, job duration at 15 minutes, SQL statements at 15 seconds and lock waits at 3 seconds (individual tests may set tighter limits). A lingering process group, unknown cleanup state or rule-removal failure is a failure; remaining cleanup is still attempted. GitHub service/VM teardown is the final disposable-storage boundary. No business-row DELETE/TRUNCATE cleanup or data reuse is introduced.

### Exact integration inventory and exclusions

`TESTS` in the new runner pins 52 existing method names. Exactly 50 are selected. Missing/extra methods, empty suites, unexpected skips, expected failures and missing result IDs fail the run. Assertions are reused from the existing classes. Approval supply hooks return the current authenticated approval object, not a stubbed true gate. Historical top-level/package import spellings share one target authority and manifest type.

The two named live exclusions are:
- `tests.test_br02_pg_rls_grants.RlsFailClosedTests.test_stage2_catalog_comparison_reports_zero_diffs`
- `tests.test_br02_pg_rollback.RollbackRefusalTests.test_empty_privileged_success_is_future_only`

Real Google authentication, Supabase, Cloudflare, AI-provider calls and deployment remain excluded. Passing the CI subset does not fully accept BR-02. A separate sacrificial database/approval is required for successful empty rollback; Stage 2 remains independently gated.

### Verification and future publication

Offline checks are `python -B tests/test_br02_pg_ci_context.py` and `python -B tests/run_br02_pg_harness.py --mode offline`. The new tests guard sockets/drivers/process launches and key/password generation; native Linux, Docker, SQL and genuine OIDC issuance are not executed. Ubuntu/Python 3.13 compatibility remains a source/design assessment until a separately authorized run. The existing pinned test requirements are reused; uvicorn is additionally installed using the existing production version constraint solely for child-server tests.

The repository was public at read-only inspection. Standard Ubuntu compute is expected to be free; no paid runner, upload or persistent Actions cache is configured. Account billing usage/budgets and effective account-wide concurrency could not be verified. Visibility changes, larger runners or billable services require fresh owner approval. One concurrency group, one job, no matrix and no automatic retries are configured.

Independent source review is required before any Git staging, commit, push, default-branch registration or dispatch. Existing BR-02 uncommitted files must be reviewed and published as an explicit file/hash manifest, never blindly staged with `git add .`. The new workflow must be registered on the default branch without deploying application code; the existing workflow's automatic runs and any external deployment hooks must be resolved/approved before that registration. Dispatch must then name the reviewed BR-02 SHA and explicitly approve execution. No such publication or execution occurred during this amendment.


### P1 issuer-provenance correction (offline candidate)

Caller-built Context objects and `_current.set()` never issue authority. The historical `_register()` now refuses unconditionally. Only the single closure-owned issuer registry can register contexts: authenticated OIDC issuance consumes a short-lived ticket, or child adoption verifies an inherited root-owned anonymous read-only descriptor. Ticket and live registries are not caller-writable module dictionaries. Caller-computable fingerprints detect mutation; they are not provenance. Duplicate JWT IDs, reused tickets, target changes after ticket binding, cross-run/attempt/commit mismatches and malformed child scope are refused. Both CI-context import spellings share one issuer registry.

Child transport carries only restricted app credentials plus expected run/commit/attempt values, checked against the inherited binding. It cannot delegate children or fixture authority. Fixture contexts require an active authenticated parent and retain that parent's app/worker credentials, runtime login, target identity, scope and expiry. Stage 2 and successful rollback-drill authority remain excluded. Native descriptor creation occurs only inside authenticated installation; no public helper mints that transport.

These are API provenance controls within trusted Python source. Python closure introspection, monkeypatching verification/code, a hostile root administrator and transfer of a genuine capability by a trusted process are outside this boundary. The descriptor's in-band HMAC is an integrity check, not independent issuer authentication; OS ownership, unlink/read-only state, inherited possession and the sandbox UID establish that transport boundary. No native Linux/OIDC/Docker result is claimed by inert verification.
