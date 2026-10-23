"""Session-independent question-review decision core (M5 implementation slice).

WHY THIS MODULE EXISTS
----------------------
The Streamlit application drives a teacher's approve / correct / flag decision
through ``app5_1_automated.apply_question_review_decision``.  That function
mixed two concerns:

* resolving *session* state (who is signed in, which row is visible, what the
  session holds), and
* the decision policy itself (fail-closed gates, evidence binding, atomic
  audit commit, rollback, notice).

A FastAPI worker cannot reuse the first concern but must reuse the second
identically.  This module therefore contains ONLY the policy: it never
imports ``streamlit`` and never reads ``st.session_state``.  Everything
session-, provider- or widget-bound arrives as an explicit ``DecisionEnv``
of callables plus the plain ``state`` mapping the caller owns (the app passes
its ``st.session_state``; a worker passes its own store-backed mapping).

GUARANTEES
----------
* The gate order, failure codes, evidence shape, binding rules, override
  write/verify, single-transaction audit commit and rollback are the ones the
  application shipped -- moved verbatim, not re-implemented from memory.
* ``state`` is treated as an opaque mutable mapping: only ``get``/``set``/
  ``setdefault``/``pop``/``[...]`` are used, so a Streamlit session state and
  a plain dict behave identically.
* Nothing here authenticates: the caller supplies the already-verified
  subject/owner, and every gate still fails closed on a missing or untrusted
  value.

Status: implemented behaviour, offline-tested (tests/test_decision_core.py).
No endpoint serves this module yet; FastAPI wiring, persistence adapter and
tenant authorization are NOT part of this slice.
"""
from __future__ import annotations

import json
from typing import Any, Callable, MutableMapping, Optional

import audit_store
import question_review

State = MutableMapping[str, Any]

# The decision records live in the caller's mapping under this key (the app
# passes st.session_state, so behaviour is unchanged). The notice key is
# caller-owned: the application adapter clears the previous
# ``question_review_notice`` before delegating and receives every result
# through ``env.fail``; the bounded event trail likewise stays with
# ``env.record_event``.
DECISIONS_KEY = "question_review_decisions"


class DecisionEnv:
    """Everything the core cannot derive itself, resolved by the caller.

    All callables are bound by the adapter in the application scope, so a
    test can substitute fakes and a FastAPI worker can substitute its own
    authenticated context without this module knowing about either.

    ``fail`` and ``record_event`` are side-effect sinks: the application
    passes its session-scoped notice writer and bounded event trail, a worker
    would pass an HTTP error mapper and its own event sink. The core itself
    only writes the decision record and the rollback-protected override state
    through the ``state`` mapping it is given.
    """

    def __init__(
        self,
        *,
        subject: Optional[str],
        owner: Optional[str],
        resolve_row: Callable[[], Optional[dict]],
        row_owner_allowed: Callable[[dict], bool],
        manual_context: str,
        reverify: Callable[[dict], tuple[Optional[dict], Optional[str]]],
        manual_provenance_ok: Callable[[dict], bool],
        plan_resolver: Callable[[dict, str], tuple[Any, Optional[dict], Optional[str]]],
        write_override: Callable[[str, str, int, str], None],
        fail: Callable[..., None],
        record_event: Callable[..., None],
        now: Callable[[], str],
    ):
        self.subject = subject
        self.owner = owner
        self.resolve_row = resolve_row
        self.row_owner_allowed = row_owner_allowed
        self.manual_context = manual_context
        self.reverify = reverify
        self.manual_provenance_ok = manual_provenance_ok
        self.plan_resolver = plan_resolver
        self.write_override = write_override
        self.fail = fail
        self.record_event = record_event
        self.now = now


def row_review_scope(row: Any) -> Optional[str]:
    """Classroom rows carry server-bound assignment ids; everything else manual."""
    if not isinstance(row, dict):
        return None
    if (isinstance(row.get("classroom_course_id"), str)
            and row.get("classroom_course_id")
            and isinstance(row.get("classroom_assignment_id"), str)
            and row.get("classroom_assignment_id")):
        return "classroom"
    return "manual"


