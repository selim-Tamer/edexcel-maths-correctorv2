"""C6-B3 adversarial tests: duplicate question identity and retained evidence.

Offline only: every provider boundary is synthetic (plain listing dicts as the
production helpers return them, faked token/listing functions in the loaded
app scope, injected session state). No live Gemini/Classroom/Drive request is
made anywhere; the shared ``requests`` fake in the headless loader raises on
any unexpected HTTP call, so a passing run proves no network access happened.

Covered:
  - question identity: a number that matches more than one question never
    resolves (first-match binding is gone), including int/whitespace forms,
  - the three mutation entry points (decision binding, review decision,
    override write) refuse a colliding id and write nothing,
  - applying overrides and grading a Classroom row never touch a colliding
    question: pristine AI marks and totals are preserved, never repaired,
  - retained authorization evidence: exactly five whitelisted string fields,
    bound to actor/scope/revision, containing no token or credential, kept on
    the decision record and its audit event,
  - historical evidence is never reusable authorization: a later decision
    still re-verifies and fails closed on revocation or a changed assignment
    revision, whatever was stored earlier,
  - final approval/publication stays a separate gate from question review,
  - the documented session-only (non-durable) audit limitation.
"""
import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import question_review as qr
import teacher_authorization as ta
import test_c6b2_security as c6b2
from support import SOURCE, benchmark, load_app, model_question

SUBJECT = "teacher-a-subject"
OWNER = "google:" + SUBJECT
PDF = b"%PDF-1.7 c6b3 duplicate question identity fixture"


class CapturingUI:
    """Recording Streamlit surface for read-only render assertions."""

    def __init__(self, session_state):
        self.session_state = session_state
        self.blocks = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def columns(self, count):
        return [self] * int(count)

    def expander(self, label, **kwargs):
        self.blocks.append(("expander", label))
        return self

    def dataframe(self, frame, **kwargs):
        self.blocks.append(("dataframe", frame))

    def radio(self, label, options, **kwargs):
        self.blocks.append(("radio", label))
        return list(options)[0]

    def selectbox(self, label, options, **kwargs):
        self.blocks.append(("selectbox", label))
        self.blocks.append(("selectbox_options", list(options)))
        return list(options)[0]

    def number_input(self, label, **kwargs):
        return kwargs.get("value", 0)

    def text_input(self, *args, **kwargs):
        return ""

    def button(self, *args, **kwargs):
        self.blocks.append(("button", args[0] if args else ""))
        return False

    def metric(self, *args, **kwargs):
        return None

    def caption(self, text, **kwargs):
        self.blocks.append(("caption", str(text)))

    def warning(self, text, **kwargs):
        self.blocks.append(("warning", str(text)))

    def error(self, text, **kwargs):
        self.blocks.append(("error", str(text)))

    def success(self, text, **kwargs):
        self.blocks.append(("success", str(text)))

    def info(self, text, **kwargs):
        self.blocks.append(("info", str(text)))

    def markdown(self, text, **kwargs):
        self.blocks.append(("markdown", str(text)))

    def subheader(self, text, **kwargs):
        self.blocks.append(("subheader", str(text)))

    def write(self, *args, **kwargs):
        self.blocks.append(("write", args[0] if args else ""))

    def code(self, *args, **kwargs):
        self.blocks.append(("code", ""))

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def sample_question(number="1", awarded=1, maximum=2):
    sample = model_question(benchmark()["questions"][0], "partial")
    sample["question_number"] = number
    sample["awarded_marks"] = awarded
    sample["max_marks"] = maximum
    return sample


