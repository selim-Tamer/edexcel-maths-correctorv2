"""BR-02-PG-HARNESS preflight tests (pure offline, inert inputs).

Behavioral, not source-mirroring: the tests exercise actual calls and
observe actual refusals, block engagements, and environment-access spies.
No network, no database, no filesystem writes, and no environment reads or
writes of any kind happen here (the process environment is never touched;
a recording stand-in is patched in where environment independence is
proven).

Covered:
* positive identity validation of a fully valid inert sample record;
* the adversarial refusal matrix: missing / unknown / shared / inconsistent /
  self-asserted / production DSN fallback, including whitespace, case, and
  type-confusion variants;
* refusal exceptions never echo raw candidate values (markers, verifier
  names, DSN-shaped strings);
* the finalized manifest schema: digest binding, separate trusted supply,
  exact field-for-field comparison, and refusal without a supplied approved
  manifest;
* the separate live-execution gate: refused unconditionally, even for a
  fully valid candidate with an approved manifest;
* block behavior of the runner: every network/DNS/driver entry point raises
  when invoked (probes raise before any syscall), verified behaviorally;
* runner behavior: live mode refuses with exit 2 before any block
  installation or import, and the runner and the validator read no
  environment at all (recording-stand-in spy).
"""

import contextlib
import copy
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root
sys.path.insert(0, str(Path(__file__).resolve().parent))      # tests/

import br02_pg_fixtures as fixtures
import br02_pg_target as target
import run_br02_pg_harness as runner


