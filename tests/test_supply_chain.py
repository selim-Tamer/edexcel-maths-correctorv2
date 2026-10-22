"""Stage S5 supply-chain regression tests (findings F-10, F-11, F-12).

Parse the dependency manifests and the GitHub Actions workflow with small
purpose-built readers (no YAML or pip libraries are installed by the offline
suite) and assert the structural guarantees introduced in Stage S5:

- production requirements use intentional, bounded or pinned constraints;
- Authlib remains declared for Streamlit's st.login OIDC flow (F-12);
- workflow actions are pinned to full 40-character commit SHAs (F-11);
- workflows run least-privilege and reference no repository secrets;
- the eight offline benchmark steps and three test suites remain in CI;
- dependabot configuration exists and covers pip and github-actions;
- the dependency policy stays documented in TESTING.md.

All files are read from the repository path resolved from this test file,
so the suite behaves identically from any working directory.
"""
import re
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]


def read_manifest(path):
    """Return a list of (name, [(op, version), ...]) requirement records.

    Comments and blank lines are stripped first; the manifests only use
    exact pins and simple range specifiers.
    """
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        match = re.match(r"^([A-Za-z0-9_.\-]+)\s*(.*)$", stripped)
        if not match:
            raise AssertionError(f"Unparsable requirement line in {path.name}: {line!r}")
        name, spec = match.groups()
        specifiers = re.findall(r"(>=|<=|==|!=|<|>)\s*([0-9][^\s,]*)", spec)
        records.append((name, specifiers))
    return records


def spec_of(records, name):
    for record_name, specifiers in records:
        if record_name.lower() == name.lower():
            return specifiers
    return None


class ProductionRequirements(TestCase):
    """Finding F-10: dependency constraints must be intentional, not open floors."""

    def setUp(self):
        self.records = read_manifest(ROOT / "requirements.txt")

    def test_every_dependency_has_an_upper_bound_or_exact_pin(self):
        for name, specifiers in self.records:
            ops = {op for op, _ in specifiers}
            self.assertTrue(
                "==" in ops or "<" in ops or "<=" in ops,
                f"{name} is declared without a bounded version range in requirements.txt",
            )

    def test_declared_dependency_set_matches_stage_s5_plan(self):
        self.assertEqual(
            {name.lower() for name, _ in self.records},
            {"streamlit", "authlib", "google-genai", "pandas",
             "requests", "pypdf", "pypdfium2", "pillow"},
        )

    def test_untrusted_pdf_stack_stays_exactly_pinned(self):
        for name in ("pypdf", "pypdfium2", "Pillow"):
            specifiers = spec_of(self.records, name)
            self.assertIn("==", {op for op, _ in specifiers},
                          f"{name} must remain exactly pinned (S3 untrusted-PDF controls)")

    def test_frameworks_stay_within_ci_validated_window(self):
        expectations = {
            "streamlit": ((1, 66, 1), (1, 67)),
            "google-genai": ((1, 40), (3,)),
            "pandas": ((2, 0), (4,)),
            "requests": ((2, 31), (3,)),
            "Authlib": ((1, 3, 2), (2,)),
        }
        for name, (floor, cap) in expectations.items():
            specifiers = spec_of(self.records, name)
            self.assertIsNotNone(specifiers, f"{name} missing from requirements.txt")
            self.assertIn(">=", {op for op, _ in specifiers},
                          f"{name} lost its documented floor")
            self.assertEqual(specifiers[0][1], ".".join(map(str, floor)),
                             f"{name} floor drifted from the CI-validated baseline")


class AuthlibRetention(TestCase):
    """Finding F-12: Authlib is required by Streamlit's st.login OIDC flow."""

    def test_authlib_stays_declared_and_referenced_by_the_app(self):
        records = read_manifest(ROOT / "requirements.txt")
        self.assertIsNotNone(spec_of(records, "Authlib"),
                             "Authlib was removed; st.login('google') needs it on Streamlit Cloud")
        app = (ROOT / "app5_1_automated.py").read_text(encoding="utf-8")
        self.assertIn("st.login(", app,
                      "Authlib is only justified while the app uses Streamlit's st.login")


class TestManifestConsistency(TestCase):
    def test_pdf_stack_pins_match_production(self):
        production = read_manifest(ROOT / "requirements.txt")
        test_only = read_manifest(ROOT / "tests" / "pdf-requirements.txt")
        for name in ("pypdf", "pypdfium2", "Pillow"):
            self.assertEqual(spec_of(production, name), spec_of(test_only, name),
                             f"{name} pins diverge between production and test manifests")

    def test_sympy_is_declared_with_the_ci_pin(self):
        test_only = read_manifest(ROOT / "tests" / "pdf-requirements.txt")
        specifiers = spec_of(test_only, "sympy")
        self.assertIsNotNone(specifiers, "sympy missing from tests/pdf-requirements.txt")
        self.assertEqual(("==", "1.14.0"), specifiers[0])

    def test_report_fixture_dependency_stays_development_only(self):
        test_only = read_manifest(ROOT / "tests" / "pdf-requirements.txt")
        self.assertIsNotNone(spec_of(test_only, "reportlab"),
                             "reportlab is the synthetic fixture generator and must stay test-only")
        production = read_manifest(ROOT / "requirements.txt")
        self.assertIsNone(spec_of(production, "reportlab"),
                          "reportlab must never enter the production manifest")