def evidence_digest(row: Any, manual_context: Any) -> str:
    """Digest of the marking evidence (mark scheme + assignment) behind a row.

    Classroom rows carry the job context digest (assignment, scheme bytes and
    marking settings); manual rows fall back to the server-derived document
    digest recorded at upload time -- supplied here explicitly by the caller
    instead of being read from a session.  No browser-supplied value is ever
    used.
    """
    if not isinstance(row, dict):
        return ""
    for value in (row.get("classroom_context"), row.get("snapshot_context"),
                  manual_context):
        if isinstance(value, str) and value:
            return value
    return ""


def pristine_question_list(row: Any) -> list:
    """The row's pristine AI questions (never the override-adjusted display)."""
    if not isinstance(row, dict):
        return []
    pristine = row.get("ai_result") or row.get("result") or {}
    if not isinstance(pristine, dict):
        return []
    questions = pristine.get("questions", [])
    return questions if isinstance(questions, list) else []


def question_identity_status(row: Any, question_number: Any) -> tuple:
    """Resolve one question id in the pristine output as ``(question, code)``."""
    return question_review.resolve_question(
        pristine_question_list(row), question_number)


def question_review_binding(student_key: str, row: Any, question_number: Any,
                            manual_context: Any) -> Any:
    """Bind one question decision to the exact evidence it reviewed.

    Identical rules to the application path: pristine AI marks, marking
    evidence digest, row revision and selected-question set.  Returns None
    when trustworthy inputs are missing or the question id collides.
    """
    if not isinstance(row, dict) or row.get("review_key") != student_key:
        return None
    question, identity = question_identity_status(row, question_number)
    if identity != question_review.QUESTION_IDENTITY_OK:
        return None
    if question is None or question.get("awarded_marks") is None:
        return None
    digest = evidence_digest(row, manual_context)
    if not digest:
        return None
    selection = row.get("selected_questions")
    scope = row_review_scope(row) or "manual"
    assignment = (str(row.get("classroom_course_id")) + "/"
                  + str(row.get("classroom_assignment_id"))
                  if scope == "classroom" else "manual")
    return question_review.question_review_binding(
        assignment_id=assignment,
        student_id=str(student_key),
        question_number=str(question_number),
        variant=json.dumps(selection, sort_keys=True) if selection else "",
        row_revision=str(row.get("revision") or ""),
        ai_marks=[question.get("awarded_marks")],
        scheme_digest=digest,
    )


def decision_key(student_key: str, question_number: Any) -> str:
    return str(student_key) + "::q" + str(question_number)


def durable_result_identity(row: Any, manual_context: Any) -> Optional[str]:
    """Owner-scoped storage identity for one reviewed result (fail closed).

    Unchanged semantics: Classroom rows key on the provider-stable
    course/assignment/submission triple plus submission revision; manual rows
    key on the server-derived document digest plus the per-run review key.
    """
    if not isinstance(row, dict):
        return None
    if row_review_scope(row) == "classroom":
        course = row.get("classroom_course_id")
        assignment = row.get("classroom_assignment_id")
        submission = row.get("submission_id")
        if not (isinstance(course, str) and course
                and isinstance(assignment, str) and assignment
                and isinstance(submission, str) and submission):
            return None
        revision = row.get("revision")
        revision = (str(revision) if isinstance(revision, str) and revision
                    else "unversioned")
        return ("classroom:" + course + ":" + assignment + ":"
                + submission + ":" + revision)
    digest = evidence_digest(row, manual_context)
    key = row.get("review_key")
    if not (isinstance(digest, str) and digest
            and isinstance(key, str) and key):
        return None
    return "manual:" + digest[:16] + ":" + key


def decision_session_snapshot(state: State, student_key: str,
                              question_number: Any) -> dict:
    """Remember the state one correction is about to change (rollback base)."""
    overrides = state.get("teacher_overrides", {})
    reasons = state.get("teacher_override_reasons", {})
    approvals = state.get("classroom_grade_approvals", {})
    target = str(question_number)
    entry = overrides.get(student_key, {}) if isinstance(overrides, dict) else {}
    reasons_entry = (reasons.get(student_key, {})
                     if isinstance(reasons, dict) else {})
    return {
        "override_present": isinstance(entry, dict) and target in entry,
        "override_value": entry.get(target) if isinstance(entry, dict) else None,
        "override_reason": (reasons_entry.get(target)
                            if isinstance(reasons_entry, dict) else None),
        "approval_present": (student_key in approvals
                             if isinstance(approvals, dict) else False),
        "approval_value": (approvals.get(student_key)
                           if isinstance(approvals, dict) else None),
        "approval_checkbox": state.get(
            f"classroom_approval_checkbox_{student_key}"),
    }