class QuestionIdentityResolutionTests(unittest.TestCase):
    """Question identity must fail closed on any collision."""

    def test_duplicate_ids_never_resolve_to_a_question(self):
        questions = [sample_question("1"), sample_question("1", awarded=0)]
        question, code = qr.resolve_question(questions, "1")
        self.assertIsNone(question)
        self.assertEqual(code, qr.AMBIGUOUS_QUESTION_CODE)
        self.assertEqual(qr.ambiguous_question_numbers(questions), ["1"])
        self.assertTrue(qr.question_is_ambiguous(questions, "1"))

    def test_identity_uses_the_same_string_forms_the_app_looks_up(self):
        questions = [sample_question("1"), sample_question("10"),
                     sample_question("2")]
        self.assertEqual(qr.resolve_question(questions, "1")[1],
                         qr.QUESTION_IDENTITY_OK)
        self.assertEqual(qr.ambiguous_question_numbers(questions), [])
        # int and whitespace variants name the SAME question id, so they
        # collide rather than quietly resolving to whichever matched first.
        for variant in (1, " 1", "1 "):
            with self.subTest(variant=variant):
                self.assertTrue(qr.question_is_ambiguous(
                    questions + [sample_question(variant)], "1"))
        self.assertEqual(qr.question_number_key(None), "None")

    def test_missing_unusable_and_empty_ids_fail_closed(self):
        for questions in (None, "junk", {}, 5, ()):
            with self.subTest(questions=questions):
                question, code = qr.resolve_question(questions, "1")
                self.assertIsNone(question)
                self.assertEqual(code, qr.UNAVAILABLE_QUESTION_CODE)
                self.assertEqual(qr.ambiguous_question_numbers(questions), [])
        unique, code = qr.resolve_question([sample_question("1")], "1")
        self.assertEqual(code, qr.QUESTION_IDENTITY_OK)
        self.assertIsInstance(unique, dict)
        question, code = qr.resolve_question([sample_question("1")], "2")
        self.assertIsNone(question)
        self.assertEqual(code, qr.UNAVAILABLE_QUESTION_CODE)
        # Two questions without a usable number still collide on "".
        self.assertEqual(
            qr.ambiguous_question_numbers(
                [{"question_number": ""}, {}, "not-a-question",
                 {"question_number": ""}]),
            [""])

    def test_only_true_collisions_are_reported(self):
        questions = [sample_question("1"), sample_question("1"),
                     sample_question("2"), sample_question("1"),
                     sample_question("Q1")]
        self.assertEqual(qr.ambiguous_question_numbers(questions), ["1"])
        self.assertFalse(qr.question_is_ambiguous(questions, "2"))
        self.assertFalse(qr.question_is_ambiguous(questions, "Q1"))


class AuthorizationEvidenceTests(unittest.TestCase):
    """Retained evidence is a fixed, bounded, audit-only record."""

    def test_evidence_is_five_bounded_string_fields(self):
        evidence = qr.authorization_evidence(
            scope="classroom", actor=OWNER, authorization_revision="a" * 64,
            row_revision="rev-1", verified_at=1_700_000_000.0)
        self.assertEqual(set(evidence), set(qr.AUTHORIZATION_EVIDENCE_FIELDS))
        self.assertTrue(all(isinstance(value, str)
                            for value in evidence.values()))
        self.assertEqual(evidence["scope"], "classroom")
        self.assertEqual(evidence["verified_at"], "1700000000.0")

    def test_unknown_scope_and_oversized_values_cannot_widen_the_record(self):
        for scope in ("student", "", None, 5, ["classroom"], {"scope": "x"}):
            with self.subTest(scope=scope):
                evidence = qr.authorization_evidence(scope=scope, actor=OWNER)
                self.assertEqual(evidence["scope"], "unscoped")
        oversized = "x" * (qr.EVIDENCE_FIELD_LIMIT * 4)
        evidence = qr.authorization_evidence(
            scope="manual", actor=oversized, authorization_revision=oversized,
            row_revision=oversized, verified_at=oversized)
        self.assertTrue(all(len(value) <= qr.EVIDENCE_FIELD_LIMIT
                            for value in evidence.values()))

    def test_audit_event_whitelists_supplied_evidence(self):
        event = qr.make_review_event(
            assignment_id="manual", student_id="review-1",
            question_number="1", action="approved", actor=OWNER,
            binding="b" * 64, timestamp="1",
            evidence={"scope": "classroom", "actor": OWNER,
                      "authorization_revision": "a" * 64,
                      "token": "synthetic-token", "student_email": "x@y.z"})
        stored = event["authorization_evidence"]
        self.assertNotIn("token", stored)
        self.assertNotIn("student_email", stored)
        self.assertEqual(set(stored), set(qr.AUTHORIZATION_EVIDENCE_FIELDS))
        for junk in (None, "evidence", 5, [], object()):
            with self.subTest(junk=junk):
                other = qr.make_review_event(
                    assignment_id="manual", student_id="review-1",
                    question_number="1", action="approved", actor=OWNER,
                    binding="b" * 64, timestamp="1", evidence=junk)
                self.assertIsNone(other["authorization_evidence"])
        # Events built without evidence stay None, never an empty dict guess.
        plain = qr.make_review_event(
            assignment_id="manual", student_id="review-1", question_number="1",
            action="approved", actor=OWNER, binding="b" * 64, timestamp="1")
        self.assertIsNone(plain["authorization_evidence"])

    def test_decisions_and_events_are_session_only_state(self):
        # Documented limitation: the stores are session-scoped, in-memory and
        # bounded. Nothing here reaches a durable audit store (persistence is
        # a separate, undecided scope).
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn('"question_review_decisions"', source)
        self.assertIn("st.session_state.setdefault(\"question_review_events\"",
                      source)
        events = [qr.make_review_event(
            assignment_id="manual", student_id="review-1",
            question_number=str(index), action="approved", actor=OWNER,
            binding="b" * 64, timestamp=str(index))
            for index in range(qr.MAX_AUDIT_EVENTS + 10)]
        self.assertEqual(len(qr.bounded_events(events)), qr.MAX_AUDIT_EVENTS)