def workflow_lines():
    return (ROOT / ".github" / "workflows" / "marking-tests.yml").read_text(
        encoding="utf-8").splitlines()


def parse_permissions_blocks():
    """Yield (job, {key: value}) for every `permissions:` block in the workflow.

    Top-level keys of `jobs:` are only tracked while inside the jobs section;
    `on:` subkeys (push, pull_request, ...) are not jobs.
    """
    lines = workflow_lines()
    blocks = []
    mode = None
    current_job = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.strip() == "permissions:":
            indent = len(line) - len(line.lstrip())
            entries = {}
            cursor = index + 1
            while cursor < len(lines):
                child = lines[cursor]
                stripped = child.strip()
                if not stripped or stripped.startswith("#"):
                    cursor += 1
                    continue
                child_indent = len(child) - len(child.lstrip())
                if child_indent <= indent:
                    break
                key, _, value = stripped.partition(":")
                entries[key.strip()] = value.strip()
                cursor += 1
            blocks.append((None if indent == 0 else current_job, entries))
            index = cursor
            continue
        if re.match(r"^[A-Za-z0-9\-_]+:$", line):
            mode = line[:-1]
            if mode != "jobs":
                current_job = None
            index += 1
            continue
        if mode == "jobs" and re.match(r"^  ([a-zA-Z0-9\-_]+):$", line):
            current_job = line.strip()[:-1]
        index += 1
    return blocks


class WorkflowHardening(TestCase):
    """Finding F-11: immutable action references and least-privilege tokens."""

    ALLOWED_ACTIONS = {"actions/checkout", "actions/setup-python", "actions/github-script"}

    def test_every_action_is_pinned_to_a_full_commit_sha(self):
        for line in workflow_lines():
            if "uses:" not in line:
                continue
            ref = line.split("uses:", 1)[1].split("#", 1)[0].strip()
            match = re.match(r"^([A-Za-z0-9_.\-/]+)@([0-9a-f]{40})$", ref)
            self.assertIsNotNone(
                match, f"Action reference is not an immutable full commit SHA: {ref!r}")
            self.assertIn(match.group(1), self.ALLOWED_ACTIONS,
                          f"Un-reviewed action repository: {match.group(1)}")

    def test_no_untrusted_pull_request_target_triggers(self):
        content = "\n".join(workflow_lines())
        self.assertNotIn("pull_request_target", content,
                         "pull_request_target exposes secrets to untrusted PR code")

    def test_workflow_never_references_repository_secrets(self):
        content = "\n".join(workflow_lines())
        self.assertNotIn("${{ secrets.", content,
                         "No workflow job in this repository needs secrets")

    def test_least_privilege_permissions_blocks(self):
        blocks = parse_permissions_blocks()
        self.assertEqual(3, len(blocks), f"Expected three permissions blocks, got {blocks}")
        job, entries = blocks[0]
        self.assertIsNone(job, "First permissions block must be workflow-level")
        self.assertEqual({"contents": "read"}, entries)
        job, entries = blocks[1]
        self.assertEqual("dependency-audit", job)
        self.assertEqual({"contents": "read"}, entries)
        job, entries = blocks[2]
        self.assertEqual("report-scheduled-failure", job)
        self.assertEqual({"issues": "write", "actions": "read"}, entries)


class WorkflowCoverage(TestCase):
    """CI must keep preserving the full offline regression surface."""

    REQUIRED_BENCHMARKS = (
        "benchmark_document_pipeline.py",
        "benchmark_local_processing.py",
        "benchmark_pdf_inspection.py",
        "benchmark_marking_requests.py",
        "benchmark_service_recovery.py",
        "benchmark_automatic_recovery.py",
        "benchmark_deadline_recovery.py",
        "benchmark_gemini_limits.py",
    )

    def test_all_eight_offline_benchmarks_remain_in_ci(self):
        content = "\n".join(workflow_lines())
        for benchmark in self.REQUIRED_BENCHMARKS:
            self.assertIn(f"python tests/{benchmark}", content,
                          f"Offline benchmark dropped from CI: {benchmark}")

    def test_regression_test_suites_remain_in_ci(self):
        content = "\n".join(workflow_lines())
        self.assertIn("unittest discover -s tests -p 'test_*.py'", content)
        self.assertIn("unittest discover -s tests/ui -p 'test_*.py'", content)
        self.assertIn("unittest discover -s tests/sdk -p 'test_*.py'", content)
        self.assertIn("unittest investigation.test_accuracy_regression", content)


class DependabotConfiguration(TestCase):
    def test_dependabot_covers_pip_and_workspace_actions(self):
        path = ROOT / ".github" / "dependabot.yml"
        self.assertTrue(path.is_file(), "dependabot.yml missing: dependency updates become unreviewable")
        content = path.read_text(encoding="utf-8")
        self.assertIn("version: 2", content)
        self.assertIn("package-ecosystem: \"pip\"", content)
        self.assertIn("package-ecosystem: \"github-actions\"", content)
        self.assertEqual(2, content.count("package-ecosystem: \"pip\""),
                         "dependabot must track both the production manifest and the tests directory")


class PolicyDocumentation(TestCase):
    def test_dependency_policy_is_documented(self):
        testing = (ROOT / "TESTING.md").read_text(encoding="utf-8")
        self.assertIn("Dependency policy and supply chain", testing,
                      "Installation and dependency documentation must stay current with the manifests")


if __name__ == "__main__":
    unittest.main()