def restore_decision_session_snapshot(state: State, student_key: str,
                                      question_number: Any,
                                      snapshot: dict) -> None:
    """Undo a correction's state writes when its durable commit failed."""
    overrides = state.get("teacher_overrides")
    reasons = state.get("teacher_override_reasons")
    approvals = state.get("classroom_grade_approvals")
    target = str(question_number)
    if (snapshot or {}).get("override_present"):
        if isinstance(overrides, dict):
            overrides.setdefault(student_key, {})[target] = snapshot.get(
                "override_value")
        if isinstance(reasons, dict):
            reasons.setdefault(student_key, {})[target] = snapshot.get(
                "override_reason")
    else:
        for store_map in (overrides, reasons):
            bucket = (store_map.get(student_key)
                      if isinstance(store_map, dict) else None)
            if isinstance(bucket, dict):
                bucket.pop(target, None)
                if not bucket:
                    store_map.pop(student_key, None)
    if isinstance(approvals, dict):
        if snapshot.get("approval_present"):
            approvals[student_key] = snapshot.get("approval_value")
        else:
            approvals.pop(student_key, None)
    checkbox_key = f"classroom_approval_checkbox_{student_key}"
    if (snapshot or {}).get("approval_checkbox") is None:
        state.pop(checkbox_key, None)
    else:
        state[checkbox_key] = snapshot["approval_checkbox"]


def commit_audit_decision(*, store, plan, actor, row, question_number, action,
                          review_status, old_score, corrected, reason, binding,
                          evidence):
    """Atomically commit one accepted decision together with its audit event.

    Same payload/idempotency construction as the application path: the
    audit-only authorization evidence rides on the event but never enters the
    payload, so a legitimate retry stays idempotent while the optimistic
    revision is enforced by the commit itself.
    """
    payload = {
        "question_number": str(question_number),
        "action": str(action),
        "review_status": str(review_status),
        "old_score": "" if old_score is None else str(old_score),
        "new_score": "" if corrected is None else str(corrected),
        "reason": (reason or "").strip(),
        "binding": binding or "",
    }
    event = {
        "event_type": "question_review_decision",
        "question_number": payload["question_number"],
        "action": payload["action"],
        "review_status": payload["review_status"],
        "old_score": payload["old_score"],
        "new_score": payload["new_score"],
        "reason": payload["reason"],
        "actor": actor,
        "binding": payload["binding"],
        "authorization_evidence": evidence,
    }
    store.register_result(owner=actor, result_id=plan["result_id"],
                          original_output=(row.get("ai_result")
                                           or row.get("result") or {}))
    return store.commit_correction(
        owner=actor, result_id=plan["result_id"],
        expected_revision=plan["expected_revision"],
        idempotency_key=audit_store.make_idempotency_key(
            owner=actor, result_id=plan["result_id"], payload=payload),
        payload=payload, event=event)