class IdentityBoundaryTests(unittest.TestCase):
    """The three mutation entry points refuse a colliding question id."""

    def setUp(self):
        self.f = load_app()
        self.session = {}
        self.f["st"] = SimpleNamespace(
            session_state=self.session, user=SimpleNamespace(sub=SUBJECT))
        self.f["google_logged_in"] = lambda: True
        self.f["authenticated_google_identity"] = lambda: SUBJECT
        self.f["pd"] = SimpleNamespace(DataFrame=lambda rows: rows)
        self.f["remember_manual_upload"](PDF)
        self.session["last_manual_context"] = hashlib.sha256(PDF).hexdigest()
        self.row = self.row_for(["1"])

    def row_for(self, numbers, review_key="review-1"):
        result = self.f["normalise_result"](
            {"questions": [sample_question(number) for number in numbers]})
        earned, available, percentage = self.f["calculate_totals"](result)
        return {"status": "Marked", "review_key": review_key,
                "report_owner": OWNER, "result": copy.deepcopy(result),
                "ai_result": copy.deepcopy(result),
                "totals": {"earned": earned, "available": available,
                           "percentage": percentage,
                           "provisional_grade":
                               self.f["percentage_band_grade"](percentage)},
                "groups": self.f["make_group_rows"](result["questions"]),
                "student_name": "Synthetic", "marking_model": "fake",
                "audit_model": "Not run"}

    def install(self, row):
        self.session["last_batch_results"] = [row]
        return row

    def decision(self, student_key="review-1", question_number="1",
                 action="approve", reason="", mark=None):
        self.f["apply_question_review_decision"](
            student_key, question_number, action, reason, mark)
        return self.session.get("question_review_notice")

    def stored(self, key="review-1::q1"):
        return self.session.get("question_review_decisions", {}).get(key)

    def assert_nothing_written(self, key="review-1::q1"):
        self.assertIsNone(self.stored(key))
        self.assertNotIn("question_review_decisions", self.session)
        self.assertNotIn("question_review_events", self.session)
        self.assertNotIn("teacher_overrides", self.session)

    def test_duplicate_id_refuses_the_decision_and_writes_nothing(self):
        row = self.install(self.row_for(["1", "1"]))
        for action, mark in (("approve", None), ("correct", 2),
                             ("flag", None)):
            with self.subTest(action=action):
                notice = self.decision(action=action, mark=mark,
                                       reason="duplicate id")
                self.assertEqual(notice[0], "review_question_ambiguous")
                self.assert_nothing_written()
        # Nothing was repaired or changed on either colliding question.
        self.assertEqual(
            [q["awarded_marks"] for q in row["ai_result"]["questions"]], [1, 1])
        self.assertEqual(
            [q["awarded_marks"] for q in row["result"]["questions"]], [1, 1])

    def test_unique_ids_in_a_colliding_row_still_work(self):
        self.install(self.row_for(["1", "1", "2"]))
        self.assertEqual(self.decision(question_number="2")[0], "ok")
        self.assertEqual(self.stored("review-1::q2")["status"], "approved")
        self.assertEqual(self.decision(question_number="1")[0],
                         "review_question_ambiguous")
        # The unique decision is the only one recorded.
        self.assertEqual(
            sorted(self.session["question_review_decisions"]), ["review-1::q2"])

    def test_missing_question_id_still_reports_unavailable(self):
        self.install(self.row_for(["1"]))
        self.assertEqual(self.decision(question_number="9")[0],
                         "review_question_unavailable")

    def test_duplicate_id_refuses_the_override_mutation(self):
        self.install(self.row_for(["1", "1", "2"]))
        self.f["save_teacher_override"]("review-1", "1", 2, "collision")
        self.assert_nothing_written()
        self.assertEqual(
            self.session["teacher_override_notice"][0],
            qr.AMBIGUOUS_QUESTION_CODE)
        # The widget callback path is refused too (it reads scalar snapshots).
        self.session["override_mark_review-1_1"] = 2
        self.session["override_reason_review-1_1"] = "collision"
        self.f["save_teacher_override_from_widgets"]("review-1", "1")
        self.assertNotIn("review-1", self.session.get("teacher_overrides", {}))
        # An id that resolves to one question still writes.
        self.f["save_teacher_override"]("review-1", "2", 2, "unique id")
        self.assertEqual(self.session["teacher_overrides"]["review-1"], {"2": 2})

    def test_override_without_a_stored_row_is_still_refused(self):
        # Ownership guard first: no registered row means no write at all.
        self.f["save_teacher_override"]("review-missing", "1", 2, "unowned")
        self.assertNotIn("teacher_overrides", self.session)

    def test_colliding_override_is_never_applied_to_marks_or_totals(self):
        row = self.install(self.row_for(["1", "1", "2"]))
        self.session["teacher_overrides"] = {
            "review-1": {"1": 2, "2": 2}}
        self.session["teacher_override_reasons"] = {
            "review-1": {"1": "collision", "2": "unique id"}}
        changed = self.f["apply_teacher_overrides"]("review-1", row["result"])
        self.assertTrue(changed)  # the unique id was applied
        applied = row["result"]["questions"]
        self.assertEqual([q["awarded_marks"] for q in applied], [1, 1, 2])
        self.assertNotIn("teacher_override", applied[0])
        self.assertNotIn("teacher_override", applied[1])
        self.assertEqual(applied[2]["teacher_override"]["original_marks"], 1)
        # Pristine AI marks stay untouched and totals are recomputed honestly.
        self.assertEqual(
            [q["awarded_marks"] for q in row["ai_result"]["questions"]],
            [1, 1, 1])
        self.assertEqual(self.f["calculate_totals"](row["result"])[:2], (4, 6))
        prepared = copy.deepcopy(row)
        self.f["prepare_review_row"](prepared)
        self.assertEqual(prepared["totals"]["earned"], 4)

    def test_classroom_totals_never_count_a_colliding_override(self):
        row = self.row_for(["1", "1"])
        row.update({"classroom_max_points": 100,
                    "homework_question_count": 2, "selected_questions": None})
        self.session["teacher_overrides"] = {row["review_key"]: {"1": 2}}
        details = self.f["classroom_grade_details"](row)
        self.assertEqual(details["score"], "2/4")
        self.assertFalse(details["has_overrides"])
        # Control: an id that resolves to one question is counted.
        other = self.row_for(["1", "2"], review_key="review-2")
        other.update({"classroom_max_points": 100,
                      "homework_question_count": 2,
                      "selected_questions": None})
        self.session["teacher_overrides"] = {"review-2": {"2": 2}}
        details = self.f["classroom_grade_details"](other)
        self.assertEqual(details["score"], "3/4")
        self.assertTrue(details["has_overrides"])

    def test_duplicate_id_renders_no_decision_controls(self):
        row = self.install(self.row_for(["1", "1"]))
        self.session["teacher_override_notice"] = (
            qr.AMBIGUOUS_QUESTION_CODE, "1")
        ui = CapturingUI(self.session)
        self.f["st"] = ui
        self.f["render_student_report"](
            "Synthetic", row["result"], row["totals"], row["groups"],
            "fake", "Not run", student_key=row["review_key"], review_row=row)
        self.assertEqual(
            [block for block in ui.blocks if block[0] == "button"
             and block[1].startswith("Submit question review")], [])
        warnings = [text for kind, text in ui.blocks if kind == "warning"]
        self.assertTrue(any("more than once" in text for text in warnings))
        captions = [text for kind, text in ui.blocks if kind == "caption"]
        self.assertTrue(any("Overrides are blocked" in text
                            for text in captions))
        # The colliding id is offered once (no second, look-alike entry).
        self.assertIn(("selectbox_options", ["1"]),
                      [block for block in ui.blocks
                       if block[0] == "selectbox_options"])
        errors = [text for kind, text in ui.blocks if kind == "error"]
        self.assertTrue(any("No marks and no totals were changed" in text
                            for text in errors))
        statuses = [
            item.get("Review status")
            for kind, payload in ui.blocks if kind == "dataframe"
            and isinstance(payload, list) for item in payload
            if isinstance(item, dict) and "Review status" in item]
        self.assertIn("Blocked (duplicate question number)", statuses)
        # Read-only rendering made no provider request (the fake would raise).
        self.assertEqual(self.session.get("question_review_decisions", {}), {})

    def test_classroom_duplicate_id_is_refused_after_authorization(self):
        self.enable_classroom()
        row = self.install(self.classroom_row(["1", "1"]))
        notice = self.decision(student_key=row["review_key"])
        self.assertEqual(notice[0], "review_question_ambiguous")
        self.assert_nothing_written("classroom_row-1::q1")
        # Authorization still ran first and was not reused for the decision.
        self.assertEqual(self.courses.call_count, 1)
        self.assertEqual(self.assignments.call_count, 1)

    def enable_classroom(self):
        self.courses = Mock(return_value=list(c6b2.COURSES))
        self.assignments = Mock(return_value=list(c6b2.ASSIGNMENTS))
        self.f["list_teacher_courses"] = self.courses
        self.f["list_assignments"] = self.assignments
        self.f["get_google_access_token"] = lambda: "synthetic-token"
        self.f["record_classroom_grants"](
            list(c6b2.COURSES), list(c6b2.ASSIGNMENTS), "course-1")

    def classroom_row(self, numbers=("1",), review_key="classroom_row-1",
                      revision="rev-1", context="c" * 64):
        row = self.row_for(list(numbers), review_key=review_key)
        row.update({"classroom_course_id": "course-1",
                    "classroom_assignment_id": "work-1",
                    "classroom_context": context, "revision": revision,
                    "selected_questions": list(numbers),
                    "classroom_max_points": 100,
                    "homework_question_count": len(numbers)})
        return row


