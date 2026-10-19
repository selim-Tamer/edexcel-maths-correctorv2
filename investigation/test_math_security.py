"""Security invariants for the Stage C4 mathematical modules.

- no forbidden imports or execution helpers in module source
- importing the modules never pulls in a network library
- importing the modules never pulls in a production module
- a battery of execution-oriented inputs is refused without any side
  effect and without any exception escaping the public API
- verification is deterministic across separate interpreter processes
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

from investigation.math_verification import verify_claim

_C4_MODULES = ("safe_math.py", "math_verification.py",
               "math_claims.py", "math_benchmark.py")

_FORBIDDEN = (
    # production / external integrations
    "import google", "from google", "import streamlit", "from streamlit",
    "import app5", "from app5", "import document_pipeline",
    "from document_pipeline", "import local_pdf", "from local_pdf",
    "import marking_requests", "from marking_requests",
    # network / process
    "import urllib", "from urllib", "import socket", "import requests",
    "from requests", "import subprocess", "from subprocess",
    "import http", "from http", "import ftplib", "import telnetlib",
    "os.system", "os.environ", "os.startfile",
    # execution helpers (the engine must never run untrusted strings)
    "eval(", "exec(", "compile(", "sympify(", "parse_expr(",
    "__import__(", "globals(", "locals(", "open(",
)

_REPO = Path(__file__).resolve().parents[1]


class ModuleHygieneTests(unittest.TestCase):

    def test_c4_module_source_contains_nothing_forbidden(self):
        base = Path(__file__).parent.resolve()
        for name in _C4_MODULES:
            source = (base / name).read_text(encoding="utf-8")
            for pattern in _FORBIDDEN:
                self.assertNotIn(pattern, source,
                                 f"{name} contains {pattern!r}")

    def test_c4_module_source_has_no_network_endpoints(self):
        import re
        base = Path(__file__).parent.resolve()
        for name in _C4_MODULES:
            source = (base / name).read_text(encoding="utf-8")
            self.assertFalse(
                re.search(r"http[s]?://|api[_-]?key|Authorization",
                          source),
                f"{name} must not reference network endpoints or keys")


class ImportIsolationTests(unittest.TestCase):

    def _fresh_interpreter_probe(self):
        code = "\n".join([
            "import sys",
            f"sys.path.insert(0, r'{_REPO}')",
            "import investigation.safe_math",
            "import investigation.math_verification",
            "import investigation.math_claims",
            "import investigation.math_benchmark",
            "network = sorted(set(sys.modules) & "
            "{'socket','urllib.request','requests','http.client',"
            "'ftplib','telnetlib','smtplib','http'})",
            "production = sorted(set(sys.modules) & "
            "{'app5_1_automated','document_pipeline','local_pdf',"
            "'marking_requests'})",
            "print('NETWORK-' , network)",
            "print('PRODUCTION-', production)",
        ])
        finished = subprocess.run(
            [sys.executable, "-c", code], cwd=str(_REPO),
            capture_output=True, text=True, timeout=300)
        self.assertEqual(finished.returncode, 0,
                         finished.stderr[-2000:] if finished.stderr else "")
        return [line for line in finished.stdout.splitlines()
                if line.startswith(("NETWORK-", "PRODUCTION-"))]

    def test_importing_the_engine_loads_no_network_module(self):
        lines = self._fresh_interpreter_probe()
        self.assertTrue(any("NETWORK-" in line and "[]" in line
                            for line in lines), lines)

    def test_importing_the_engine_loads_no_production_module(self):
        lines = self._fresh_interpreter_probe()
        self.assertTrue(any("PRODUCTION-" in line and "[]" in line
                            for line in lines), lines)


class ExecutionSafetyTests(unittest.TestCase):

    MALICIOUS = (
        '__import__("os").system("pwned")',
        'open("calc.txt", "w")',
        'eval("2 + 2")',
        'exec("2 + 2")',
        'globals()',
        'x.__class__',
        '().__class__.__base__',
        'x.__init__.__globals__',
        'lambda: x',
        '[y for y in (x,)]',
        'x if x else 3',
        '{"a": {"b": [c.__class__, 2]}}',
    )

    def test_every_execution_attempt_is_refused_explicitly(self):
        for text in self.MALICIOUS:
            result = verify_claim({
                "claim_id": "SEC-" + str(self.MALICIOUS.index(text)),
                "kind": "algebraic_equivalence",
                "lhs": text,
                "rhs": "x",
                "source_references": [],
            })
            self.assertEqual(result["outcome"], "unsupported", text)
            self.assertIn("refusal_reason", result["evidence"], text)

    def test_parser_refusal_detail_is_sanitised_not_raw(self):
        from investigation.safe_math import ExpressionRefused
        error = ExpressionRefused("unknown_symbol", "super-secret name!")
        self.assertNotIn("!", error.detail)
        self.assertNotIn(" ", error.detail)


class CrossProcessDeterminismTests(unittest.TestCase):

    def test_same_claim_same_result_across_two_interpreters(self):
        claim = {
            "claim_id": "DET-1", "kind": "fraction_equivalence",
            "lhs": "3/(x - 2)", "rhs": "3*(x + 2)/(x**2 - 4)",
            "source_references": [],
        }
        code = "\n".join([
            f"import sys; sys.path.insert(0, r'{_REPO}')",
            "import json",
            "from investigation.math_verification import verify_claim",
            "claim = json.loads(sys.argv[1])",
            "print(json.dumps(verify_claim(claim), sort_keys=True))",
        ])
        serialised = json.dumps(claim)
        outputs = []
        for _ in range(2):
            finished = subprocess.run(
                [sys.executable, "-c", code, serialised], cwd=str(_REPO),
                capture_output=True, text=True, timeout=300)
            self.assertEqual(finished.returncode, 0,
                             finished.stderr[-2000:])
            outputs.append(finished.stdout)
        self.assertEqual(outputs[0], outputs[1])


if __name__ == "__main__":
    unittest.main()
