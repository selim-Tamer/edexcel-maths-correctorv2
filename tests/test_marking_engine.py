"""Real marking-engine wiring tests (offline, injected collaborators).

The engine adapter must be fail-closed without explicit opt-in, must reuse
the existing marking path instead of inventing logic, must upload the static
mark scheme only once, and must degrade to an empty output (classified
``original_output_invalid`` by the registration path) when the provider
returns something unusable - never to a fabricated result.

No test here sets a real API key or makes a network call: the collaborators
are injected, and the configured path is driven with a temporary ``%PDF``
mark scheme only.
"""
from __future__ import annotations

import inspect
import os
import sys
import tempfile
import types
import unittest

import marking_engine


def questions_output():
    return {"questions": [
        {"question_number": "1", "awarded_marks": 2, "max_marks": 3},
        {"question_number": "2", "awarded_marks": 1, "max_marks": 1},
    ]}


class EngineConfigurationTests(unittest.TestCase):
    def setUp(self):
        for name in (marking_engine.REAL_ENV, marking_engine.SCHEME_ENV,
                     marking_engine.AUDIT_ENV, marking_engine.KEY_ENV):
            prior = os.environ.pop(name, None)
            if prior is not None:
                self.addCleanup(os.environ.__setitem__, name, prior)
            else:
                self.addCleanup(os.environ.pop, name, None)

    def test_unconfigured_means_no_engine(self):
        # Default: nothing set -> no engine (the runner then 503s).
        self.assertIsNone(marking_engine.build_engine())
        # Opt-in without a key -> still no engine.
        os.environ[marking_engine.REAL_ENV] = "1"
        self.assertIsNone(marking_engine.build_engine())
        # Key without a mark scheme -> still no engine.
        os.environ[marking_engine.KEY_ENV] = "test-key-not-a-secret"
        self.assertIsNone(marking_engine.build_engine())
        # Mark scheme path that does not exist -> still no engine.
        os.environ[marking_engine.SCHEME_ENV] = os.path.join(
            tempfile.gettempdir(), "definitely-missing-scheme.pdf")
        self.assertIsNone(marking_engine.build_engine())

    def test_non_pdf_mark_scheme_is_refused(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
            handle.write(b"this is not a pdf")
            path = handle.name
        self.addCleanup(os.environ.pop, marking_engine.REAL_ENV, None)
        self.addCleanup(os.unlink, path)
        os.environ[marking_engine.REAL_ENV] = "1"
        os.environ[marking_engine.KEY_ENV] = "test-key-not-a-secret"
        os.environ[marking_engine.SCHEME_ENV] = path
        self.assertIsNone(marking_engine.build_engine())

    def test_building_never_imports_the_provider_or_app_modules(self):
        """Fail-closed must not even load the SDK/app side effects."""
        before = set(sys.modules)
        self.assertIsNone(marking_engine.build_engine())
        loaded = set(sys.modules) - before
        self.assertEqual(
            sorted(m for m in loaded
                   if m.startswith(("google", "app5_1_automated",
                                    "document_pipeline"))), [])


class EngineWiringTests(unittest.TestCase):
    def setUp(self):
        scheme = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        scheme.write(b"%PDF-1.4 synthetic mark scheme")
        scheme.close()
        self.scheme_path = scheme.name
        self.addCleanup(os.unlink, self.scheme_path)
        for name, value in ((marking_engine.REAL_ENV, "1"),
                            (marking_engine.KEY_ENV, "test-key-not-a-secret"),
                            (marking_engine.SCHEME_ENV, self.scheme_path)):
            prior = os.environ.get(name)
            os.environ[name] = value
            if prior is None:
                self.addCleanup(os.environ.pop, name, None)
            else:
                self.addCleanup(os.environ.__setitem__, name, prior)
        self.clients = []
        self.uploads = []
        self.grades = []

    def _engine(self, grade_result=None):
        def client_factory(key):
            self.clients.append(key)
            return object()

        def upload(client, data, filename):
            self.uploads.append((filename, data[:5]))
            return "file:" + filename

        def grade(**kwargs):
            self.grades.append(kwargs)
            if grade_result is None:
                return {"result": questions_output(), "audit_model": "Not run"}
            return grade_result

        return marking_engine.build_engine(client_factory=client_factory,
                                            upload=upload, grade=grade)

    def test_engine_runs_the_existing_path_and_uploads_the_scheme_once(self):
        engine = self._engine()
        self.assertIsNotNone(engine)
        first = engine(b"%PDF-first submission")
        second = engine(b"%PDF-second submission")
        self.assertEqual([q["awarded_marks"] for q in first["questions"]],
                         [2, 1])
        self.assertEqual(second, first)
        # One client per execution, built from the configured key.
        self.assertEqual(self.clients, ["test-key-not-a-secret"] * 2)
        # The student document is transferred per job; the static mark scheme
        # exactly once for the engine's lifetime.
        names = [name for name, _ in self.uploads]
        self.assertEqual(names.count("submission.pdf"), 2)
        self.assertEqual(names.count(os.path.basename(self.scheme_path)), 1)
        # The grade step receives the uploaded handles, by keyword.
        self.assertEqual(len(self.grades), 2)
        self.assertEqual(self.grades[0]["student_file"], "file:submission.pdf")
        self.assertEqual(self.grades[0]["mark_scheme_file"],
                         "file:" + os.path.basename(self.scheme_path))

    def test_unusable_provider_output_degrades_to_an_empty_result(self):
        for bad in ({}, {"result": "not a mapping"},
                    {"result": {"questions": "not a list"}}):
            with self.subTest(bad=bad):
                self.clients, self.uploads, self.grades = [], [], []
                engine = self._engine(grade_result=bad)
                # An empty dict is what the shared registration path
                # classifies as original_output_invalid with no store write.
                self.assertEqual(engine(b"%PDF-submission"), {})

    def test_non_pdf_input_never_reaches_the_provider(self):
        engine = self._engine()
        self.assertEqual(engine(b"<html>not a pdf</html>"), {})
        self.assertEqual(self.clients, [])
        self.assertEqual(self.uploads, [])
        self.assertEqual(self.grades, [])


class RealGradeSignatureTests(unittest.TestCase):
    """IR's PR60 finding: the grade argument names must match engine().

    A mismatch only surfaces on a CONFIGURED run - after the paid provider
    uploads - so it is pinned here instead: the exact keyword arguments
    ``engine()`` passes must bind to the default real grade, and the default
    wiring must actually reach that function.
    """

    ENGINE_CALL = {"client": object(), "student_file": "file:submission.pdf",
                   "mark_scheme_file": "file:scheme.pdf"}

    def test_engine_call_kwargs_bind_to_the_real_grade(self):
        signature = inspect.signature(marking_engine._real_grade)
        bound = signature.bind(**self.ENGINE_CALL)
        self.assertEqual(list(bound.arguments),
                         ["client", "student_file", "mark_scheme_file"])
        # audit_enabled is supplied by build_engine as a partial keyword.
        bound.apply_defaults()
        self.assertIs(bound.arguments["audit_enabled"], False)

    def test_default_wiring_reaches_the_real_grade_with_those_kwargs(self):
        calls = {}

        class FakeApp(types.ModuleType):
            def mark_one_submission(self, *args, **kwargs):
                calls["args"] = args
                calls["kwargs"] = kwargs
                return {"result": questions_output(), "audit_model": "stub"}

        stub = FakeApp("app5_1_automated")
        prior = sys.modules.get("app5_1_automated")
        sys.modules["app5_1_automated"] = stub
        try:
            if prior is None:
                self.addCleanup(sys.modules.pop, "app5_1_automated", None)
            else:
                self.addCleanup(sys.modules.__setitem__, "app5_1_automated",
                                prior)
            scheme = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
            scheme.write(b"%PDF-1.4 synthetic mark scheme")
            scheme.close()
            self.addCleanup(os.unlink, scheme.name)
            for name, value in ((marking_engine.REAL_ENV, "1"),
                                (marking_engine.KEY_ENV, "test-key"),
                                (marking_engine.SCHEME_ENV, scheme.name)):
                os.environ[name] = value
                self.addCleanup(os.environ.pop, name, None)

            engine = marking_engine.build_engine(
                client_factory=lambda key: "client",
                upload=lambda client, data, filename: "file:" + filename)
            self.assertIsNotNone(engine)
            output = engine(b"%PDF-submission")
        except TypeError as failure:  # the exact regression under test
            self.fail(f"default grade wiring rejected engine kwargs: {failure}")
        self.assertEqual([q["awarded_marks"] for q in output["questions"]],
                         [2, 1])
        self.assertEqual(
            calls["args"][:5],
            ("client", "", "file:submission.pdf", "file:" + os.path.basename(
                scheme.name), None))
        self.assertIn("audit_enabled", calls["kwargs"])


if __name__ == "__main__":
    unittest.main()
