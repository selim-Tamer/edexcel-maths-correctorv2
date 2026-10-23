"""M5 slice C: the session-free decision core (offline, synthetic only).

Two claims are checked here and nowhere else:

1. **Isolation** -- ``decision_core`` imports and runs with no Streamlit in
   the process and never touches ``st.session_state``; the caller supplies
   the state mapping, identity and side-effect sinks explicitly.
2. **Parity and fail-closed gates** -- driving the core directly produces
   byte-identical decision records, event trails, notices and override state
   to the Streamlit application adapter, and every gate refuses *before*
   writing anything.

No network: the headless loader's shared ``requests`` fake raises on any
unexpected call, so a green run proves nothing reached a provider.
"""
import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audit_store
import decision_core
import question_review as qr
from support import benchmark, load_app, model_question

ROOT = Path(__file__).resolve().parents[1]
SUBJECT = "teacher-a-subject"
OWNER = "google:" + SUBJECT
PDF = b"%PDF-1.7 m5 decision core fixture"
DIGEST = hashlib.sha256(PDF).hexdigest()
NOW = "1700000000"
DECISION_KEYS = ("question_review_decisions", "question_review_events",
                 "question_review_notice", "teacher_overrides",
                 "teacher_override_reasons")
# What a refusal must never touch. The notice is the refusal channel itself,
# so it is deliberately absent here: a gate failure writes the notice only.
WRITE_KEYS = ("question_review_decisions", "question_review_events",
              "teacher_overrides", "teacher_override_reasons")


def sample_question(number="1", awarded=1, maximum=2):
    sample = model_question(benchmark()["questions"][0], "partial")
    sample["question_number"] = number
    sample["awarded_marks"] = awarded
    sample["max_marks"] = maximum
    return sample


def manual_row(review_key="review-1", owner=OWNER):
    questions = [sample_question()]
    return {"status": "Marked", "review_key": review_key,
            "report_owner": owner, "snapshot_context": DIGEST,
            "revision": "", "selected_questions": ["1"],
            "ai_result": {"questions": json.loads(json.dumps(questions))},
            "result": {"questions": json.loads(json.dumps(questions))},
            "student_name": "Synthetic", "marking_model": "fake",
            "audit_model": "Not run"}


# --- explicit sinks: the same writes the application performs -------------

def notice_sink(state):
    def fail(code, question_number, detail=""):
        state["question_review_notice"] = (code, str(question_number), detail)
    return fail


def event_sink(state, actor=OWNER, timestamp=NOW):
    def record_event(row, student_key, question_number, action, binding,
                     evidence):
        events = state.setdefault("question_review_events", [])
        events.append(qr.make_review_event(
            assignment_id=("manual"
                           if decision_core.row_review_scope(row) == "manual"
                           else str(row.get("classroom_assignment_id"))),
            student_id=str(student_key),
            question_number=str(question_number),
            action=str(action),
            actor=str(actor or "unauthenticated"),
            binding=binding or "",
            evidence=evidence,
            timestamp=timestamp))
        state["question_review_events"] = qr.bounded_events(events)
    return record_event


def override_sink(state):
    def write(student_key, question_number, mark, reason):
        state.setdefault("teacher_overrides", {}).setdefault(
            student_key, {})[question_number] = int(mark)
        state.setdefault("teacher_override_reasons", {}).setdefault(
            student_key, {})[question_number] = reason
    return write


def build_env(state, row, *, subject=SUBJECT, owner=OWNER, resolve_row=None,
              row_owner_allowed=None, manual_context=DIGEST,
              provenance=True, plan_resolver=None, reverify=None):
    return decision_core.DecisionEnv(
        subject=subject,
        owner=owner,
        resolve_row=(resolve_row or (lambda: row)),
        row_owner_allowed=(row_owner_allowed
                           or (lambda candidate:
                               candidate.get("report_owner") == owner)),
        manual_context=manual_context,
        reverify=(reverify or (lambda candidate:
                               (None, "classroom_verification_unavailable"))),
        manual_provenance_ok=(lambda candidate: provenance),
        plan_resolver=(plan_resolver
                       or (lambda candidate, actor: (None, None, None))),
        write_override=override_sink(state),
        fail=notice_sink(state),
        record_event=event_sink(state),
        now=lambda: NOW,
    )