class RetainedEvidenceTests(unittest.TestCase):
    """Evidence of the gate, never a replacement for the gate."""

    def setUp(self):
        self.f = load_app()
        self.session = {}
        self.f["st"] = SimpleNamespace(
            session_state=self.session, user=SimpleNamespace(sub=SUBJECT))
        self.f["google_logged_in"] = lambda: True
        self.f["authenticated_google_identity"] = lambda: SUBJECT
        self.f["pd"] = SimpleNamespace(DataFrame=lambda rows: rows)
        self.f["remember_manual_upload"](PDF)
        self.session["last_manual_context"] = hashlib.sha256(PDF).hexdigest()

    def row_for(self, numbers, review_key="review-1"):
        result = self.f["normalise_result"](
            {"questions": [sample_question(number) for number in numbers]})
        return {"status": "Marked", "review_key": review_key,
                "report_owner": OWNER, "result": copy.deepcopy(result),
                "ai_result": copy.deepcopy(result),
                "totals": {"earned": 1, "available": 2,
                           "percentage": 50.0, "provisional_grade": "5"},
                "groups": [], "student_name": "Synthetic",
                "marking_model": "fake", "audit_model": "Not run"}

    def classroom_row(self, numbers=("1", "2"), review_key="classroom_row-1",
                      revision="rev-1", context="c" * 64):
        row = self.row_for(list(numbers), review_key=review_key)
        row.update({"classroom_course_id": "course-1",
                    "classroom_assignment_id": "work-1",
                    "classroom_context": context, "revision": revision,
                    "selected_questions": list(numbers),
                    "classroom_max_points": 100,
                    "homework_question_count": len(numbers)})
        return row

    def install(self, row):
        self.session["last_batch_results"] = [row]
        return row

    def enable_classroom(self):
        self.courses = Mock(return_value=list(c6b2.COURSES))
        self.assignments = Mock(return_value=list(c6b2.ASSIGNMENTS))
        self.f["list_teacher_courses"] = self.courses
        self.f["list_assignments"] = self.assignments
        self.f["get_google_access_token"] = lambda: "synthetic-token"
        self.f["record_classroom_grants"](
            list(c6b2.COURSES), list(c6b2.ASSIGNMENTS), "course-1")

    def decision(self, student_key, question_number="1", action="approve",
                 reason="", mark=None):
        self.f["apply_question_review_decision"](
            student_key, question_number, action, reason, mark)
        return self.session.get("question_review_notice")

    def stored(self, key):
        return self.session.get("question_review_decisions", {}).get(key)

    def serialized_session(self):
        return (json.dumps(self.session.get("question_review_decisions", {}))
                + json.dumps(self.session.get("question_review_events", [])))

    def test_classroom_decision_retains_fresh_reverification_evidence(self):
        self.enable_classroom()
        row = self.install(self.classroom_row(("1",)))
        self.assertEqual(self.decision(row["review_key"])[0], "ok")
        record = self.stored("classroom_row-1::q1")
        evidence = record["authorization_evidence"]
        self.assertEqual(set(evidence), set(qr.AUTHORIZATION_EVIDENCE_FIELDS))
        self.assertEqual(evidence["scope"], "classroom")
        self.assertEqual(evidence["actor"], OWNER)
        self.assertEqual(evidence["row_revision"], "rev-1")
        # The revision retained is the one freshly computed from live listings.
        self.assertEqual(
            evidence["authorization_revision"],
            ta.assignment_revision("course-1", c6b2.assignment("work-1")))
        self.assertEqual(len(evidence["authorization_revision"]), 64)
        self.assertTrue(evidence["verified_at"])
        event = self.session["question_review_events"][-1]
        self.assertEqual(event["authorization_evidence"], evidence)
        self.assertEqual(event["actor"], OWNER)
        # No token, credential or PII field is stored anywhere in the trail.
        blob = self.serialized_session()
        self.assertNotIn("synthetic-token", blob)
        self.assertNotIn("access_token", blob)

    def test_manual_decision_retains_owned_document_evidence(self):
        row = self.install(self.row_for(["1"]))
        self.assertEqual(self.decision(row["review_key"])[0], "ok")
        evidence = self.stored("review-1::q1")["authorization_evidence"]
        self.assertEqual(evidence["scope"], "manual")
        self.assertEqual(evidence["actor"], OWNER)
        # Server-derived digest of the owned upload, never a caller string.
        self.assertEqual(evidence["authorization_revision"],
                         hashlib.sha256(PDF).hexdigest())
        self.assertEqual(evidence["authorization_revision"],
                         self.session["last_manual_context"])
        self.assertTrue(evidence["verified_at"])
        self.assertNotIn("synthetic-token", self.serialized_session())
        event = self.session["question_review_events"][-1]
        self.assertEqual(event["authorization_evidence"], evidence)

    def test_historical_evidence_never_authorizes_a_new_decision(self):
        self.enable_classroom()
        row = self.install(self.classroom_row(("1", "2")))
        self.assertEqual(self.decision(row["review_key"], "1")[0], "ok")
        self.assertEqual(len(self.session["question_review_events"]), 1)
        recorded = copy.deepcopy(self.stored("classroom_row-1::q1"))
        self.assertEqual(recorded["authorization_evidence"]["scope"],
                         "classroom")
        # Provider-side revocation inside the cached-grant window.
        self.courses.return_value = [c6b2.COURSES[1]]
        # Tamper with the stored evidence: it must still authorize nothing.
        self.session["question_review_decisions"]["classroom_row-1::q1"] = (
            qr.authorization_evidence(scope="classroom", actor=OWNER,
                                      authorization_revision="f" * 64,
                                      row_revision="rev-1",
                                      verified_at="1700000000"))
        notice = self.decision(row["review_key"], "2")
        self.assertEqual(notice[0], "course_access_unverified")
        self.assertIsNone(self.stored("classroom_row-1::q2"))
        # Exactly one event: the failed attempt recorded nothing at all.
        self.assertEqual(len(self.session["question_review_events"]), 1)
        self.assertEqual(self.session.get("teacher_overrides", {}), {})
        # The valid first record itself was left untouched for audit.
        self.session["question_review_decisions"]["classroom_row-1::q1"] = (
            recorded)
        self.assertEqual(self.stored("classroom_row-1::q1")["status"],
                         "approved")

    def test_changed_assignment_revision_refuses_the_decision(self):
        self.enable_classroom()
        row = self.install(self.classroom_row(("1", "2")))
        self.assignments.return_value = [
            c6b2.assignment("work-1", title="Edited after verification")]
        notice = self.decision(row["review_key"], "1")
        self.assertEqual(notice[0], "assignment_revision_changed")
        self.assertIsNone(self.stored("classroom_row-1::q1"))
        self.assertNotIn("question_review_events", self.session)
        self.assertEqual(self.session.get("teacher_overrides", {}), {})

    def test_lost_manual_provenance_refuses_the_decision(self):
        row = self.install(self.row_for(["1"]))
        self.session["last_manual_context"] = hashlib.sha256(
            b"other document").hexdigest()
        self.assertEqual(self.decision(row["review_key"])[0],
                         "manual_document_provenance_required")
        self.assertIsNone(self.stored("review-1::q1"))
        self.assertNotIn("question_review_events", self.session)

    def test_final_approval_and_publication_stay_separately_gated(self):
        self.enable_classroom()
        row = self.install(self.classroom_row(("1",)))
        key = row["review_key"]
        self.session["classroom_grade_approvals"] = {key: True}
        self.session[f"classroom_approval_checkbox_{key}"] = True
        # Approving one question neither publishes nor clears the approval.
        self.assertEqual(self.decision(key, "1", action="approve")[0], "ok")
        self.assertTrue(self.session["classroom_grade_approvals"][key])
        # A correction invalidates the existing approval for this result.
        notice = self.decision(key, "1", action="correct",
                               reason="scheme B1 missed", mark=2)
        self.assertEqual(notice[0], "ok")
        self.assertNotIn(key, self.session["classroom_grade_approvals"])
        self.assertFalse(self.session[f"classroom_approval_checkbox_{key}"])
        # Publication still requires its own, separately gated action.
        self.assertTrue(self.f["owned_review_key"](key))
        self.session[f"classroom_approval_checkbox_{key}"] = True
        self.f["save_classroom_approval"](key)
        self.assertTrue(self.session["classroom_grade_approvals"][key])
        # Approving the question again leaves publication state alone.
        self.assertEqual(self.decision(key, "1", action="approve",
                                       reason="re-checked")[0], "ok")
        self.assertTrue(self.session["classroom_grade_approvals"][key])

    def test_ambiguous_decision_never_touches_publication_state(self):
        self.enable_classroom()
        row = self.install(self.classroom_row(("1", "1")))
        self.session["classroom_grade_approvals"] = {row["review_key"]: True}
        self.assertEqual(self.decision(row["review_key"], "1")[0],
                         "review_question_ambiguous")
        self.assertTrue(
            self.session["classroom_grade_approvals"][row["review_key"]])
        self.assertNotIn("question_review_events", self.session)


if __name__ == "__main__":
    unittest.main()
