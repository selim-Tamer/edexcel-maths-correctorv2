"""Real marking-engine wiring for the dev/demo job runner (explicit opt-in).

Honest posture
--------------
* **No new marking logic.** This module only reuses the already-reviewed
  marking path: ``document_pipeline.transfer`` for the byte-preserving upload
  and ``app5_1_automated.mark_one_submission`` (client -> grade -> normalise
  -> verify) for the generation and validation stages. Nothing here grades,
  clamps or rewrites a mark.
* **Fail-closed by default.** ``build_engine()`` returns ``None`` unless the
  operator sets ``AI_MARKER_ENGINE_REAL=1`` AND a Gemini API key is present
  AND ``AI_MARKER_MARK_SCHEME`` points at an existing ``%PDF`` mark scheme.
  With no engine the job runner refuses ``503 model_unavailable`` and leaves
  the job untouched - it never fabricates a result and never spends tokens.
* **Cost disclosure.** A configured engine DOES call the paid Gemini API per
  executed job (upload + generation, plus audit when
  ``AI_MARKER_ENGINE_AUDIT=1``). Nothing in this repository's tests or CI sets
  these variables, so no test run or CI job spends anything: offline tests
  exercise the wiring with injected fakes instead.
* **Synchronous execution.** The engine runs inside the
  ``POST /v1/jobs/{job_id}/run`` request (bounded by that request). A
  background worker is the production shape; that is a disclosed beta
  deviance, not a claim of a running worker.

Collaborators are injectable (``client_factory``/``upload``/``grade``) purely
so the wiring itself can be tested offline with fakes; the defaults are the
real path above.
"""
from __future__ import annotations

import functools
import os
import time

REAL_ENV = "AI_MARKER_ENGINE_REAL"
SCHEME_ENV = "AI_MARKER_MARK_SCHEME"
AUDIT_ENV = "AI_MARKER_ENGINE_AUDIT"
KEY_ENV = "GEMINI_API_KEY"

_FILENAME = "submission.pdf"


def _configured() -> tuple:
    """(ready, api_key, scheme_bytes, scheme_name) - every gate fail-closed."""

    if (os.environ.get(REAL_ENV) or "").strip() != "1":
        return False, "", b"", ""
    key = (os.environ.get(KEY_ENV) or "").strip()
    path = (os.environ.get(SCHEME_ENV) or "").strip()
    if not key or not path:
        return False, "", b"", ""
    try:
        with open(path, "rb") as handle:
            scheme = handle.read()
    except OSError:
        return False, "", b"", ""
    if not scheme.startswith(b"%PDF"):
        return False, "", b"", ""
    return True, key, scheme, os.path.basename(path)


def _real_grade(client, student_file, mark_scheme_file, *, audit_enabled=False):
    """The existing marking path, called exactly as ``engine()`` calls it.

    Parameter names are load-bearing: ``engine()`` passes ``client``,
    ``student_file`` and ``mark_scheme_file`` by keyword, and a mismatch would
    only surface on a configured run - after the provider uploads were
    already paid for. ``tests/test_marking_engine.py`` pins this signature.
    The import is lazy because the app module is Streamlit-side and is only
    ever loaded when a real engine has been explicitly enabled.
    """
    import app5_1_automated as app
    wrapper = app.mark_one_submission(client, "", student_file,
                                      mark_scheme_file, None,
                                      audit_enabled=bool(audit_enabled))
    return wrapper if isinstance(wrapper, dict) else {}


def build_engine(*, client_factory=None, upload=None, grade=None):
    """Return the real marking callable, or ``None`` when not configured.

    ``None`` is the normal, safe state: the runner then refuses with
    ``503 model_unavailable`` instead of inventing a result.
    """
    ready, key, scheme_bytes, scheme_name = _configured()
    if not ready:
        return None

    def real_client(api_key: str):
        # Imported lazily: the Gemini SDK is only touched on the opt-in path,
        # so importing this module (or building an unconfigured app) never
        # loads the provider SDK or reads credentials.
        from google import genai
        from google.genai import types
        return genai.Client(api_key=api_key,
                            http_options=types.HttpOptions(timeout=120_000))

    def real_upload(client, data: bytes, filename: str):
        import document_pipeline
        return document_pipeline.transfer(client, data, filename,
                                          progress=None, clock=time)

    client_factory = client_factory or real_client
    upload = upload or real_upload
    # The default grade is the REAL path, exposed as a module-level function
    # so its signature can be pinned against the exact keyword arguments
    # ``engine()`` passes (an argument-name mismatch would otherwise only
    # surface on a configured run, after provider calls were already paid).
    audit_enabled = (os.environ.get(AUDIT_ENV) or "").strip() == "1"
    grade = grade or functools.partial(_real_grade, audit_enabled=audit_enabled)
    cached_scheme = {"file": None}

    def engine(data: bytes) -> dict:
        """Mark one stored document and return its ``original_output``.

        Failure shapes are deliberately boring: unrecognised input or an
        unusable provider response returns ``{}``, which the shared
        registration path classifies as ``original_output_invalid`` with no
        store write - never a fabricated marking result.
        """
        if not isinstance(data, (bytes, bytearray)) or data[:4] != b"%PDF":
            return {}
        client = client_factory(key)
        student = upload(client, bytes(data), _FILENAME)
        if cached_scheme["file"] is None:
            # The mark scheme is uploaded once per engine instance, not per
            # job: repeated jobs do not re-transfer a static document.
            cached_scheme["file"] = upload(client, scheme_bytes, scheme_name)
        wrapper = grade(client=client, student_file=student,
                        mark_scheme_file=cached_scheme["file"])
        result = wrapper.get("result") if isinstance(wrapper, dict) else None
        if not isinstance(result, dict) or not isinstance(result.get("questions"),
                                                           list):
            return {}
        if audit_enabled:
            # Surfaced for the status payload only; the audit model label is
            # produced by the existing marking path, never re-derived here.
            result.setdefault("audit_model", wrapper.get("audit_model", "Not run"))
        return result

    return engine