def run_core(state, action="approve", reason="", mark=None, **env_kwargs):
    decision_core.apply_review_decision(
        state, student_key="review-1", question_number="1", action=action,
        reason=reason, corrected_mark=mark,
        env=build_env(state, env_kwargs.pop("row", None) or manual_row(),
                      **env_kwargs))
    return state.get("question_review_notice")


class DecisionCoreIsolationTests(unittest.TestCase):
    """The core must be importable and runnable without Streamlit."""

    def test_source_never_references_streamlit_or_session_state(self):
        """Check the code, not prose: docstrings may name what is avoided."""
        import ast

        tree = ast.parse((ROOT / "decision_core.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        self.assertFalse(any(name.startswith("streamlit") for name in imported),
                         f"decision_core imports {sorted(imported)}")
        offending = [
            ast.dump(node) for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id == "st"
        ]
        self.assertEqual(offending, [], "decision_core references the name `st`")

    def test_import_leaves_streamlit_unimported(self):
        probe = subprocess.run(
            [sys.executable, "-c",
             "import sys, decision_core; "
             "assert 'streamlit' not in sys.modules, "
             "'importing decision_core pulled in streamlit'"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=120)
        self.assertEqual(probe.returncode, 0, probe.stderr)

    def test_application_adapter_still_passes_the_session_state(self):
        """The streamlit side keeps exactly the coupling it claims."""
        app = (ROOT / "app5_1_automated.py").read_text(encoding="utf-8")
        self.assertIn("decision_core.apply_review_decision(\n"
                      "        st.session_state,", app)


class CoreGateTests(unittest.TestCase):
    """Every gate refuses before a single write happens."""

    def setUp(self):
        self.state = {}

    def assert_nothing_written(self):
        for key in WRITE_KEYS:
            self.assertNotIn(key, self.state, f"{key} must not be written")

    def test_anonymous_subject_fails_before_any_row_lookup(self):
        resolve = Mock(side_effect=AssertionError("row lookup must not run"))
        notice = run_core(self.state, subject=None, resolve_row=resolve)
        self.assertEqual(notice[0], "authentication_required")
        resolve.assert_not_called()
        self.assert_nothing_written()

    def test_foreign_owner_row_is_refused(self):
        notice = run_core(self.state, row_owner_allowed=lambda row: False)
        self.assertEqual(notice[0], "review_context_unavailable")
        self.assert_nothing_written()

    def test_row_for_another_review_key_is_refused(self):
        notice = run_core(self.state, row=manual_row(review_key="review-2"))
        self.assertEqual(notice[0], "review_context_unavailable")
        self.assert_nothing_written()

    def test_missing_manual_provenance_fails_closed(self):
        notice = run_core(self.state, provenance=False)
        self.assertEqual(notice[0], "manual_document_provenance_required")
        self.assert_nothing_written()

    def test_invalid_action_is_refused(self):
        notice = run_core(self.state, action="release-grade")
        self.assertEqual(notice[0], "review_action_invalid")
        self.assert_nothing_written()

    def test_classroom_reverify_failure_never_reaches_the_store(self):
        plan = Mock(side_effect=AssertionError("plan must not be resolved"))
        row = manual_row()
        row.update({"classroom_course_id": "course-1",
                    "classroom_assignment_id": "work-1",
                    "classroom_context": "c" * 64})
        notice = run_core(self.state, row=row,
                          reverify=lambda candidate:
                          (None, "classroom_verification_unavailable"),
                          plan_resolver=plan)
        self.assertEqual(notice[0], "classroom_verification_unavailable")
        plan.assert_not_called()
        self.assert_nothing_written()


class CoreDecisionTests(unittest.TestCase):
    """Success paths write exactly the decision record, event and notice."""

    def setUp(self):
        self.state = {}
        self.row = manual_row()

    def decision(self, action="approve", reason="", mark=None, **kwargs):
        kwargs.setdefault("row", self.row)
        return run_core(self.state, action=action, reason=reason, mark=mark,
                        **kwargs)

    def stored(self):
        return self.state.get("question_review_decisions", {}).get(
            "review-1::q1")

    def test_approve_records_decision_event_and_notice(self):
        self.assertEqual(self.decision()[0], "ok")
        record = self.stored()
        self.assertEqual(record["status"], "approved")
        self.assertEqual(record["actor"], OWNER)
        self.assertEqual(record["updated_at"], NOW)
        self.assertEqual(len(record["binding"]), 64)
        evidence = record["authorization_evidence"]
        self.assertEqual(evidence["scope"], "manual")
        self.assertEqual(evidence["actor"], OWNER)
        self.assertEqual(evidence["authorization_revision"], DIGEST)
        self.assertEqual(evidence["verified_at"], NOW)
        events = self.state["question_review_events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["binding"], record["binding"])
        self.assertEqual(events[0]["actor"], OWNER)
        self.assertEqual(self.state["question_review_notice"],
                         ("ok", "1", "approved"))
        self.assertNotIn("teacher_overrides", self.state)

    def test_correct_writes_the_override_without_touching_original_marks(self):
        notice = self.decision(action="correct", mark=2, reason="misread")
        self.assertEqual(notice[0], "ok")
        self.assertEqual(self.state["teacher_overrides"]["review-1"]["1"], 2)
        self.assertEqual(self.state["teacher_override_reasons"]["review-1"]["1"],
                         "misread")
        self.assertEqual(self.stored()["status"], "corrected")
        original = self.row["ai_result"]["questions"][0]["awarded_marks"]
        self.assertEqual(original, 1, "original AI marks must never be edited")

    def test_override_write_rejected_when_the_state_write_is_ignored(self):
        def refusing_write(student_key, question_number, mark, reason):
            return None  # the write "happens" but changes nothing

        env = build_env(self.state, self.row)
        env.write_override = refusing_write
        decision_core.apply_review_decision(
            self.state, student_key="review-1", question_number="1",
            action="correct", reason="misread", corrected_mark=2, env=env)
        self.assertEqual(
            self.state["question_review_notice"][0],
            "override_write_rejected")
        # The decision read may create the empty store (the application path
        # does the same), but no record may survive the failed commit.
        self.assertEqual(self.state.get("question_review_decisions"), {})
        self.assertNotIn("question_review_events", self.state)


class CoreRollbackTests(unittest.TestCase):
    """A failed durable commit must leave no partial state behind."""

    def setUp(self):
        self.state = {}
        self.row = manual_row()

    def plan_for(self, error):
        def plan_resolver(row, actor):
            store = Mock()
            store.register_result = Mock()
            if isinstance(error, Exception):
                store.commit_correction = Mock(side_effect=error)
            else:
                store.commit_correction = Mock(
                    side_effect=audit_store.AuditStoreError(error))
            return store, {"result_id": "manual:" + DIGEST[:16] + ":review-1",
                           "expected_revision": 3}, None
        return plan_resolver

    def test_stale_revision_restores_the_previous_override(self):
        self.state["teacher_overrides"] = {"review-1": {"1": 5}}
        self.state["teacher_override_reasons"] = {"review-1": {"1": "prior"}}
        env = build_env(self.state, self.row,
                        plan_resolver=self.plan_for("stale_revision"))
        decision_core.apply_review_decision(
            self.state, student_key="review-1", question_number="1",
            action="correct", reason="misread", corrected_mark=2, env=env)
        self.assertEqual(self.state["question_review_notice"],
                         ("stale_revision", "1", ""))
        self.assertEqual(self.state["teacher_overrides"]["review-1"]["1"], 5)
        self.assertEqual(
            self.state["teacher_override_reasons"]["review-1"]["1"], "prior")
        # The decision read may create the empty store (the application path
        # does the same), but no record may survive the failed commit.
        self.assertEqual(self.state.get("question_review_decisions"), {})
        self.assertNotIn("question_review_events", self.state)

    def test_commit_crash_removes_the_fresh_override(self):
        env = build_env(self.state, self.row,
                        plan_resolver=self.plan_for(RuntimeError("disk gone")))
        decision_core.apply_review_decision(
            self.state, student_key="review-1", question_number="1",
            action="correct", reason="misread", corrected_mark=2, env=env)
        self.assertEqual(self.state["question_review_notice"],
                         ("audit_persist_failed", "1", ""))
        self.assertNotIn("review-1", self.state.get("teacher_overrides", {}))
        # The decision read may create the empty store (the application path
        # does the same), but no record may survive the failed commit.
        self.assertEqual(self.state.get("question_review_decisions"), {})
        self.assertNotIn("question_review_events", self.state)

    def test_successful_commit_adds_the_audit_reference(self):
        store = Mock()
        store.register_result = Mock()
        store.commit_correction = Mock(
            return_value={"sequence": 7, "event_id": "evt-7"})
        env = build_env(
            self.state, self.row,
            plan_resolver=lambda row, actor: (
                store, {"result_id": "manual:" + DIGEST[:16] + ":review-1",
                        "expected_revision": 0}, None))
        decision_core.apply_review_decision(
            self.state, student_key="review-1", question_number="1",
            action="approve", reason="", corrected_mark=None, env=env)
        record = self.state["question_review_decisions"]["review-1::q1"]
        self.assertEqual(record["audit_reference"],
                         {"result_id": "manual:" + DIGEST[:16] + ":review-1",
                          "sequence": 7, "event_id": "evt-7"})
        # The idempotency key is owner + result stream + payload derived.
        self.assertEqual(store.commit_correction.call_count, 1)
        kwargs = store.commit_correction.call_args.kwargs
        self.assertEqual(kwargs["expected_revision"], 0)
        self.assertEqual(len(kwargs["idempotency_key"]), 64)


class AppAdapterParityTests(unittest.TestCase):
    """The same decision through the Streamlit adapter and through the core
    must leave byte-identical decision state."""

    def setUp(self):
        self.f = load_app()
        self.session = {}
        self.f["st"] = SimpleNamespace(
            session_state=self.session, user=SimpleNamespace(sub=SUBJECT))
        self.f["google_logged_in"] = lambda: True
        self.f["authenticated_google_identity"] = lambda: SUBJECT
        self.f["pd"] = SimpleNamespace(DataFrame=lambda rows: rows)
        self.f["remember_manual_upload"](PDF)
        self.session["last_manual_context"] = DIGEST
        # Deterministic clock: the adapter and the core both format it.
        self.f["time"] = SimpleNamespace(time=lambda: 1700000000.0)
        self.row = manual_row()

    def run_both(self, action, reason, mark):
        app_session = dict(self.session)
        app_session["last_batch_results"] = [dict(self.row)]
        self.f["st"] = SimpleNamespace(
            session_state=app_session, user=SimpleNamespace(sub=SUBJECT))
        self.f["apply_question_review_decision"](
            "review-1", "1", action, reason, mark)

        core_state = {"last_manual_context": DIGEST}
        run_core(core_state, action=action, reason=reason, mark=mark,
                 row=manual_row())
        return app_session, core_state

    def assert_parity(self, app_session, core_state):
        for key in DECISION_KEYS:
            with self.subTest(key=key):
                self.assertEqual(app_session.get(key), core_state.get(key),
                                 f"{key} differs between adapter and core")

    def test_approve_parity(self):
        app_session, core_state = self.run_both("approve", "", None)
        self.assertEqual(app_session["question_review_notice"][0], "ok")
        self.assert_parity(app_session, core_state)

    def test_correct_parity(self):
        app_session, core_state = self.run_both(
            "correct", "misread the working", 2)
        self.assertEqual(app_session["question_review_notice"][0], "ok")
        self.assertEqual(app_session["teacher_overrides"]["review-1"]["1"], 2)
        self.assert_parity(app_session, core_state)

    def test_flag_parity(self):
        app_session, core_state = self.run_both(
            "flag", "answer unclear", None)
        self.assertEqual(app_session["question_review_notice"][0], "ok")
        self.assert_parity(app_session, core_state)


if __name__ == "__main__":
    unittest.main()