class _RecordingMapping(dict):
    """Stand-in for os.environ that records every read. The real process
    environment is never read, copied, or modified."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reads = []

    def __getitem__(self, key):
        self.reads.append(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self.reads.append(key)
        return super().get(key, default)


def assert_refused(testcase, record, reason, approved=None):
    with testcase.assertRaises(target.TargetRefused) as caught:
        target.validate_target(record, approved)
    testcase.assertEqual(caught.exception.reason, reason)
    return caught.exception


class PositiveIdentityTests(unittest.TestCase):
    """A fully valid inert record passes every rule and, with a supplied
    synthetic approved manifest, completes metadata validation."""

    def setUp(self):
        self.record = target.synthetic_manifest_record()
        manifest_record, digest = target.synthetic_approved_manifest()
        self.approved = target.supply_approved_manifest(manifest_record,
                                                        digest)

    def test_sample_record_passes_every_rule(self):
        normalized = target.check_identity(self.record)
        self.assertEqual(normalized.kind, target.EXPECTED_KIND)
        self.assertEqual(normalized.dsn_env, target.TEST_DSN_ENV)
        target.check_exclusivity(self.record)
        target.check_verification(self.record)
        target.check_dsn_env(self.record)
        target.check_consistency(self.record)
        target.check_loopback(self.record)

    def test_validation_completes_against_the_approved_manifest(self):
        normalized = target.validate_target(self.record, self.approved)
        self.assertEqual(normalized.attestation_marker,
                         target.ATTESTATION_MARKER)

    def test_validation_is_pure_and_repeatable(self):
        first = target.validate_target(self.record, self.approved)
        second = target.validate_target(self.record, self.approved)
        self.assertEqual(first, second)

    def test_validation_reads_no_environment(self):
        spy = _RecordingMapping()
        with mock.patch.object(os, "environ", spy):
            target.validate_target(self.record, self.approved)
        self.assertEqual(spy.reads, [])


class RefusalMatrixTests(unittest.TestCase):
    """Each malformed variant is refused with its specific reason,
    including adversarial whitespace, case, and type-confusion inputs."""

    def _mutant(self, **changes):
        record = copy.deepcopy(target.synthetic_manifest_record())
        record.update(changes)
        return record

    def test_missing_record(self):
        for bad in (None, {}, [], "not-a-mapping", 0, b"{}"):
            with self.subTest(bad=bad):
                assert_refused(self, bad, target.MISSING_TARGET)

    def test_unknown_record(self):
        for marker in ("other-marker", 123, None,
                       " " + target.ATTESTATION_MARKER + " "):
            with self.subTest(marker=marker):
                assert_refused(self, self._mutant(attestation_marker=marker),
                               target.UNKNOWN_TARGET)
        record = self._mutant()
        del record["attestation_marker"]
        assert_refused(self, record, target.UNKNOWN_TARGET)

    def test_shared_record(self):
        for exclusivity in ("shared_team", "", "EXCLUSIVE_SINGLE_AGENT"):
            with self.subTest(exclusivity=exclusivity):
                assert_refused(self, self._mutant(exclusivity=exclusivity),
                               target.SHARED_TARGET)
        record = self._mutant()
        del record["exclusivity"]
        assert_refused(self, record, target.SHARED_TARGET)

    def test_self_asserted_record(self):
        for verified in ({"verifier": "self", "evidence": "trust-me"},
                         {"verifier": "SELF", "evidence": "x"},
                         {"verifier": " Self ", "evidence": "x"},
                         {"verifier": "", "evidence": "x"},
                         {"verifier": "someone", "evidence": ""},
                         {"verifier": "someone"},
                         True,
                         "verified-by-me"):
            with self.subTest(verified=verified):
                assert_refused(
                    self, self._mutant(independently_verified=verified),
                    target.SELF_ASSERTED_TARGET)
        record = self._mutant()
        del record["independently_verified"]
        assert_refused(self, record, target.SELF_ASSERTED_TARGET)

    def test_production_dsn_fallback(self):
        for dsn_env in ("AI_MARKER_PG_DSN",
                        "AI_MARKER_PG_DSN_SECONDARY",
                        "SOME_OTHER_DSN",
                        "",
                        # A raw DSN string is never acceptable in the field
                        # that names the DSN variable.
                        "postgresql://inert-example.invalid/db"):
            with self.subTest(dsn_env=dsn_env):
                assert_refused(self, self._mutant(dsn_env=dsn_env),
                               target.PRODUCTION_DSN_FALLBACK)
        record = self._mutant()
        del record["dsn_env"]
        assert_refused(self, record, target.PRODUCTION_DSN_FALLBACK)

    def test_inconsistent_record(self):
        for changes in ({"kind": "production_postgresql"},
                        {"kind": "DISPOSABLE_SYNTHETIC_POSTGRESQL"},
                        {"disposable": False},
                        {"disposable": "yes"},
                        {"other_users": ["someone-else"]},
                        {"loopback_endpoint": "10.0.0.5:5432"},
                        {"loopback_endpoint": "db.internal.example:5432"},
                        {"role_inventory": ["superuser"]},
                        {"role_inventory": ["marker_admin"]},
                        {"role_inventory": []},
                        {"resource_cost_usd": 5},
                        {"resource_cost_usd": "0"},
                        {"resource_cost_usd": True}):
            with self.subTest(changes=changes):
                assert_refused(self, self._mutant(**changes),
                               target.INCONSISTENT_TARGET)
        record = self._mutant()
        del record["instance"]
        assert_refused(self, record, target.INCONSISTENT_TARGET)

    def test_refusals_never_echo_raw_values(self):
        """Adversarial markers, verifier names, and DSN-shaped strings must
        never appear in any refusal message."""
        sentinels = ("attest-SECRET-marker-xyz",
                     "postgresql://SECRET-HOST-xyz/db",
                     "SECRET-VERIFIER-xyz",
                     "SECRET-EVIDENCE-xyz")
        poisoned = self._mutant(
            attestation_marker=sentinels[0],
            dsn_env=sentinels[1],
            independently_verified={"verifier": sentinels[2],
                                    "evidence": sentinels[3]},
        )
        cases = [poisoned, self._mutant(kind="production_postgresql"),
                 self._mutant(exclusivity="shared")]
        for case in cases:
            with self.subTest(case=type(case).__name__):
                try:
                    target.validate_target(case)
                except target.TargetRefused as exc:
                    message = str(exc)
                    for sentinel in sentinels:
                        self.assertNotIn(sentinel, message)
                else:
                    self.fail("expected a refusal")


class ManifestTests(unittest.TestCase):
    """The finalized manifest schema: digest binding, separate trusted
    supply, exact comparison, refusal without a supplied manifest."""

    def test_synthetic_approved_manifest_validates_end_to_end(self):
        record, digest = target.synthetic_approved_manifest()
        approved = target.supply_approved_manifest(record, digest)
        normalized = target.validate_target(
            target.synthetic_manifest_record(), approved)
        self.assertEqual(normalized.kind, target.EXPECTED_KIND)

    def test_validation_without_a_supplied_manifest_refuses(self):
        assert_refused(self, target.synthetic_manifest_record(),
                       target.NO_APPROVED_MANIFEST)

    def test_wrongly_shaped_manifest_refuses(self):
        with self.assertRaises(target.TargetRefused) as caught:
            target.validate_target(target.synthetic_manifest_record(),
                                   approved_manifest={"not": "a manifest"})
        self.assertEqual(caught.exception.reason, target.NO_APPROVED_MANIFEST)

    def test_each_binding_field_is_bound_exactly(self):
        for field in target.MANIFEST_BINDING_FIELDS:
            with self.subTest(field=field):
                record = target.synthetic_manifest_record()
                if field == "role_inventory":
                    record[field] = ["marker_app", "marker_worker"]
                elif field == "resource_cost_usd":
                    record[field] = record[field] + 5
                else:
                    record[field] = record[field] + "-changed"
                approved = target.supply_approved_manifest(
                    *target.synthetic_approved_manifest())
                # resource_cost_usd is bound at the shape level (only 0 is a
                # valid cost), so a changed value refuses as inconsistent
                # before the manifest comparison can report a mismatch.
                expected = (target.INCONSISTENT_TARGET
                            if field == "resource_cost_usd"
                            else target.MANIFEST_MISMATCH)
                exc = assert_refused(self, record, expected, approved)
                if expected is target.MANIFEST_MISMATCH:
                    self.assertIn(field, exc.detail)
                    self.assertNotIn("-changed", exc.detail)

    def test_supply_refuses_a_tampered_digest(self):
        record, _ = target.synthetic_approved_manifest()
        with self.assertRaises(target.TargetRefused) as caught:
            target.supply_approved_manifest(record, "0" * 64)
        self.assertEqual(caught.exception.reason, target.MANIFEST_MISMATCH)

    def test_supply_refuses_an_invalid_approved_record(self):
        record, _ = target.synthetic_approved_manifest()
        record["exclusivity"] = "shared_team"
        with self.assertRaises(target.TargetRefused) as caught:
            target.supply_approved_manifest(record, "0" * 64)
        self.assertEqual(caught.exception.reason, target.SHARED_TARGET)

    def test_digest_is_deterministic_and_sensitive(self):
        first = target.manifest_digest(target.synthetic_manifest_record())
        second = target.manifest_digest(target.synthetic_manifest_record())
        self.assertEqual(first, second)
        other = target.manifest_digest(
            target.synthetic_manifest_record(run_id="synthetic-run-0002"))
        self.assertNotEqual(first, other)
        self.assertEqual(len(first), 64)


class ExecutionGateTests(unittest.TestCase):
    """Metadata validity never implies live-execution authorization."""

    def test_execution_is_never_authorized(self):
        self.assertFalse(target.execution_authorized())

    def test_authorize_execution_refuses_unconditionally(self):
        for approval in (None, {}, "approval", object()):
            with self.subTest(approval=type(approval).__name__):
                with self.assertRaises(target.TargetRefused) as caught:
                    target.authorize_execution(approval)
                self.assertEqual(caught.exception.reason,
                                 target.EXECUTION_NOT_AUTHORIZED)

    def test_valid_candidate_with_approved_manifest_still_cannot_execute(self):
        record, digest = target.synthetic_approved_manifest()
        approved = target.supply_approved_manifest(record, digest)
        target.validate_target(target.synthetic_manifest_record(), approved)
        with self.assertRaises(target.TargetRefused) as caught:
            target.authorize_execution(approved)
        self.assertEqual(caught.exception.reason,
                         target.EXECUTION_NOT_AUTHORIZED)


class BlockBehaviorTests(unittest.TestCase):
    """Behavioral proof that the runner's blocks engage: every network, DNS,
    and driver entry point raises instead of attempting anything. Probes call
    the installed wrappers with inert arguments; wrappers raise before any
    syscall, and methods are invoked unbound (self=None) so no socket object
    is ever created."""

    @classmethod
    def setUpClass(cls):
        cls.status = runner.install_connection_blocks()
        runner.verify_blocks(cls.status)

    def _assert_blocked(self, probe):
        # Match on the stable refusal marker, never on class identity: the
        # runner module can exist twice (as __main__ and as an import), and
        # the installed wrappers may belong to either copy.
        with self.assertRaises(RuntimeError) as caught:
            probe()
        self.assertIs(
            getattr(caught.exception, "_br02_harness_refusal", False), True)

    def test_tcp_connect_paths_are_blocked(self):
        import socket

        self._assert_blocked(lambda: socket.socket.connect(
            None, ("127.0.0.1", 5432)))
        self._assert_blocked(lambda: socket.socket.connect_ex(
            None, ("127.0.0.1", 5432)))
        self._assert_blocked(lambda: socket.create_connection(
            ("127.0.0.1", 5432)))

    def test_udp_sendto_is_blocked(self):
        import socket

        self._assert_blocked(lambda: socket.socket.sendto(
            None, b"x", ("127.0.0.1", 5432)))

    def test_dns_resolution_is_blocked(self):
        import socket

        self._assert_blocked(lambda: socket.getaddrinfo("127.0.0.1", 5432))
        self._assert_blocked(lambda: socket.gethostbyname("127.0.0.1"))
        self._assert_blocked(lambda: socket.gethostbyname_ex("127.0.0.1"))

    def test_postgres_driver_paths_are_blocked_when_installed(self):
        try:
            import psycopg
        except ImportError:
            self.skipTest("psycopg is not installed; nothing to block")
        self._assert_blocked(lambda: psycopg.connect("host=127.0.0.1"))
        self._assert_blocked(
            lambda: psycopg.Connection.connect("host=127.0.0.1"))
        self._assert_blocked(
            lambda: psycopg.AsyncConnection.connect("host=127.0.0.1"))

    def test_installation_is_idempotent(self):
        again = runner.install_connection_blocks()
        self.assertEqual(again["socket"], "already-blocked")
        runner.verify_blocks(again)


class EnvironmentGuardTests(unittest.TestCase):
    """Behavioral proof of the offline ENV SAFETY guard: the synthetic
    neutral mapping is built by reference-rebinding with per-name neutral
    reads only (never a copy or iteration of the real environment),
    credential lookups fail, tests may mutate the synthetic map, and OS
    process spawns plus ``api.server`` imports are refused offline."""

    def test_guard_reads_only_neutral_names_and_never_copies(self):
        class _RefusingSource(dict):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.reads = []

            def get(self, key, default=None):
                self.reads.append(key)
                return dict.get(self, key, default)

            def _refuse(self, *args, **kwargs):
                raise AssertionError(
                    "the guard must never copy or iterate the real "
                    "environment")

            items = _refuse
            values = _refuse
            keys = _refuse
            copy = _refuse
            __iter__ = _refuse

        source = _RefusingSource({
            "PATH": "synthetic-path",
            "AI_MARKER_PG_DSN": "postgresql://ambient-secret/db",
            "GEMINI_API_KEY": "synthetic-not-real",
        })
        synthetic = runner._SyntheticEnviron(source,
                                             runner.NEUTRAL_ENV_KEYS)
        self.assertEqual(synthetic.get("PATH"), "synthetic-path")
        self.assertEqual(source.reads, list(runner.NEUTRAL_ENV_KEYS),
                         "only the explicitly neutral names are read, one "
                         "by one")
        self.assertNotIn("AI_MARKER_PG_DSN", synthetic)
        self.assertNotIn("GEMINI_API_KEY", synthetic)

    def test_credential_lookups_fail_under_the_guard(self):
        import os

        real = runner.install_environment_guard()
        try:
            for name in ("AI_MARKER_PG_DSN", "AI_MARKER_TEST_PG_DSN",
                         "GEMINI_API_KEY", "AI_MARKER_SUPABASE_SERVICE_KEY"):
                with self.subTest(name=name):
                    with self.assertRaises(KeyError):
                        os.environ[name]
                    self.assertIsNone(os.environ.get(name))
            # Tests may mutate the synthetic map; ambient names stay absent.
            os.environ["SYNTHETIC_TEST_KEY"] = "synthetic-value"
            self.assertEqual(os.environ["SYNTHETIC_TEST_KEY"],
                             "synthetic-value")
            del os.environ["SYNTHETIC_TEST_KEY"]
            self.assertNotIn("AI_MARKER_PG_DSN", os.environ)
        finally:
            runner.restore_environment(real)

    def test_restore_rebinds_the_original_reference(self):
        import os

        sentinel = {"sentinel": "reference"}
        with mock.patch.object(os, "environ", sentinel):
            real = runner.install_environment_guard()
            try:
                self.assertIsNot(os.environ, sentinel)
                runner.restore_environment(real)
                self.assertIs(os.environ, real,
                              "the guard restores the SAME mapping reference")
            finally:
                runner.restore_environment(real)
        # Outside the patch the guard's reference restore is the identity
        # operation on the real module attribute.
        real = runner.install_environment_guard()
        try:
            self.assertIsNot(os.environ, real)
            runner.restore_environment(real)
            self.assertIs(os.environ, real)
        finally:
            runner.restore_environment(real)

    def test_offline_process_spawns_are_refused(self):
        import subprocess

        real_env = runner.install_environment_guard()
        try:
            runner.install_offline_process_block()
            with self.assertRaises(RuntimeError) as caught:
                subprocess.Popen(["python", "-c", "pass"])
            self.assertIs(getattr(caught.exception, "_br02_harness_refusal",
                                  False), True)
            # A repeated install returns normally (no raise) and the blocks
            # remain engaged.
            runner.install_offline_process_block()
            with self.assertRaises(RuntimeError) as again:
                subprocess.Popen(["python", "-c", "pass"])
            self.assertIs(getattr(again.exception, "_br02_harness_refusal",
                                  False), True)
        finally:
            runner.restore_environment(real_env)

    def test_api_server_import_is_refused_offline(self):
        import importlib

        real_env = runner.install_environment_guard()
        try:
            runner.install_offline_process_block()
            sys.modules.pop("api.server", None)
            with self.assertRaises(RuntimeError) as caught:
                importlib.import_module("api.server")
            self.assertIs(getattr(caught.exception, "_br02_harness_refusal",
                                  False), True)
        finally:
            runner.restore_environment(real_env)


class _Rows:
    """Minimal fetch surface for the recording doubles (offline only)."""

    def __init__(self, rows):
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


_MISSING = object()


class _Stage2Double:
    """Recording stage-2 connection double. It records every statement in
    order, so the tests can prove mandatory validation happens BEFORE any
    query, and it answers the catalog queries from the expected identity."""

    def __init__(self, **identity):
        self.calls = []
        self.identity = {
            "address": "127.0.0.1",
            "port": 5432,
            "version": "PostgreSQL 16.1 (synthetic)",
            "database": "synthetic_disposable",
            "session_user": "marker_app_login",
            "current_user": "marker_app_login",
            "marker": "synthetic-run-0001",
        }
        self.identity.update(identity)

    def execute(self, sql, params=None):
        lowered = " ".join(sql.lower().split())
        self.calls.append((lowered, params))
        if "inet_server_addr" in lowered:
            return _Rows([(
                self.identity["address"], self.identity["port"],
                self.identity["version"], self.identity["database"],
                self.identity["session_user"],
                self.identity["current_user"])])
        if "from schema_meta where key" in lowered:
            marker = self.identity["marker"]
            return _Rows([] if marker is _MISSING else [(marker,)])
        if "select cls.relname from pg_catalog.pg_class cls" in lowered:
            return _Rows([(name,) for name in
                          sorted(target.EXPECTED_CATALOG)])
        if "select att.attname from pg_catalog.pg_attribute att" in lowered:
            if params:
                table = params[0]
                return _Rows([(column,) for column in
                              sorted(target.EXPECTED_CATALOG[table])])
            return _Rows([(column,) for column in
                          sorted(target.EXPECTED_CATALOG[
                              "tenant_membership"])])
        if "information_schema.tables" in lowered:
            return _Rows([(name,) for name in
                          sorted(target.EXPECTED_CATALOG)])
        if "information_schema.columns" in lowered:
            table = params[0]
            return _Rows([(column,) for column in
                          sorted(target.EXPECTED_CATALOG[table])])
        if "from pg_constraint where conrelid" in lowered \
                and "contype" not in lowered:
            table = params[0].split(".", 1)[1]
            names = list(target.EXPECTED_CONSTRAINTS.get(table, ()))
            names.append(f"{table}_pkey")
            return _Rows([(name,) for name in names])
        if "contype = 'p'" in lowered:
            return _Rows([(1,)])
        if "from schema_meta" in lowered:
            return _Rows([(key, value) for key, value in
                          sorted(target.EXPECTED_SCHEMA_MARKERS.items())])
        if "rolreplication" in lowered:
            return _Rows([("marker_app", False, False, False, False, False,
                           False),
                          ("marker_worker", False, False, False, False,
                           False, False)])
        if "relforcerowsecurity" in lowered:
            return _Rows([(table, True, True) for table in
                          target.EXPECTED_RLS_FORCED_TABLES])
        if "has_schema_privilege" in lowered:
            return _Rows([(False if "create" in lowered else True,)])
        if "has_table_privilege" in lowered:
            role, table, verb = params
            if role not in target.EXPECTED_RUNTIME_ROLES:
                raise AssertionError(
                    "PUBLIC must never be passed to has_*_privilege")
            expected = (target.EXPECTED_EFFECTIVE_TABLE_PRIVS
                        .get(table, {}).get(role, ()))
            return _Rows([(verb in expected,)])
        if "pg_sequences" in lowered:
            return _Rows([])
        if "has_column_privilege" in lowered:
            if len(params) == 4:
                role, table, column, verb = params
            else:
                # The worker-membership probe binds only the column and
                # inlines marker_worker / tenant_membership / SELECT. Answer
                # it from the same migration-derived matrix (not a blind
                # False), so a wrong expected posture still fails.
                role, table, verb = "marker_worker", "tenant_membership", \
                    "SELECT"
                column = params[0]
                if ("has_column_privilege('marker_worker',"
                        " 'tenant_membership', %s, 'select')") not in lowered:
                    raise AssertionError(
                        "unexpected has_column_privilege query shape: "
                        f"{lowered[:100]}")
            expected = (target.EXPECTED_EFFECTIVE_COLUMN_PRIVS
                        .get(table, {}).get(verb, {}).get(role, ()))
            return _Rows([(column in expected,)])
        if "aclexplode" in lowered:
            return _Rows([])
        if "pg_auth_members" in lowered:
            return _Rows([(0,)])
        raise AssertionError(f"unexpected stage-2 query: {lowered[:100]}")


class Stage2IdentityBindingSourceTests(unittest.TestCase):
    """Offline SOURCE proof of the mandatory stage-2 identity binding.

    The always-false execution gate is patched only to exercise comparison
    LOGIC against a recording double; this is NOT a PostgreSQL proof. The
    tests prove: missing/invalid expectations refuse before any query; a
    matching identity plus matching ACTUAL schema_meta marker passes; wrong
    server/database/current-user/marker values surface diffs; and a missing
    actual marker refuses the initialized stage without creating anything.
    """

    def setUp(self):
        record, digest = target.synthetic_approved_manifest()
        self.approved = target.supply_approved_manifest(record, digest)
        self.run_id = self.approved.binding["run_id"]
        gate = mock.patch.object(target, "authorize_execution")
        gate.start()
        self.addCleanup(gate.stop)

    def _approval(self, **changes):
        values = dict(
            approved_manifest=self.approved,
            execution_authorized=True,
            target_dsn="postgresql://inert-target/db",
            runtime_login="marker_app_login",
            attested_server_address="127.0.0.1",
            attested_server_port=5432,
            attested_server_version_prefix="PostgreSQL",
            run_marker=self.run_id,
        )
        values.update(changes)
        return fixtures.SeedApproval(**values)

    def _refused_before_query(self, approval=_MISSING, approved=_MISSING,
                              reason=None):
        connection = _Stage2Double()
        approval = self._approval() if approval is _MISSING else approval
        approved = self.approved if approved is _MISSING else approved
        with self.assertRaises(target.TargetRefused) as caught:
            target.stage2_run_catalog_comparison(
                connection, approval, approved)
        if reason is not None:
            self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(connection.calls, [],
                         "mandatory validation must run BEFORE any query")
        return caught.exception

    def test_missing_or_invalid_identity_refuses_before_any_query(self):
        cases = (
            {"attested_server_address": ""},
            {"attested_server_address": "   "},
            {"attested_server_port": 0},
            {"attested_server_port": 70000},
            {"attested_server_port": True},
            {"attested_server_port": "5432"},
            {"attested_server_version_prefix": ""},
            {"runtime_login": ""},
            {"run_marker": ""},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self._refused_before_query(
                    approval=self._approval(**changes),
                    reason=target.TARGET_IDENTITY_INCOMPLETE)

    def test_input_marker_must_equal_the_manifest_run_identity(self):
        self._refused_before_query(
            approval=self._approval(run_marker="different-run"),
            reason=target.MANIFEST_MISMATCH)

    def test_missing_or_tampered_manifest_refuses_before_any_query(self):
        self._refused_before_query(approved=None,
                                   reason=target.NO_APPROVED_MANIFEST)
        tampered = target.ApprovedManifest(
            record=self.approved.record,
            binding=self.approved.binding,
            digest="0" * 64)
        self._refused_before_query(approved=tampered,
                                   reason=target.MANIFEST_MISMATCH)

    def test_approval_must_carry_the_approved_manifest(self):
        self._refused_before_query(
            approval=self._approval(approved_manifest=None),
            reason=target.NO_APPROVED_MANIFEST)

    def test_matching_identity_and_actual_marker_pass_the_logic(self):
        connection = _Stage2Double()
        report = target.stage2_run_catalog_comparison(
            connection, self._approval(), self.approved)
        self.assertEqual(report["diffs"], [], repr(report["diffs"]))
        self.assertTrue(report["passed"])
        self.assertIn("synthetic_run_marker", report["checked"])
        marker_calls = [call for call in connection.calls
                        if "schema_meta where key" in call[0]]
        self.assertEqual(len(marker_calls), 1,
                         "the ACTUAL marker is read from schema_meta, not "
                         "inferred from the agreeing input fields")

    def test_wrong_server_database_and_current_user_are_diffs(self):
        cases = (
            ({"address": "10.9.9.9"}, "address"),
            ({"database": "other_database"}, "database"),
            ({"port": 6543}, "port"),
            ({"version": "MariaDB 11"}, "version"),
            ({"session_user": "some_other_login"}, "session_user"),
            ({"current_user": "marker_admin_session"}, "current_user"),
        )
        for identity, label in cases:
            with self.subTest(identity=identity):
                connection = _Stage2Double(**identity)
                report = target.stage2_run_catalog_comparison(
                    connection, self._approval(), self.approved)
                diffs = [diff for diff in report["diffs"]
                         if diff["check"] == "attested_server_identity"]
                self.assertTrue(diffs, f"{label} mismatch must surface")
                self.assertFalse(report["passed"])

    def test_wrong_actual_marker_is_a_diff(self):
        connection = _Stage2Double(marker="another-run")
        report = target.stage2_run_catalog_comparison(
            connection, self._approval(), self.approved)
        marker_diffs = [diff for diff in report["diffs"]
                        if diff["check"] == "synthetic_run_marker"]
        self.assertEqual(len(marker_diffs), 2, repr(report["diffs"]))
        self.assertFalse(report["passed"])

    def test_missing_actual_marker_refuses_the_initialized_stage(self):
        connection = _Stage2Double(marker=_MISSING)
        with self.assertRaises(target.TargetRefused) as caught:
            target.stage2_run_catalog_comparison(
                connection, self._approval(), self.approved)
        self.assertEqual(caught.exception.reason,
                         target.TARGET_NOT_INITIALIZED)
        self.assertFalse([call for call in connection.calls
                          if "insert into schema_meta" in call[0]],
                         "stage 2 never creates a marker")


class _MarkerDouble:
    """Recording double for the stage-0 bootstrap and normal-seed marker
    checks; no live connection is ever touched."""

    def __init__(self, marker=_MISSING, schema_markers=None):
        self.calls = []
        self.marker = marker
        self.schema_markers = schema_markers or [
            ("schema_version", "1"), ("result_context_version", "1")]

    @contextlib.contextmanager
    def transaction(self):
        self.calls.append(("transaction", None))
        yield self

    def execute(self, sql, params=None):
        lowered = " ".join(sql.lower().split())
        self.calls.append((lowered, params))
        if "from schema_meta where key in" in lowered:
            return _Rows(self.schema_markers)
        if "from schema_meta where key" in lowered:
            return _Rows([] if self.marker is _MISSING
                         else [(self.marker,)])
        if "insert into schema_meta" in lowered:
            return _Rows([])
        if "insert into tenants" in lowered:
            return _Rows([])
        raise AssertionError(
            f"unexpected marker-operation query: {lowered[:100]}")


class BootstrapMarkerSourceTests(unittest.TestCase):
    """Offline SOURCE proof of the separate stage-0 bootstrap marker routine:
    it never runs automatically, needs its own authorization plus explicit
    non-production, verifies an exact existing marker, refuses another run's
    marker, and creates a missing marker only with a plain one-shot INSERT."""

    def setUp(self):
        record, digest = target.synthetic_approved_manifest()
        self.approved = target.supply_approved_manifest(record, digest)
        self.run_id = self.approved.binding["run_id"]
        gate = mock.patch.object(target, "authorize_execution")
        gate.start()
        self.addCleanup(gate.stop)

    def _approval(self, **changes):
        values = dict(
            approved_manifest=self.approved,
            bootstrap_authorized=True,
            target_dsn="postgresql://inert-target/db",
            run_marker=self.run_id,
        )
        values.update(changes)
        return fixtures.BootstrapApproval(**values)

    def test_requires_separate_authorization_and_exact_non_production(self):
        cases = (
            (self._approval(bootstrap_authorized=False), False),
            (self._approval(), True),
        )
        for approval, is_production in cases:
            with self.subTest(is_production=is_production):
                connection = _MarkerDouble()
                with self.assertRaises(target.TargetRefused) as caught:
                    fixtures.bootstrap_run_marker(
                        connection, approval, is_production=is_production)
                self.assertEqual(caught.exception.reason,
                                 target.EXECUTION_NOT_AUTHORIZED)
                self.assertEqual(connection.calls, [])

    def test_creates_a_missing_marker_once_with_plain_insert(self):
        connection = _MarkerDouble()
        result = fixtures.bootstrap_run_marker(
            connection, self._approval(), is_production=False)
        self.assertEqual(result["action"], "created")
        inserts = [call for call in connection.calls
                   if "insert into schema_meta" in call[0]]
        self.assertEqual(len(inserts), 1)
        self.assertEqual(inserts[0][1],
                         (target.STAGE2_RUN_MARKER_KEY, self.run_id))
        self.assertFalse(any("on conflict" in call[0]
                             for call in connection.calls))

    def test_verifies_an_exact_existing_marker_without_writing(self):
        connection = _MarkerDouble(marker=self.run_id)
        result = fixtures.bootstrap_run_marker(
            connection, self._approval(), is_production=False)
        self.assertEqual(result["action"], "verified-existing")
        self.assertFalse([call for call in connection.calls
                          if "insert into schema_meta" in call[0]])

    def test_refuses_another_runs_marker_without_overwrite(self):
        connection = _MarkerDouble(marker="another-run")
        with self.assertRaises(target.TargetRefused) as caught:
            fixtures.bootstrap_run_marker(
                connection, self._approval(), is_production=False)
        self.assertEqual(caught.exception.reason,
                         target.INCONSISTENT_TARGET)
        self.assertFalse([call for call in connection.calls
                          if "insert into schema_meta" in call[0]])


class SeedMarkerSourceTests(unittest.TestCase):
    """Normal seeding never creates a marker and refuses a missing or
    another-run marker before any fixture INSERT."""

    def setUp(self):
        record, digest = target.synthetic_approved_manifest()
        self.approved = target.supply_approved_manifest(record, digest)
        self.run_id = self.approved.binding["run_id"]
        self.bundle = fixtures.build_bundle()
        self.approval = fixtures.SeedApproval(
            approved_manifest=self.approved,
            execution_authorized=True,
            target_dsn="postgresql://inert-target/db",
            run_marker=self.run_id)
        gate = mock.patch.object(target, "authorize_execution")
        gate.start()
        self.addCleanup(gate.stop)

    def test_refuses_a_missing_marker_without_creating_it(self):
        connection = _MarkerDouble(marker=_MISSING)
        with self.assertRaises(target.TargetRefused) as caught:
            fixtures.seed(connection, self.bundle, self.approval,
                          is_production=False)
        self.assertEqual(caught.exception.reason,
                         target.TARGET_NOT_INITIALIZED)
        self.assertFalse([call for call in connection.calls
                          if "insert into" in call[0]])

    def test_refuses_another_runs_marker(self):
        connection = _MarkerDouble(marker="another-run")
        with self.assertRaises(target.TargetRefused) as caught:
            fixtures.seed(connection, self.bundle, self.approval,
                          is_production=False)
        self.assertEqual(caught.exception.reason,
                         target.INCONSISTENT_TARGET)
        self.assertFalse([call for call in connection.calls
                          if "insert into" in call[0]])


class RunnerBehaviorTests(unittest.TestCase):
    """Behavioral runner contract: live refusal precedes every setup step,
    and the runner reads no environment."""

    def test_live_mode_refuses_with_exit_2(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = runner.main(["--mode", "live"])
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED", stderr.getvalue())
        self.assertIn("never unlock", stderr.getvalue())

    def test_live_refusal_precedes_block_installation_and_imports(self):
        with mock.patch.object(runner, "install_connection_blocks") as install, \
                mock.patch.object(runner, "verify_blocks") as verify:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                rc = runner.main(["--mode", "live"])
        self.assertEqual(rc, 2)
        install.assert_not_called()
        verify.assert_not_called()

    def test_runner_reads_no_dsn_or_credential_environment(self):
        """The runner may not be able to stop stdlib internals (argparse,
        locale, stream colorization) from peeking at locale/terminal
        variables, but it must never touch any DSN, key, secret, or token
        variable - so no environment content can unlock anything."""
        spy = _RecordingMapping()
        with mock.patch.object(os, "environ", spy):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                rc = runner.main(["--mode", "live"])
        self.assertEqual(rc, 2)
        for key in spy.reads:
            with self.subTest(key=key):
                lowered = key.lower()
                self.assertNotIn("dsn", lowered)
                self.assertNotIn("key", lowered)
                self.assertNotIn("secret", lowered)
                self.assertNotIn("token", lowered)
                self.assertFalse(lowered.startswith("ai_marker"))

    def test_exact_module_contract(self):
        self.assertEqual(
            runner.IMPORT_GATE_MODULES,
            ("br02_pg_target", "br02_pg_fixtures", "br02_pg_api_child"))
        self.assertEqual(
            runner.TEST_MODULES,
            ("tests.test_br02_pg_preflight",
             "tests.test_postgres_result_store_contract",
             "tests.test_postgres_api_pg_contract",
             "tests.test_br02_pg_registration",
             "tests.test_br02_pg_api",
             "tests.test_br02_pg_transactions",
             "tests.test_br02_pg_concurrency",
             "tests.test_br02_pg_processes",
             "tests.test_br02_pg_rls_grants",
             "tests.test_br02_pg_worker_denial",
             "tests.test_br02_pg_rollback"))


class FocusedCorrectionRegressionTests(unittest.TestCase):
    """Exercise corrected live-test logic with inert doubles, never a PG target.

    These direct method calls do not run live setup or authorize execution.
    All I/O boundaries are replaced; the runner's blocks remain installed.
    """

    def test_retry_uses_raw_subject_and_preserves_receipt_checks(self):
        from tests import test_br02_pg_transactions as tx
        case = tx.FaultStageTransactionTests()
        case.bundle = fixtures.build_bundle()
        case.register_original = mock.Mock()
        faulted, retried, fresh = mock.Mock(), mock.Mock(), mock.Mock()
        faulted.commit_correction.side_effect = tx.audit_store.AuditStoreError(
            "injected", "synthetic fault")
        retried.commit_correction.return_value = {"status": "committed"}
        fresh.snapshot.return_value = {"result_revision": 1, "events": [{}]}
        case.store = mock.Mock(side_effect=[faulted, retried, fresh])
        case.test_fault_after_event_insert_rolls_back_the_event()
        self.assertEqual(retried.commit_correction.call_args.kwargs["owner"],
                         case.bundle.raw_subject)
        self.assertNotEqual(case.bundle.raw_subject, case.bundle.canonical_owner)
        for store in (faulted, retried, fresh):
            store.close.assert_called_once()

    def test_both_live_contracts_resolve_explicit_fixture_helpers(self):
        from tests import test_postgres_api_pg_contract as api
        from tests import test_postgres_result_store_contract as result
        for module, cls in (
                (api, api.PostgresStoreAdapterLiveContractTests),
                (result, result.PostgresResultContextContractTests)):
            with self.subTest(module=module.__name__):
                case = cls()
                case.bundle = fixtures.build_bundle()
                case.dsn = "synthetic-not-a-dsn"
                with mock.patch.object(module.postgres_result_store,
                                       "open_store") as opened:
                    case.fixture_store()
                    args = opened.call_args
                    self.assertEqual(args.args, (case.dsn,))
                    self.assertIs(args.kwargs["fresh_completion_denied"], False)
                    identity = args.kwargs["identity"]
                    self.assertEqual(identity.membership_subject,
                                     case.bundle.raw_subject)
                    self.assertEqual(identity.tenant_id, case.bundle.tenant_id)
                    opened.reset_mock()
                    case.store_for(case.bundle.raw_subject, case.bundle.tenant_id)
                    self.assertNotIn("fresh_completion_denied",
                                     opened.call_args.kwargs)

    def test_production_still_refuses_explicit_fixture_flag(self):
        import postgres_result_store as store_module
        store = object.__new__(store_module.PostgresResultStore)
        store.fresh_completion_denied = False
        with mock.patch.object(store_module.runtime_config,
                               "is_production_runtime", return_value=True):
            with self.assertRaises(store_module.AuditStoreError) as caught:
                store.register_with_context(
                    result_id="synthetic-result", original_output={},
                    scope="manual", review_key="synthetic-review")
        self.assertEqual(caught.exception.code, "store_unusable")

    def test_api_cross_tenant_read_uses_other_runtime_identity(self):
        from tests import test_postgres_api_pg_contract as api
        case = api.PostgresStoreAdapterLiveContractTests()
        case.bundle = fixtures.build_bundle()
        own, other = mock.Mock(), mock.Mock()
        case.fixture_store = mock.Mock(return_value=own)
        case.store_for = mock.Mock(return_value=other)
        other.visible_row.return_value = None
        other.snapshot.side_effect = api.audit_store.AuditStoreError(
            "result_not_registered", "synthetic absent result")
        case.test_cross_tenant_read_is_indistinguishable_from_absent()
        case.fixture_store.assert_called_once()
        case.store_for.assert_called_once_with(
            case.bundle.other_subject, case.bundle.other_tenant_id)
        own.register_with_context.assert_called_once()
        other.register_with_context.assert_not_called()
        own.close.assert_called_once()
        other.close.assert_called_once()

    def test_rollback_fixture_registration_uses_fixture_store(self):
        from types import SimpleNamespace
        from tests import test_postgres_result_store_contract as result
        case = result.PostgresResultContextContractTests()
        case.bundle = fixtures.build_bundle()
        case.dsn = "synthetic-not-a-dsn"
        store, probe = mock.Mock(), mock.MagicMock()
        case.fixture_store = mock.Mock(return_value=store)
        case.store_for = mock.Mock(side_effect=AssertionError("runtime path"))
        failure = type("SyntheticRefusal", (Exception,), {})
        probe.__enter__.return_value.execute.side_effect = failure()
        pg = SimpleNamespace(connect=mock.Mock(return_value=probe),
                             errors=SimpleNamespace(RaiseException=failure))
        with mock.patch.object(result, "psycopg", pg):
            case.test_rollback_draft_refuses_while_originals_exist()
        case.fixture_store.assert_called_once()
        store.register_with_context.assert_called_once()
        store.close.assert_called_once()

    def _changed_content_race(self, winner, bad_receipt=False, bad_stored=False,
                              worker_error=False, still_alive=False):
        from tests import test_br02_pg_concurrency as race
        case = race.ConcurrencyOverlapTests()
        case.bundle = fixtures.build_bundle()
        changed = {"questions": [{"question_number": "1",
                                  "awarded_marks": 4, "max_marks": 3}]}
        inputs = [case.bundle.original_output, changed]
        digest = race.audit_store.digest_value(inputs[winner])
        store = mock.Mock()
        calls = []

        def register(**kwargs):
            calls.append(kwargs["original_output"])
            if worker_error:
                raise RuntimeError("synthetic worker failure")
            if len(calls) == 1:
                return {"original_digest": "wrong" if bad_receipt else digest}
            raise race.audit_store.AuditStoreError(
                "original_output_immutable", "synthetic losing contender")

        store.register_with_context.side_effect = register
        case.new_store = mock.Mock(return_value=store)
        case.conn = mock.Mock()
        case.conn.execute.return_value.fetchone.return_value = (
            "wrong" if bad_stored else digest,)
        pending = []

        class DeterministicThread:
            def __init__(self, target, args, **kwargs):
                self.target, self.args = target, args
                pending.append(self)

            def start(self):
                pass

            def join(self, timeout):
                if pending:
                    ordered = sorted(pending, key=lambda t: t.args[0] != winner)
                    pending.clear()
                    for thread in ordered:
                        thread.target(*thread.args)

            def is_alive(self):
                return still_alive

        with mock.patch.object(race.threading, "Thread", DeterministicThread), \
                mock.patch.object(race.threading, "Barrier"):
            case.test_concurrent_changed_content_exactly_one_winner()

    def test_changed_content_race_accepts_either_identified_winner(self):
        for winner in (0, 1):
            with self.subTest(winner=winner):
                self._changed_content_race(winner)

    def test_changed_content_race_rejects_mismatched_receipt_or_storage(self):
        for winner in (0, 1):
            for field in ("bad_receipt", "bad_stored"):
                with self.subTest(winner=winner, field=field):
                    with self.assertRaises(AssertionError):
                        self._changed_content_race(winner, **{field: True})

    def test_changed_content_race_surfaces_worker_failure_and_timeout(self):
        for field in ("worker_error", "still_alive"):
            with self.subTest(field=field):
                with self.assertRaises(AssertionError):
                    self._changed_content_race(0, **{field: True})

    def _rollback_case(self):
        from types import SimpleNamespace
        from tests import test_br02_pg_rollback as rollback
        case = rollback.RollbackRefusalTests()
        case.bundle = fixtures.build_bundle()
        case.dsn = "synthetic-not-a-dsn"
        case.register_original = mock.Mock()
        case.effective_role_is_elevated = mock.Mock(return_value=True)
        failure = type("SyntheticPgError", (Exception,), {})
        pg = SimpleNamespace(errors=SimpleNamespace(
            Error=failure, LockNotAvailable=failure))
        return rollback, case, failure, pg

    def test_rollback_refusals_recover_before_verification_or_role_reset(self):
        from contextlib import contextmanager

        for method in ("test_privileged_populated_refusal_preserves_everything",
                       "test_unauthorized_role_refused_even_on_empty_context"):
            with self.subTest(method=method):
                module, case, failure, pg = self._rollback_case()
                aborted, order = [False], []
                case.conn = mock.Mock()
                active, snapshots = [False], []

                @contextmanager
                def transaction():
                    self.assertFalse(aborted[0], "snapshot before recovery")
                    self.assertFalse(active[0], "nested snapshot transaction")
                    phase = "after" if snapshots else "before"
                    if phase == "after":
                        self.assertEqual(order[-1], "recovery")
                    snapshots.append(phase)
                    order.append(phase + " snapshot")
                    active[0] = True
                    try:
                        yield
                    finally:
                        active[0] = False

                def execute(sql, params=None):
                    self.assertFalse(aborted[0], "use of aborted transaction")
                    if active[0]:
                        if sql == "set local statement_timeout = '2s'":
                            return mock.Mock()
                        if "to_regclass" in sql:
                            return mock.Mock(fetchone=lambda: (True,))
                        if "schema_meta" in sql:
                            return mock.Mock(fetchall=lambda: [
                                ('result_context_version', '1')])
                        if "to_jsonb" in sql:
                            self.assertIn(
                                "order by tenant_id, membership_subject, result_id",
                                sql)
                            return mock.Mock(fetchall=lambda: [
                                ('retained-row',), ('current-row',)])
                        if "count(*)" in sql:
                            self.assertEqual(params, (case.bundle.tenant_id,
                                                     case.bundle.raw_subject,
                                                     case.bundle.result_id))
                            return mock.Mock(fetchone=lambda: (1,))
                        self.fail("unexpected snapshot query")
                    if "BEGIN;" in sql or "begin;" in sql:
                        aborted[0] = True
                        raise failure("rollback refused: result_context is populated")
                    if "to_regclass" in sql or "to_jsonb" in sql:
                        self.fail("snapshot outside owned transaction")
                    order.append(sql)
                    return mock.Mock(fetchone=lambda: (0,))

                def recover():
                    order.append("recovery")
                    aborted[0] = False

                def state():
                    self.assertFalse(aborted[0])
                    order.append("state")
                    return True, 1, 1

                case.conn.execute.side_effect = execute
                case.conn.rollback.side_effect = recover
                case.conn.transaction.side_effect = transaction
                case.context_state = state
                with mock.patch.object(module, "psycopg", pg):
                    getattr(case, method)()
                if "unauthorized" in method:
                    self.assertLess(order.index("recovery"), order.index("reset role"))
                    self.assertEqual(snapshots, [])
                else:
                    self.assertEqual(snapshots, ["before", "after"])
                    self.assertLess(order.index("before snapshot"),
                                    order.index("recovery"))
                    self.assertLess(order.index("recovery"),
                                    order.index("after snapshot"))
                self.assertFalse(active[0])
                self.assertFalse(aborted[0])
                self.assertLess(order.index("recovery"), len(order) - 1)

    def test_rollback_lock_cleanup_precedes_readback_even_on_failure(self):
        from contextlib import contextmanager

        for setup_failure in (False, True):
            with self.subTest(setup_failure=setup_failure):
                module, case, failure, pg = self._rollback_case()
                order = []
                blocker, candidate = mock.Mock(), mock.Mock()
                blocker.rollback.side_effect = lambda: order.append("blocker rollback")
                blocker.close.side_effect = lambda: order.append("blocker close")
                candidate.rollback.side_effect = lambda: order.append("candidate rollback")
                candidate.close.side_effect = lambda: order.append("candidate close")

                def execute(sql):
                    order.append(sql)
                    if setup_failure or "result_context is populated" in sql:
                        raise failure("synthetic lock timeout")

                candidate.execute.side_effect = execute
                connections = iter((blocker, candidate))

                def connect(*args, **kwargs):
                    self.assertIn("before snapshot", order)
                    connection = next(connections)
                    order.append("connect blocker" if connection is blocker
                                 else "connect candidate")
                    return connection

                pg.connect = mock.Mock(side_effect=connect)
                snapshots, active = [], [False]
                case.conn = mock.Mock()

                @contextmanager
                def snapshot_transaction():
                    self.assertFalse(active[0])
                    phase = "after" if snapshots else "before"
                    if phase == "after":
                        self.assertEqual(order[-2:],
                                         ["blocker rollback", "blocker close"])
                    else:
                        self.assertEqual(order, [])
                    snapshots.append(phase)
                    order.append(phase + " snapshot")
                    active[0] = True
                    try:
                        yield
                    finally:
                        active[0] = False

                def snapshot_execute(sql, params=None):
                    self.assertTrue(active[0], "read outside snapshot transaction")
                    if "statement_timeout" in sql:
                        self.assertEqual(sql, "set local statement_timeout = '2s'")
                        return mock.Mock()
                    if "to_regclass" in sql:
                        return mock.Mock(fetchone=lambda: (True,))
                    if "schema_meta" in sql:
                        return mock.Mock(fetchall=lambda: [
                            ("result_context_version", "1")])
                    if "to_jsonb" in sql:
                        return mock.Mock(fetchall=lambda: [
                            ("retained-row",), ("current-fixture-row",)])
                    if "count(*)" in sql:
                        self.assertEqual(params, (case.bundle.tenant_id,
                                                 case.bundle.raw_subject,
                                                 case.bundle.result_id))
                        return mock.Mock(fetchone=lambda: (1,))
                    raise AssertionError(f"unexpected snapshot query: {sql}")

                case.conn.transaction.side_effect = snapshot_transaction
                case.conn.execute.side_effect = snapshot_execute
                with mock.patch.object(module, "psycopg", pg):
                    if setup_failure:
                        with self.assertRaises(failure):
                            case.test_lock_writer_blocks_the_draft_atomically()
                    else:
                        case.test_lock_writer_blocks_the_draft_atomically()
                        self.assertLess(order.index("begin"),
                                        order.index("set local lock_timeout = '100ms'"))
                self.assertIn("candidate rollback", order)
                self.assertIn("candidate close", order)
                self.assertLess(order.index("candidate close"),
                                order.index("blocker rollback"))
                self.assertEqual(snapshots, ["before"] if setup_failure
                                 else ["before", "after"])
                self.assertFalse(active[0])
                self.assertLess(order.index("before snapshot"),
                                order.index("connect blocker"))

    def test_rollback_state_readback_has_local_statement_timeout(self):
        from tests import test_br02_pg_rollback as module
        case = module.RollbackRefusalTests()
        case.conn = mock.MagicMock()
        case.conn.execute.return_value.fetchone.return_value = (1,)
        case.context_state()
        case.conn.transaction.assert_called_once()
        self.assertEqual(case.conn.execute.call_args_list[0].args,
                         ("set local statement_timeout = '2s'",))

    def test_anchor_digest_hashes_decoded_json_and_rejects_mismatches(self):
        from tests import test_br02_pg_registration as registration
        for mismatch in (None, "context", "digest"):
            with self.subTest(mismatch=mismatch):
                case = registration.RegistrationBothOrNothingTests()
                case.bundle = fixtures.build_bundle()
                original = case.bundle.original_output
                digest = registration.audit_store.digest_value(original)
                self.assertEqual(len(digest), 64)
                with self.assertRaises(ValueError):
                    registration.audit_store.json.loads(digest)
                content = {"changed": True} if mismatch == "context" else original
                anchor = "f" * 64 if mismatch == "digest" else digest
                case.conn = mock.Mock()
                case.conn.execute.side_effect = [
                    mock.Mock(fetchone=lambda: (anchor, 0)),
                    mock.Mock(fetchone=lambda: (
                        registration.audit_store.json.dumps(content),
                        case.bundle.job_id, case.bundle.assignment_id))]
                store = mock.Mock()
                store.register_with_context.return_value = {
                    "created": True, "result_revision": 0}
                case.fixture_store = mock.Mock(return_value=store)
                if mismatch:
                    with self.assertRaises(AssertionError):
                        case.test_anchor_and_context_are_written_together()
                else:
                    case.test_anchor_and_context_are_written_together()


class Stage2AuthorizationGateTests(unittest.TestCase):
    """Actual validator/dispatch with inert authority and connection doubles.

    No receipt is persisted. Patching the unavailable authority here is only
    an offline fixture; no runtime activation API is installed.
    """

    def setUp(self):
        import br02_pg_authorization as authorization
        from dataclasses import replace

        self.auth, self.replace = authorization, replace
        self.clock = mock.patch.object(authorization.time, "time", return_value=1000)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        monotonic = mock.patch.object(authorization.time, "monotonic", return_value=1000)
        monotonic.start()
        self.addCleanup(monotonic.stop)
        self.order = []
        binding = authorization.Binding(
            "synthetic-instance", "synthetic-object-storage", "127.0.0.1:5432",
            "127.0.0.1", 5432, "synthetic_disposable", "marker_app_login",
            "synthetic-run-0001", "PostgreSQL 17.0 (synthetic)")
        inventory = authorization._artifact_inventory()
        digest = authorization.artifact_digest(inventory)
        record = target.synthetic_manifest_record()
        record.update(phase=authorization.OPERATION,
                      role_inventory=[binding.restricted_login], artifact_hash=digest)
        manifest_digest = target.manifest_digest(record)
        import json
        self.reviewed = authorization.ReviewedTarget(
            "synthetic-independent-review", json.dumps(record), manifest_digest, binding)
        self.receipt = authorization.Receipt(
            "synthetic-once", "synthetic-owner-approval", self.reviewed.review_ref,
            manifest_digest, digest, inventory, binding, authorization.OPERATION,
            990, 1100, 10, 1)
        self.connection = _Stage2Double(version=binding.server_version)
        self.connection.close = mock.Mock(side_effect=lambda: self.order.append("close"))
        original_execute = self.connection.execute
        self.probe = (binding.address, binding.port, binding.server_version,
                      binding.database, binding.restricted_login,
                      binding.restricted_login, "on", "1s", False, "pg_catalog, public")

        def execute(sql, params=None):
            self.assertIn("consume", self.order)
            self.assertIn("open", self.order)
            self.order.append("query")
            if sql == authorization._IDENTITY_PROBE:
                self.connection.calls.append((sql, params))
                return _Rows([self.probe])
            return original_execute(sql, params)

        self.connection.execute = mock.Mock(side_effect=execute)
        self.authority = mock.Mock(spec=authorization.TrustedAuthority)
        self.authority.lookup.side_effect = lambda ref: self.receipt
        self.authority.reviewed_target.side_effect = lambda ref: self.reviewed
        self.used = set()
        import threading
        consumption_lock = threading.Lock()

        def consume(receipt, evidence):
            self.order.append("consume")
            self.assertEqual(receipt, self.receipt)
            self.assertEqual(len(evidence), 64)
            with consumption_lock:
                if receipt.authorization_id in self.used:
                    return False
                self.used.add(receipt.authorization_id)
                return True

        self.authority.consume.side_effect = consume

        def open_connection(permit):
            self.assertEqual(self.order[-1], "consume")
            self.assertEqual(permit.binding, self.receipt.binding)
            self.assertEqual(permit.operation, authorization.OPERATION)
            self.assertEqual(permit.deadline, 1010)
            self.order.append("open")
            return self.connection

        self.authority.open_connection.side_effect = open_connection

    def _run(self):
        with mock.patch.object(self.auth, "_trusted_authority",
                               return_value=self.authority):
            return target.stage2_authorized_readonly("synthetic-reference")

    def _preconnection_denied(self):
        with self.assertRaises(self.auth.AuthorizationDenied):
            self._run()
        self.authority.consume.assert_not_called()
        self.authority.open_connection.assert_not_called()
        self.connection.execute.assert_not_called()

    def test_actual_dispatch_consumes_once_before_open_and_closes(self):
        report = self._run()
        self.assertTrue(report["passed"], report["diffs"])
        self.assertEqual(self.order[:3], ["consume", "open", "query"])
        self.assertEqual(self.order[-1], "close")
        self.assertFalse(target.execution_authorized())
        self.assertIsNone(self.auth._dispatch.get())

    def test_real_authority_unavailable_before_evidence_artifact_or_connection_io(self):
        with mock.patch.object(self.auth, "_artifact_inventory") as inventory:
            for value in ("synthetic-reference", self.receipt, True, None):
                with self.subTest(value=type(value).__name__):
                    with self.assertRaisesRegex(self.auth.AuthorizationDenied,
                                                "^authority_unavailable$"):
                        target.stage2_authorized_readonly(value)
            inventory.assert_not_called()
        self.authority.open_connection.assert_not_called()

    def test_missing_wrong_type_and_owner_review_references_refuse(self):
        original = self.receipt
        for value in (None, True, {}, self.replace(original, owner_approval_ref=""),
                      self.replace(original, target_review_ref=""),
                      self.replace(original, authorization_id="")):
            with self.subTest(value=type(value).__name__):
                self.receipt = value
                self._preconnection_denied()

    def test_target_or_review_substitution_refuses(self):
        original = self.reviewed
        for field, value in (("instance", "substituted-instance"),
                             ("storage", "substituted-storage"),
                             ("database", "other-db"), ("restricted_login", "other"),
                             ("run_id", "other-run"), ("address", "127.0.0.2"),
                             ("port", 6543), ("server_version", "PostgreSQL 16.1")):
            with self.subTest(field=field):
                self.reviewed = self.replace(
                    original, binding=self.replace(original.binding, **{field: value}))
                self._preconnection_denied()
        self.reviewed = self.replace(original, review_ref="substituted-review")
        self._preconnection_denied()

    def test_agreeing_receipt_and_review_cannot_override_manifest_binding(self):
        original_receipt, original_review = self.receipt, self.reviewed
        for field, value in (("instance", "other"), ("storage", "other"),
                             ("database", "other"), ("restricted_login", "other"),
                             ("run_id", "other"), ("endpoint", "127.0.0.1:6543")):
            with self.subTest(field=field):
                binding = self.replace(original_receipt.binding, **{field: value})
                self.receipt = self.replace(original_receipt, binding=binding)
                self.reviewed = self.replace(original_review, binding=binding)
                self._preconnection_denied()

    def test_expired_future_revoked_and_invalid_attempt_bounds_refuse(self):
        original = self.receipt
        for changes in ({"expires_at": 1000}, {"not_before": 1001},
                        {"revoked": True}, {"max_attempts": 2}, {"max_attempts": True},
                        {"max_seconds": 0}, {"max_seconds": 61},
                        {"expires_at": 2000}, {"not_before": True}):
            with self.subTest(changes=changes):
                self.receipt = self.replace(original, **changes)
                self._preconnection_denied()

    def test_other_operations_are_never_accepted(self):
        original = self.receipt
        for operation in ("*", "seed", "bootstrap", "migration", "rollback",
                          "worker", "api", "live", "br02.stage2.identity_catalog"):
            with self.subTest(operation=operation):
                self.receipt = self.replace(original, operation=operation)
                self._preconnection_denied()
        with self.assertRaises(target.TargetRefused):
            target.authorize_execution(original)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(runner.main(["--mode", "live"]), 2)

    def test_manifest_digest_or_record_substitution_refuses(self):
        original = self.reviewed
        for changes in ({"manifest_digest": "0" * 64},
                        {"manifest_json": original.manifest_json.replace(
                            'synthetic_disposable', 'substituted_database')},
                        {"manifest_json": "{}"}):
            with self.subTest(changes=list(changes)):
                self.reviewed = self.replace(original, **changes)
                self._preconnection_denied()

    def test_incomplete_extra_duplicate_reordered_and_wrong_artifact_entries_refuse(self):
        original = self.receipt
        entries = original.artifacts
        for artifacts in (entries[:-1], entries + (("extra.py", "0" * 64),),
                          entries + entries[:1], tuple(reversed(entries)),
                          ((entries[0][0], "0" * 64),) + entries[1:]):
            with self.subTest(artifacts=artifacts):
                self.receipt = self.replace(original, artifacts=artifacts)
                self._preconnection_denied()
        self.receipt = self.replace(original, artifact_digest="0" * 64)
        self._preconnection_denied()

    def test_actual_dependency_set_is_hashed_and_dynamic_or_unknown_imports_deny(self):
        self.assertEqual(tuple(p for p, _ in self.receipt.artifacts),
                         tuple(sorted(self.auth.ENTRY_ARTIFACTS)))
        source = self.auth._source
        for addition in (b"\nimport unreviewed_dependency\n",
                         b"\n__import__('audit_store')\n",
                         b"\nfrom . import other\n"):
            def altered(path):
                data = source(path)
                return data + addition if path.endswith('br02_pg_target.py') else data
            with self.subTest(addition=addition):
                with mock.patch.object(self.auth, "_source", side_effect=altered):
                    self._preconnection_denied()

    def test_denied_or_uncertain_atomic_consumption_never_opens(self):
        for effect in (False, None, RuntimeError("private authority detail")):
            with self.subTest(effect=type(effect).__name__):
                self.authority.consume.side_effect = effect if isinstance(effect, Exception) \
                    else lambda *_args, value=effect: value
                with self.assertRaises(self.auth.AuthorizationDenied) as caught:
                    self._run()
                self.assertNotIn("private authority detail", str(caught.exception))
                self.authority.open_connection.assert_not_called()

    def test_replay_refuses_after_success_and_after_connection_failure(self):
        self._run()
        self.authority.open_connection.reset_mock()
        with self.assertRaisesRegex(self.auth.AuthorizationDenied, "consumption_refused"):
            self._run()
        self.authority.open_connection.assert_not_called()
        self.used.clear()
        self.authority.open_connection.side_effect = RuntimeError("private DSN")
        with self.assertRaises(self.auth.AuthorizationDenied):
            self._run()
        self.authority.open_connection.reset_mock()
        with self.assertRaisesRegex(self.auth.AuthorizationDenied, "consumption_refused"):
            self._run()
        self.authority.open_connection.assert_not_called()

    def test_source_change_or_expiry_after_consumption_never_opens(self):
        inventory = self.auth._artifact_inventory
        with mock.patch.object(self.auth, "_artifact_inventory",
                               side_effect=[inventory(), ()]):
            with self.assertRaises(self.auth.AuthorizationDenied):
                self._run()
        self.authority.open_connection.assert_not_called()
        self.used.clear()
        def consume(*_args):
            self.order.append("consume")
            self.auth.time.monotonic.return_value = 1011
            return True
        self.authority.consume.side_effect = consume
        with self.assertRaises(self.auth.AuthorizationDenied):
            self._run()
        self.authority.open_connection.assert_not_called()

    def test_connected_identity_privilege_or_timeout_mismatch_closes_before_catalog(self):
        original = self.probe
        for index, value in ((0, "10.0.0.1"), (1, 6543), (2, "PostgreSQL 16.1"),
                             (3, "other-db"), (4, "admin"), (5, "marker_app"),
                             (6, "off"), (7, "0"), (7, "11s"), (8, True),
                             (9, "public")):
            with self.subTest(index=index, value=value):
                self.used.clear()
                self.order.clear()
                self.connection.calls.clear()
                self.probe = original[:index] + (value,) + original[index + 1:]
                with self.assertRaises(self.auth.AuthorizationDenied):
                    self._run()
                self.assertEqual(len(self.connection.calls), 1)
                self.assertEqual(self.order[-1], "close")

    def test_run_marker_mismatch_closes_without_catalog_reads(self):
        self.connection.identity["marker"] = "substituted-run"
        report = self._run()
        self.assertFalse(report["passed"])
        self.assertFalse(any("pg_class" in sql for sql, _ in self.connection.calls))
        self.assertEqual(self.order[-1], "close")

    def test_concurrent_dispatch_has_one_atomic_consumer_and_one_open(self):
        import threading
        barrier = threading.Barrier(2)
        consume = self.authority.consume.side_effect
        outcomes = []

        def concurrent_consume(*args):
            barrier.wait(timeout=2)
            return consume(*args)

        self.authority.consume.side_effect = concurrent_consume

        def invoke():
            try:
                outcomes.append(target.stage2_authorized_readonly('synthetic-reference'))
            except self.auth.AuthorizationDenied as error:
                outcomes.append(error)

        with mock.patch.object(self.auth, '_trusted_authority', return_value=self.authority):
            threads = [threading.Thread(target=invoke) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=4)
                self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(sum(isinstance(result, dict) and result['passed']
                             for result in outcomes), 1)
        self.assertEqual(sum(isinstance(result, self.auth.AuthorizationDenied)
                             for result in outcomes), 1)
        self.authority.open_connection.assert_called_once()
        self.connection.close.assert_called_once()
        self.assertIsNone(self.auth._dispatch.get())

    def test_new_transitive_source_dependency_cannot_be_omitted(self):
        source, is_file = self.auth._source, Path.is_file

        def additional_source(path):
            if path == 'gate_extra.py':
                return b'import math\n'
            data = source(path)
            return data + b'\nimport gate_extra\n' \
                if path.endswith('br02_pg_target.py') else data

        def exists(path):
            return True if path == self.auth.ROOT / 'gate_extra.py' else is_file(path)

        with mock.patch.object(self.auth, '_source', side_effect=additional_source), \
                mock.patch.object(Path, 'is_file', new=exists):
            inventory = self.auth._artifact_inventory()
            self.assertIn('gate_extra.py', dict(inventory))
            self._preconnection_denied()

    def test_artifact_symlinks_refuse_without_reading(self):
        with mock.patch.object(Path, 'is_symlink', return_value=True), \
                mock.patch.object(Path, 'read_bytes') as read:
            with self.assertRaises(self.auth.AuthorizationDenied):
                self.auth._artifact_inventory()
            read.assert_not_called()

    def test_monotonic_deadline_and_expiry_are_checked_during_queries(self):
        original_execute = self.connection.execute.side_effect
        def advance_clock(sql, params=None):
            result = original_execute(sql, params)
            self.auth.time.monotonic.return_value = 1011
            return result
        self.connection.execute.side_effect = advance_clock
        with self.assertRaisesRegex(self.auth.AuthorizationDenied, '^deadline$'):
            self._run()
        self.assertEqual(len(self.connection.calls), 1)
        self.assertEqual(self.order[-1], 'close')

    def test_authority_errors_and_cleanup_errors_are_sanitized(self):
        self.authority.lookup.side_effect = self.auth.AuthorizationDenied('private approval')
        with self.assertRaises(self.auth.AuthorizationDenied) as caught:
            self._run()
        self.assertNotIn('private approval', str(caught.exception))
        self.authority.lookup.side_effect = lambda _ref: self.receipt
        self.connection.close.side_effect = RuntimeError('private credential')
        with self.assertRaisesRegex(self.auth.AuthorizationDenied,
                                    '^connection_cleanup_failure$'):
            self._run()

    def test_dispatch_permit_cannot_be_substituted_or_reentered(self):
        connection, approval, manifest = object(), object(), object()
        token = self.auth._dispatch.set((connection, approval, manifest, 1010))
        try:
            with self.assertRaisesRegex(self.auth.AuthorizationDenied, 'dispatch_substitution'):
                self.auth._claim_stage2_dispatch(object(), approval, manifest)
            self.assertTrue(self.auth._claim_stage2_dispatch(connection, approval, manifest))
            self.assertFalse(self.auth._claim_stage2_dispatch(connection, approval, manifest))
        finally:
            self.auth._dispatch.reset(token)

    def test_query_failure_closes_and_does_not_leak_or_reuse_dispatch(self):
        self.connection.execute.side_effect = RuntimeError("private credential detail")
        with self.assertRaisesRegex(self.auth.AuthorizationDenied,
                                    "^evidence_or_dispatch_failure$"):
            self._run()
        self.connection.close.assert_called_once_with()
        self.assertIsNone(self.auth._dispatch.get())
        with self.assertRaises(target.TargetRefused):
            target.stage2_run_catalog_comparison(self.connection, self.receipt, None)

    def test_import_is_driver_network_environment_and_receipt_io_free(self):
        import builtins
        import importlib.util
        # Execute a fresh module, with all inputs prepared before the spies.
        source = Path(self.auth.__file__).read_bytes()
        name = "_br02_authorization_import_probe"
        spec = importlib.util.spec_from_loader(name, loader=None)
        module = importlib.util.module_from_spec(spec)
        module.__file__ = self.auth.__file__
        original_import = builtins.__import__
        def guarded_import(name, *args, **kwargs):
            if name.split('.')[0] in {'psycopg', 'psycopg2', 'socket'}:
                self.fail("driver/network import during discovery")
            return original_import(name, *args, **kwargs)
        with mock.patch.dict(sys.modules, {name: module}), \
                mock.patch.object(builtins, '__import__', side_effect=guarded_import), \
                mock.patch.object(builtins, 'open', side_effect=AssertionError('file read')), \
                mock.patch.object(Path, 'read_bytes', side_effect=AssertionError('file read')), \
                mock.patch.object(os, 'environ', _RecordingMapping()):
            exec(compile(source, module.__file__, 'exec'), module.__dict__)
            with self.assertRaises(module.AuthorizationDenied):
                module.execute_stage2('synthetic-reference')
            self.assertEqual(os.environ.reads, [])


if __name__ == "__main__":
    unittest.main()