def apply_review_decision(state: State, *, student_key, question_number,
                          action, reason, corrected_mark, env: DecisionEnv) -> None:
    """Record one teacher's question-level review decision (fail closed).

    This is the moved body of ``app5_1_automated.
    apply_question_review_decision``: authenticated identity, owner-verified
    row, trusted document provenance for manual rows, fresh re-verification
    for Classroom rows, exact question identity, mark validation, evidence
    binding, revision-checked persistence plan, single-override write that is
    verified after writing, one-transaction audit commit with rollback, then
    the decision record, the caller's bounded event trail (``env.record_event``)
    and the caller's result notice (``env.fail``). Any previous notice is the
    caller's to clear; the application adapter does it before delegating.

    Every gate runs here, at the mutation boundary, and fails closed.  The
    caller supplies already-verified identity and session-scoped lookups
    through ``env``; nothing here trusts a browser-supplied value.
    """
    if env.subject is None:
        return env.fail("authentication_required", question_number)
    # Row resolution happens only AFTER the identity gate, in the same order
    # as the application path, so no session lookup runs for an anonymous
    # session.
    row = env.resolve_row()
    if (row is None or not env.row_owner_allowed(row)
            or row.get("review_key") != student_key):
        return env.fail("review_context_unavailable", question_number)
    scope = row_review_scope(row)
    actor = str(env.owner or "unauthenticated")
    if scope == "classroom":
        reverified, code = env.reverify(row)
        if code:
            return env.fail(code, question_number)
        evidence = question_review.authorization_evidence(
            scope="classroom", actor=actor,
            authorization_revision=(reverified or {}).get("revision", ""),
            row_revision=str(row.get("revision") or ""),
            verified_at=(reverified or {}).get("verified_at", ""))
    else:
        if not env.manual_provenance_ok(row):
            return env.fail("manual_document_provenance_required",
                              question_number)
        evidence = question_review.authorization_evidence(
            scope="manual", actor=actor,
            authorization_revision=evidence_digest(row, env.manual_context),
            row_revision=str(row.get("revision") or ""),
            verified_at=env.now())
    status = {"approve": "approved", "correct": "corrected",
              "flag": "flagged"}.get(action)
    if status is None:
        return env.fail("review_action_invalid", question_number)
    question, identity = question_identity_status(row, question_number)
    if identity != question_review.QUESTION_IDENTITY_OK:
        # Duplicate/colliding question id (or no such question): refuse the
        # decision instead of binding it to whichever question matched first.
        return env.fail(identity, question_number)
    corrected = None
    if status == "corrected":
        ok, code = question_review.validate_mark_value(
            corrected_mark, question.get("max_marks"))
        if not ok:
            return env.fail(code, question_number)
        corrected = int(corrected_mark)
    binding = question_review_binding(student_key, row, question_number,
                                      env.manual_context)
    if binding is None:
        return env.fail("review_binding_unavailable", question_number)
    key = decision_key(student_key, question_number)
    decisions = state.setdefault(DECISIONS_KEY, {})
    current = decisions.get(key)
    if not isinstance(current, dict):
        current = None
    code, new_status, evaluated = question_review.evaluate_question_review(
        status=status, binding=binding,
        corrected_marks=[corrected] if corrected is not None else None,
        reason=reason,
        prior_status=(current or {}).get("status"),
        prior_binding=(current or {}).get("binding"))
    if code != "ok":
        return env.fail(code, question_number)
    # Persistence plan (stream revision) is resolved BEFORE any write, so a
    # decision against a superseded revision is refused as stale.
    store, plan, plan_error = env.plan_resolver(row, actor)
    if plan_error is not None:
        return env.fail(plan_error, question_number)
    snapshot = None
    if status == "corrected":
        if store is not None:
            snapshot = decision_session_snapshot(state, student_key,
                                                 question_number)
        env.write_override(student_key, str(question_number), corrected,
                           (reason or "").strip())
        if state.get("teacher_overrides", {}).get(
                student_key, {}).get(str(question_number)) != corrected:
            return env.fail("override_write_rejected",
                              question_number)
    committed = None
    if store is not None:
        try:
            committed = commit_audit_decision(
                store=store, plan=plan, actor=actor, row=row,
                question_number=question_number, action=action,
                review_status=new_status,
                old_score=question.get("awarded_marks"),
                corrected=corrected, reason=reason, binding=binding,
                evidence=evidence)
        except audit_store.AuditStoreError as failure:
            if snapshot is not None:
                restore_decision_session_snapshot(state, student_key,
                                                  question_number, snapshot)
            return env.fail(failure.code, question_number)
        except Exception:
            if snapshot is not None:
                restore_decision_session_snapshot(state, student_key,
                                                  question_number, snapshot)
            return env.fail("audit_persist_failed", question_number)
    record = dict(evaluated)
    record["actor"] = actor
    record["updated_at"] = env.now()
    # Audit-only record of the gate that authorized THIS decision; no gate
    # reads it back, so it can never be replayed as authorization.
    record["authorization_evidence"] = evidence
    if committed is not None:
        record["audit_reference"] = {
            "result_id": plan["result_id"],
            "sequence": committed["sequence"],
            "event_id": committed["event_id"],
        }
    state.setdefault(DECISIONS_KEY, {})[key] = record
    env.record_event(row, student_key, question_number, status, binding,
                     evidence)
    env.fail("ok", question_number, status)
