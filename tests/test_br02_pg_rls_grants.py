"""BR-02 PG harness: RLS and least-privilege grants (offline + PG-gated;
live classes unexecuted by design until the future approval exists - a skip
is the honest gate, never a pass).

Coverage map (gated, REAL operations against the live target):
* the restricted runtime LOGIN DSNs (app/worker) are supplied separately by
  the approval; probes connect as those logins directly - NEVER as the
  admin/owner DSN with SET ROLE (a superuser session keeps SET ROLE
  authority, so such a "proof" would be invalid);
* tenant-UNBOUND connections fail closed, and every GUC binding + probe runs
  in the SAME explicit transaction (set_config(..., true) is
  transaction-local: separate autocommit statements lose the binding); each
  expected-error probe owns its transaction and rolls back before more SQL;
* cross-tenant reads are indistinguishable from absent (same transaction);
* WITHIN-TENANT owner privacy is a REAL two-owner proof: two subjects in the
  SAME tenant, two valid result contexts registered through the real
  non-production owner-scoped store; owner A addressing B's KNOWN result id
  finds nothing and holds no replay receipt, while B legitimately sees B -
  enforced by the application/store owner scoping, explicitly NOT claimed
  as tenant RLS;
* runtime roles hold no elevation (rolsuper/rolbypassrls/rolcreatedb/
  rolcreaterole/rolreplication all false, no privileged inherited membership
  via pg_auth_members) and no CREATE role/grant is ever executed;
* the REAL stage-2 identity/catalog comparison (implementation behind the
  future approval gate) reports zero diffs, and grant snapshots covering
  ALL layers (effective has_*_privilege matrix, ACLs via aclexplode,
  membership edges) are unchanged by real harness operations.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root
sys.path.insert(0, str(Path(__file__).resolve().parent))      # tests/

try:
    import psycopg
except ImportError:  # pragma: no cover - driver ships in the test manifest
    psycopg = None

import br02_pg_fixtures as fixtures
import br02_pg_target
import identity_context
import postgres_result_store


def _live_approval():
    """Future supply point; returns None today, never inferred from
    env/DSN/flags."""
    return None


def _test_dsn():
    import os
    return os.environ.get(br02_pg_target.TEST_DSN_ENV, "").strip()


def _live_gate():
    """Authorization first: the current unauthorized phase is refused before
    ANY environment value access, driver connect, or module entrypoint."""
    if not br02_pg_target.execution_authorized():
        return ("live execution remains refused in the current phase: it "
                "requires a future independently approved target plus "
                "separate execution authorization; a DSN alone never "
                "unlocks it")
    approval = _live_approval()
    if approval is None:
        return ("no independently supplied seed approval exists for the "
                "attested target; the live classes skip until the lead "
                "supplies it")
    if psycopg is None:
        return "psycopg driver is not installed"
    if not _test_dsn():
        return ("AI_MARKER_TEST_PG_DSN is not configured - the BR-02 "
                "PostgreSQL gate is BLOCKED locally; never AI_MARKER_PG_DSN")
    return None


def _runtime_privilege_snapshot(connection):
    """ALL grant layers in one snapshot: the effective has_*_privilege
    matrix for the real runtime roles (these include PUBLIC and inherited
    grants), plus the ACL inventory of tables, columns, the schema, and
    sequences, plus the membership edges of the runtime roles.

    PUBLIC is never passed to has_*_privilege as a role argument: PUBLIC is
    not a role. Its grants are read from the ACL inventory, where grantee OID
    0 is rendered as the literal 'PUBLIC'. Empty ACL arrays are cast as
    ``pg_catalog.aclitem[]`` (the aclexplode argument type), never ``acl``."""
    snapshot = {"effective": [], "column_privileges": [],
                "sequence_privileges": [], "table_acls": [],
                "column_acls": [], "schema_acls": [], "sequence_acls": [],
                "memberships": []}
    for role in br02_pg_target.EXPECTED_RUNTIME_ROLES:
        for table in sorted(br02_pg_target.EXPECTED_CATALOG):
            for verb in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE",
                         "REFERENCES", "TRIGGER"):
                granted = bool(connection.execute(
                    "select has_table_privilege(%s, %s, %s)",
                    (role, table, verb)).fetchone()[0])
                if granted:
                    snapshot["effective"].append((role, table, verb))
        # Measure USAGE and CREATE SEPARATELY: a false CREATE says nothing
        # about whether USAGE actually holds.
        usage = bool(connection.execute(
            "select has_schema_privilege(%s, 'public', 'USAGE')",
            (role,)).fetchone()[0])
        create = bool(connection.execute(
            "select has_schema_privilege(%s, 'public', 'CREATE')",
            (role,)).fetchone()[0])
        snapshot["effective"].append((role, "schema:public:USAGE", usage))
        snapshot["effective"].append((role, "schema:public:CREATE", create))
        # Effective column-scoped privileges: 0005's membership resolver
        # columns and 0006's UPDATE(result_revision). Every column of the
        # relevant table is probed, so an over-grant also shows up.
        for table, verbs in sorted(
                br02_pg_target.EXPECTED_EFFECTIVE_COLUMN_PRIVS.items()):
            for verb, _role_columns in sorted(verbs.items()):
                for column in sorted(
                        br02_pg_target.EXPECTED_CATALOG.get(table, ())):
                    granted = bool(connection.execute(
                        "select has_column_privilege(%s, %s, %s, %s)",
                        (role, table, column, verb)).fetchone()[0])
                    if granted:
                        snapshot["column_privileges"].append(
                            (role, table, column, verb))
    snapshot["table_acls"] = [tuple(row) for row in connection.execute(
        "select cls.relname, coalesce(rol.rolname, 'PUBLIC'),"
        " acl.privilege_type"
        " from pg_class cls,"
        " lateral aclexplode(coalesce(cls.relacl,"
        " ARRAY[]::pg_catalog.aclitem[])) acl"
        " left join pg_roles rol on rol.oid = acl.grantee"
        " where cls.relnamespace = 'public'::regnamespace"
        "   and cls.relkind in ('r', 'p')"
        " order by 1, 2, 3").fetchall()]
    snapshot["column_acls"] = [tuple(row) for row in connection.execute(
        "select cls.relname, att.attname,"
        " coalesce(rol.rolname, 'PUBLIC'), acl.privilege_type"
        " from pg_attribute att"
        " join pg_class cls on cls.oid = att.attrelid,"
        " lateral aclexplode(coalesce(att.attacl,"
        " ARRAY[]::pg_catalog.aclitem[])) acl"
        " left join pg_roles rol on rol.oid = acl.grantee"
        " where cls.relnamespace = 'public'::regnamespace"
        "   and cls.relkind in ('r', 'p')"
        "   and att.attnum > 0 and not att.attisdropped"
        " order by 1, 2, 3, 4").fetchall()]
    snapshot["schema_acls"] = [tuple(row) for row in connection.execute(
        "select nsp.nspname, coalesce(rol.rolname, 'PUBLIC'),"
        " acl.privilege_type"
        " from pg_namespace nsp,"
        " lateral aclexplode(coalesce(nsp.nspacl,"
        " ARRAY[]::pg_catalog.aclitem[])) acl"
        " left join pg_roles rol on rol.oid = acl.grantee"
        " where nsp.nspname = 'public'"
        " order by 1, 2, 3").fetchall()]
    snapshot["sequence_acls"] = [tuple(row) for row in connection.execute(
        "select cls.relname, coalesce(rol.rolname, 'PUBLIC'),"
        " acl.privilege_type"
        " from pg_class cls,"
        " lateral aclexplode(coalesce(cls.relacl,"
        " ARRAY[]::pg_catalog.aclitem[])) acl"
        " left join pg_roles rol on rol.oid = acl.grantee"
        " where cls.relnamespace = 'public'::regnamespace"
        "   and cls.relkind = 'S'"
        " order by 1, 2, 3").fetchall()]
    snapshot["memberships"] = [tuple(row) for row in connection.execute(
        "select child.rolname, parent.rolname from pg_auth_members m"
        " join pg_roles child on child.oid = m.member"
        " join pg_roles parent on parent.oid = m.roleid"
        " where child.rolname in ('marker_app','marker_worker')"
        " order by 1, 2").fetchall()]
    # Sequence privileges, if the target has any public sequences (the
    # reviewed migrations define none, but the snapshot must not assume it).
    sequences = sorted(row[0] for row in connection.execute(
        "select sequencename from pg_sequences where schemaname = 'public'"
    ).fetchall())
    for role in br02_pg_target.EXPECTED_RUNTIME_ROLES:
        for sequence in sequences:
            for verb in ("USAGE", "SELECT", "UPDATE"):
                granted = bool(connection.execute(
                    "select has_sequence_privilege(%s, %s, %s)",
                    (role, f"public.{sequence}", verb)).fetchone()[0])
                if granted:
                    snapshot["sequence_privileges"].append(
                        (role, sequence, verb))
    return snapshot


def _bound_probe(connection, tenant_id, statement, params=(),
                 expected_error=None):
    """Bind app.tenant_id and run the probe in the SAME explicit
    transaction; an expected error aborts that transaction, which rolls
    back before any further SQL touches the connection."""
    try:
        with connection.transaction():
            connection.execute(
                "select set_config('app.tenant_id', %s, true)",
                (tenant_id,))
            return connection.execute(statement, params).fetchall()
    except expected_error:
        return None


class _LiveRlsBase(unittest.TestCase):
    """Live base: gate in setUpClass, one unique seeded bundle and one owned
    connection per test, rows retained, no cleanup SQL anywhere. The owner
    DSN provisions and seeds; the restricted runtime probes use the
    SEPARATELY SUPPLIED app/worker login DSNs."""

    @classmethod
    def setUpClass(cls):
        reason = _live_gate()
        if reason:
            raise unittest.SkipTest(reason)
        cls.dsn = _test_dsn()
        cls.approval = _live_approval()
        cls.conn = psycopg.connect(cls.dsn, autocommit=True)
        try:
            rows = dict(cls.conn.execute(
                "select key, value from schema_meta"
                " where key in ('schema_version','result_context_version')"
            ).fetchall())
        except Exception:
            cls.conn.close()
            raise
        if rows.get("schema_version") != "1" \
                or rows.get("result_context_version") != "1":
            cls.conn.close()
            raise unittest.SkipTest(
                "target schema capability missing; no implicit migrations "
                "are ever applied by tests")
        # The future approval must bind a run marker to the approved manifest
        # overall run identity, and the target must already carry that EXACT
        # marker (stage-0 bootstrap is separate and explicitly authorized).
        approval = cls.approval
        manifest = getattr(approval, "approved_manifest", None)
        binding = getattr(manifest, "binding", None)
        bound_run = binding.get("run_id") if isinstance(binding, dict) else None
        run_marker = getattr(approval, "run_marker", "")
        if not isinstance(run_marker, str) or not run_marker.strip() \
                or run_marker != bound_run:
            cls.conn.close()
            raise unittest.SkipTest(
                "the future approval supplies no run_marker bound to the "
                "approved manifest run identity; live classes stay "
                "unexecuted (disclosed, never faked)")
        try:
            marker = cls.conn.execute(
                "select value from schema_meta where key = %s",
                (br02_pg_target.STAGE2_RUN_MARKER_KEY,)).fetchone()
        except Exception:
            cls.conn.close()
            raise
        if marker is None:
            cls.conn.close()
            raise unittest.SkipTest(
                "the target has no approved synthetic run marker; stage-0 "
                "bootstrap is a separate explicitly authorized operation - "
                "live classes stay unexecuted")

    @staticmethod
    def _stage2_identity_gaps(approval):
        """Names of mandatory stage-2 expectations the future approval has
        not supplied. Any gap is a disclosed skip, never a false pass."""
        required = ("runtime_login", "attested_server_address",
                    "attested_server_version_prefix", "run_marker")
        gaps = [name for name in required
                if getattr(approval, name, None) in (None, "")]
        port = getattr(approval, "attested_server_port", None)
        if isinstance(port, bool) or not isinstance(port, int) \
                or not (0 < port <= 65535):
            gaps.append("attested_server_port")
        if not getattr(approval, "app_dsn", ""):
            gaps.append("app_dsn")
        return sorted(set(gaps))

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "conn", None) is not None:
            cls.conn.close()

    def setUp(self):
        self.conn = psycopg.connect(self.dsn, autocommit=True)
        try:
            self.bundle = fixtures.build_bundle()
            fixtures.seed(self.conn, self.bundle, self.approval,
                          is_production=False)
            self.app_connection = self._runtime_connection(
                getattr(self.approval, "app_dsn", ""))
        except Exception:
            self.conn.close()
            raise

    def tearDown(self):
        if getattr(self, "app_connection", None) is not None:
            self.app_connection.close()
        if getattr(self, "conn", None) is not None:
            self.conn.close()

    def _runtime_connection(self, runtime_dsn):
        """A REAL restricted runtime login connection, supplied separately -
        never the owner DSN with SET ROLE."""
        if not runtime_dsn:
            self.skipTest(
                "the approval supplies no restricted app login DSN yet; the "
                "runtime probes stay unexecuted (disclosed, never faked)")
        return psycopg.connect(runtime_dsn, autocommit=True)

    def store(self, raw_subject=None):
        return postgres_result_store.open_store(
            self.dsn,
            identity=identity_context.from_verified_subject(
                raw_subject or self.bundle.raw_subject,
                self.bundle.tenant_id),
            fresh_completion_denied=False)

    def register_contexts_for_both_owners(self):
        """Two REAL result contexts for two subjects in the SAME tenant."""
        for subject, result_id, review_key, document_digest in (
                (self.bundle.raw_subject, self.bundle.result_id,
                 self.bundle.review_key, self.bundle.document_digest),
                (self.bundle.peer_subject, self.bundle.peer_result_id,
                 self.bundle.peer_review_key,
                 self.bundle.peer_document_digest)):
            store = self.store(subject)
            try:
                store.register_with_context(
                    result_id=result_id,
                    original_output=self.bundle.original_output,
                    scope="manual", review_key=review_key,
                    document_digest=document_digest)
            finally:
                store.close()


class RlsFailClosedTests(_LiveRlsBase):
    """Runtime-role behavior under RLS, proven on the restricted logins."""

    def test_tenant_unbound_fails_closed_on_the_restricted_login(self):
        self.register_contexts_for_both_owners()
        rows = _bound_probe(self.app_connection, None,
                            "select count(*) from result_context")
        self.assertEqual(rows, [(0,)],
                         "no tenant binding: zero rows (fail closed)")
        refused = _bound_probe(
            self.app_connection, None,
            "insert into result_context (tenant_id, membership_subject,"
            " owner_subject, result_id, result_scope, review_key,"
            " original_output, original_digest)"
            " values (%s, %s, %s, 'x', 'manual', 'k', '{}', %s)",
            (self.bundle.tenant_id, self.bundle.raw_subject,
             self.bundle.canonical_owner, "0" * 64),
            expected_error=psycopg.errors.InsufficientPrivilege)
        self.assertIsNone(refused, "an unbound insert must be refused")

    def test_cross_tenant_is_indistinguishable_from_absent(self):
        self.register_contexts_for_both_owners()
        rows = _bound_probe(self.app_connection, self.bundle.other_tenant_id,
                            "select count(*) from result_context where"
                            " result_id = %s", (self.bundle.result_id,))
        self.assertEqual(rows, [(0,)])

    def test_within_tenant_owner_privacy_is_store_level(self):
        """REAL two-owner proof in the SAME tenant: A cannot address B's
        KNOWN result through the owner-scoped store, and holds no replay
        receipt for it, while B legitimately sees B. Enforced by the
        application/store owner scoping (composite keys + explicit
        predicates) - deliberately NOT claimed as tenant RLS, which only
        separates tenants."""
        self.register_contexts_for_both_owners()
        store_a = self.store(self.bundle.raw_subject)
        try:
            self.assertIsNone(
                store_a.visible_row(self.bundle.peer_result_id),
                "owner A must not see owner B's known result id")
            with self.assertRaises(audit_store_error()) as caught:
                store_a.snapshot(result_id=self.bundle.peer_result_id)
            self.assertEqual(caught.exception.code, "result_not_registered")
            with self.assertRaises(audit_store_error()) as caught:
                store_a.commit_correction(
                    owner=self.bundle.peer_canonical_owner,
                    result_id=self.bundle.peer_result_id,
                    expected_revision=0, idempotency_key="a-on-b",
                    payload={"question_number": "1"},
                    event={"actor": self.bundle.peer_canonical_owner,
                           "question_number": "1", "action": "approve"})
            self.assertEqual(caught.exception.code, "result_not_registered",
                             "A holds no replay receipt for B's result")
        finally:
            store_a.close()
        store_b = self.store(self.bundle.peer_subject)
        try:
            snapshot = store_b.snapshot(result_id=self.bundle.peer_result_id)
            self.assertIsNotNone(snapshot, "legitimate B sees B")
            self.assertEqual(snapshot["result_revision"], 0)
        finally:
            store_b.close()

    def test_runtime_roles_hold_no_elevation_on_their_own_login(self):
        # On the RESTRICTED login session (not an admin session, which keeps
        # SET ROLE authority and would make this probe vacuous):
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            self.app_connection.execute("set role postgres")
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            self.app_connection.execute(
                "create table probe_elevation (id int)")
        shapes = self.conn.execute(
            "select rolname, rolsuper, rolbypassrls, rolcreatedb,"
            " rolcreaterole, rolreplication from pg_roles"
            " where rolname in ('marker_app','marker_worker')"
            " order by rolname").fetchall()
        self.assertEqual(len(shapes), 2)
        for _name, rolsuper, bypassrls, createdb, createrole, replication \
                in shapes:
            self.assertFalse(rolsuper)
            self.assertFalse(bypassrls)
            self.assertFalse(createdb)
            self.assertFalse(createrole)
            self.assertFalse(replication)
        privileged_edges = self.conn.execute(
            "select count(*) from pg_auth_members m"
            " join pg_roles child on child.oid = m.member"
            " join pg_roles parent on parent.oid = m.roleid"
            " where child.rolname in ('marker_app','marker_worker')"
            "   and (parent.rolsuper or parent.rolbypassrls"
            "        or parent.rolcreatedb or parent.rolcreaterole"
            "        or parent.rolreplication)").fetchone()[0]
        self.assertEqual(privileged_edges, 0,
                         "no inherited privileged membership")

    def test_worker_membership_still_absent_everywhere(self):
        self.assertEqual(
            self.conn.execute(
                "select has_table_privilege('marker_worker',"
                " 'tenant_membership', 'SELECT')").fetchone()[0], False)
        for column in ("tenant_id", "subject", "role", "active",
                       "updated_at"):
            self.assertEqual(
                self.conn.execute(
                    "select has_column_privilege('marker_worker',"
                    " 'tenant_membership', %s, 'SELECT')",
                    (column,)).fetchone()[0], False, column)
        self.assertEqual(
            self.conn.execute(
                "select count(*) from pg_auth_members m"
                " join pg_roles child on child.oid = m.member"
                " where child.rolname = 'marker_worker'").fetchone()[0], 0,
            "the worker group inherits nothing at all")

    def test_stage2_catalog_comparison_reports_zero_diffs(self):
        gaps = self._stage2_identity_gaps(self.approval)
        if gaps:
            raise unittest.SkipTest(
                "the future approval supplies no restricted runtime "
                "identity/connection (%s); stage 2 stays unexecuted - a skip "
                "is the honest gate, never a pass" % ", ".join(gaps))
        # The ACTUAL restricted runtime login connection, whose
        # session_user/current_user must equal the attested runtime_login -
        # never the fixture-owner admin DSN.
        report = br02_pg_target.stage2_run_catalog_comparison(
            self.app_connection, self.approval,
            self.approval.approved_manifest)
        self.assertEqual(report["diffs"], [],
                         "the deployed catalog must equal the expected "
                         "identity; diffs: " + repr(report["diffs"]))
        self.assertTrue(report["passed"])
        for check in ("attested_server_identity", "synthetic_run_marker",
                      "table_inventory",
                      "column_inventory", "constraint_inventory",
                      "effective_table_privileges",
                      "effective_column_privileges", "schema_usage",
                      "sequence_inventory", "worker_membership_absent",
                      "membership_acl_inventory"):
            self.assertIn(check, report["checked"])

    def test_no_runtime_grant_changes_across_operations(self):
        before = _runtime_privilege_snapshot(self.conn)
        self.register_contexts_for_both_owners()
        after = _runtime_privilege_snapshot(self.conn)
        self.assertEqual(before, after,
                         "no harness operation ever changes any grant layer")


def audit_store_error():
    import audit_store
    return audit_store.AuditStoreError


if __name__ == "__main__":
    unittest.main()
