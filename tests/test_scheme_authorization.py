"""C6-A synthetic adversarial tests against the real approval engine."""
import copy
import json
import unittest
from dataclasses import replace

import scheme_review as sr
from scheme_authorization import (
    ApprovalAuthority, AuthorizationContext, AuthorizationError, ServerIdentity,
)


def review(*, assignment="assignment-a", certainty="explicit", maximum=1):
    return sr.build_review(
        {"questions": [{"question_number": "1", "max_marks": maximum,
                        "criteria": [{"criterion_id": "b", "code": "B1", "marks": 1,
                                      "source_page": 1, "source_evidence": "B1 correct"}]}]},
        assignment_id=assignment,
        evidence_document={"page_count": 1, "pages": [
            {"page": 1, "text": "Question 1 B1 correct", "status": "extracted"}]},
        question_mapping={"matches": [{"question_id": "1", "certainty": certainty,
                                       "pages": [1], "references": []}], "unresolved": []})


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.identity = ServerIdentity("teacher-a", "session-a", "assignment-v1")
        self.authority = ApprovalAuthority(lambda assignment: (
            self.identity if assignment is not None or self.identity is None
            else replace(self.identity, assignment_revision=None)))
        self.context = self.authority.authorize("assignment-a")
        self.record = review()
        self.bind(self.record)

    def bind(self, record, context=None):
        sr.bind_scheme_review(record, context or self.context,
                              review_id="review-" + str(len(self.authority._reviews)),
                              document_digest="a" * 64)

    def approve(self, record=None, context=None):
        record = self.record if record is None else record
        return sr.approve_scheme_review(record, context or self.context, record["scheme_revision"])

    def assert_not_approved(self, record=None, context=None):
        record = self.record if record is None else record
        context = context or self.context
        self.assertNotEqual(sr.approval_state(record, context, record.get("scheme_revision"))[0], "approved")
        self.assertNotEqual(sr.determine_status(record, context), sr.STATUS_APPROVED)
        self.assertNotEqual(sr.review_summary(record, context)["status"], sr.STATUS_APPROVED)

    def test_anonymous_and_malformed_contexts_rejected(self):
        for value in (None, "", " ", "teacher-a", {}, [], 1, True):
            with self.subTest(value=repr(value)), self.assertRaises(sr.SchemeReviewApprovalError):
                sr.approve_scheme_review(self.record, value, self.record["scheme_revision"])

    def test_malformed_verified_identity_rejected(self):
        for owner in (None, "", "  ", " teacher-a", "a\nb", 1, {}, ["teacher-a"]):
            with self.subTest(owner=repr(owner)), self.assertRaises(AuthorizationError):
                ApprovalAuthority(lambda a: ServerIdentity(owner, "session")).authorize("assignment-a")

    def test_unverified_identity_representation_rejected(self):
        for identity in ("teacher-a", {"owner": "teacher-a"}, None, True):
            with self.subTest(identity=repr(identity)), self.assertRaises(AuthorizationError):
                ApprovalAuthority(lambda a: identity).authorize("assignment-a")

    def test_forged_context_rejected(self):
        forged = AuthorizationContext(self.authority, self.identity, "assignment-a")
        with self.assertRaises(sr.SchemeReviewApprovalError):
            self.approve(context=forged)

    def test_foreign_teacher_cannot_approve_or_resolve(self):
        self.identity = ServerIdentity("teacher-b", "session-b", "assignment-v1")
        foreign = self.authority.authorize("assignment-a")
        for context in (self.context, foreign):
            with self.subTest(context=context), self.assertRaises(sr.SchemeReviewApprovalError):
                self.approve(context=context)
            with self.assertRaises(sr.SchemeReviewApprovalError):
                sr.resolve_findings(self.record, ["any"], context)

    def test_missing_assignment_rejected_for_assignment_scope(self):
        with self.assertRaises(sr.SchemeReviewApprovalError):
            self.bind(review(assignment=None))

    def test_explicit_manual_scope_is_supported_without_fabricated_assignment(self):
        context = self.authority.authorize(None)
        record = review(assignment=None)
        self.bind(record, context)
        self.approve(record, context)
        self.assertEqual(sr.approval_state(record, context, record["scheme_revision"]), ("approved", None))

    def test_mismatched_revision_rejected_before_mutation(self):
        before = copy.deepcopy(self.record)
        with self.assertRaisesRegex(sr.SchemeReviewApprovalError, "revision_mismatch"):
            sr.approve_scheme_review(self.record, self.context, "wrong")
        self.assertEqual(before, self.record)

    def test_approval_is_idempotent_and_all_outputs_agree(self):
        self.approve()
        before = copy.deepcopy(self.record)
        self.approve()
        self.assertEqual(before, self.record)
        self.assertEqual(sr.approval_state(self.record, self.context, self.record["scheme_revision"]), ("approved", None))
        self.assertEqual(sr.determine_status(self.record, self.context), sr.STATUS_APPROVED)
        self.assertEqual(sr.review_summary(self.record, self.context)["status"], sr.STATUS_APPROVED)

    def test_no_context_never_displays_historical_metadata_as_approval(self):
        self.approve()
        self.assertNotEqual(sr.determine_status(self.record), sr.STATUS_APPROVED)
        summary = sr.review_summary(self.record)
        self.assertNotEqual(summary["status"], sr.STATUS_APPROVED)
        self.assertTrue(summary["historical_approval"])

    def test_mutations_invalidate_every_effective_status(self):
        self.approve()
        mutations = {
            "revision": lambda r: r.update(scheme_revision="changed"),
            "assignment": lambda r: r.update(assignment_id="other"),
            "owner": lambda r: r["approval_binding"].update(owner="teacher-b"),
            "digest": lambda r: r["approval_binding"].update(document_digest="b" * 64),
            "review_id": lambda r: r["approval_binding"].update(review_id="new"),
            "scheme": lambda r: r["extracted"]["questions"][0].update(max_marks=2),
            "raw_scheme": lambda r: r["approval_inputs"]["interpretation"].update(unseen="new"),
            "evidence": lambda r: r["approval_inputs"]["evidence_document"]["pages"][0].update(text="changed"),
            "mapping": lambda r: r["approval_inputs"]["question_mapping"].update(unresolved=["1"]),
            "inventory": lambda r: r["approval_inputs"].update(question_numbers=["1", "2"]),
            "findings": lambda r: r["verified"]["findings"].append(sr._finding("TOTAL_MISMATCH", "test")),
            "assisted": lambda r: r.update(assisted={"failure_note": "timeout"}),
            "decision": lambda r: r["teacher"].update(approved_by="teacher-b"),
            "rejection": lambda r: r["teacher"].update(status=sr.STATUS_REJECTED),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                changed = copy.deepcopy(self.record)
                mutate(changed)
                self.assert_not_approved(changed)
                with self.assertRaises(sr.SchemeReviewApprovalError):
                    self.approve(changed)

    def test_replay_in_different_session_rejected(self):
        self.approve()
        other = ApprovalAuthority(lambda a: ServerIdentity("teacher-a", "session-new", "assignment-v1"))
        self.assert_not_approved(context=other.authorize("assignment-a"))

    def test_assignment_context_replay_rejected(self):
        self.approve()
        self.assert_not_approved(context=self.authority.authorize("assignment-b"))

    def test_revised_assignment_invalidates_unchanged_record(self):
        self.approve()
        self.identity = replace(self.identity, assignment_revision="assignment-v2")
        self.assert_not_approved()
        new_context = self.authority.authorize("assignment-a")
        self.assert_not_approved(context=new_context)
        with self.assertRaises(sr.SchemeReviewApprovalError):
            self.approve(context=new_context)

    def test_assignment_scope_requires_verified_revision(self):
        authority = ApprovalAuthority(lambda a: ServerIdentity("teacher-a", "login-a"))
        with self.assertRaises(AuthorizationError):
            authority.authorize("assignment-a")

    def test_regenerated_record_cannot_reuse_registered_identity(self):
        self.approve()
        with self.assertRaises(sr.SchemeReviewApprovalError):
            sr.bind_scheme_review(review(), self.context, review_id="review-0", document_digest="a" * 64)

    def test_missing_digest_rejected(self):
        for digest in (None, "", "a", {}, "A" * 64):
            with self.subTest(digest=digest), self.assertRaises(sr.SchemeReviewApprovalError):
                sr.bind_scheme_review(review(), self.context, review_id="new", document_digest=digest)

    def test_missing_review_identity_and_revision_rejected(self):
        for value in (None, "", " ", 1, {}):
            record = review()
            record["scheme_revision"] = value
            with self.subTest(value=value), self.assertRaises(sr.SchemeReviewApprovalError):
                self.bind(record)
            with self.assertRaises(sr.SchemeReviewApprovalError):
                sr.bind_scheme_review(review(), self.context, review_id=value, document_digest="a" * 64)

    def test_raw_approved_metadata_cannot_grant_approval(self):
        self.record["teacher"] = {"status": sr.STATUS_APPROVED, "approved_by": "teacher-a",
                                  "approved_revision": self.record["scheme_revision"]}
        self.assert_not_approved()

    def test_tampered_findings_cannot_be_registered_as_fresh(self):
        record = review(maximum=5)
        record["verified"]["findings"] = []
        record["teacher"]["status"] = sr.STATUS_CHECKED
        with self.assertRaises(sr.SchemeReviewApprovalError):
            self.bind(record)

    def test_unresolved_hard_blocker_prevents_approval(self):
        record = review(maximum=5)
        self.bind(record)
        with self.assertRaisesRegex(sr.SchemeReviewApprovalError, "unresolved_finding"):
            self.approve(record)

    def test_fractional_allocation_cannot_be_certified_after_legacy_truncation(self):
        record = review(maximum=1.9)
        self.assertEqual(record["extracted"]["questions"][0]["max_marks"], 1)
        with self.assertRaises(sr.SchemeReviewApprovalError):
            self.bind(record)

    def test_source_evidence_blocker_requires_correction(self):
        inputs = copy.deepcopy(self.record["approval_inputs"])
        inputs["evidence_document"]["pages"][0]["text"] = "No matching source excerpt"
        record = sr.build_review(**inputs, assignment_id="assignment-a")
        self.bind(record)
        self.assertTrue(sr._blocking_findings(record))
        for finding in sr._blocking_findings(record):
            with self.assertRaises(sr.SchemeReviewApprovalError):
                sr.resolve_findings(record, [finding["finding_id"]], self.context,
                                    reason="checked", evidence={"document_digest": "a" * 64,
                                                               "page": 1, "excerpt": "B1 correct"})
        with self.assertRaises(sr.SchemeReviewApprovalError):
            self.approve(record)

    def test_blank_identity_cannot_resolve(self):
        for identity in (None, "", " ", "teacher-a"):
            with self.subTest(identity=identity), self.assertRaises(sr.SchemeReviewApprovalError):
                sr.resolve_findings(self.record, ["any"], identity)

    def uncertain_record(self):
        record = review(certainty="heuristic")
        self.bind(record)
        self.assertEqual(len(record["verified"]["findings"]), 1)
        return record

    def resolve(self, record, **kwargs):
        values = {"reason": "Teacher checked the question heading in the official page.",
                  "evidence": {"document_digest": "a" * 64, "page": 1, "excerpt": "B1 correct"}}
        values.update(kwargs)
        return sr.resolve_findings(record, [record["verified"]["findings"][0]["finding_id"]], self.context, **values)

    def test_individual_evidenced_mapping_resolution_then_separate_approval(self):
        record = self.uncertain_record()
        self.resolve(record)
        self.assert_not_approved(record)
        self.assertEqual(record["verified"]["findings"][0]["resolved_by"], "teacher-a")
        self.approve(record)
        self.assertEqual(sr.approval_state(record, self.context, record["scheme_revision"])[0], "approved")

    def test_resolution_reason_and_evidence_required(self):
        for kwargs in ({"reason": None}, {"reason": " "}, {"evidence": None}, {"evidence": {}},
                       {"evidence": {"document_digest": "b" * 64, "page": 1, "excerpt": "B1 correct"}},
                       {"evidence": {"document_digest": "a" * 64, "page": 1, "excerpt": "invented"}},
                       {"evidence": {"document_digest": "a" * 64, "page": True, "excerpt": "B1 correct"}},
                       {"evidence": {"document_digest": "a" * 64, "page": 2, "excerpt": "B1 correct"}}):
            with self.subTest(kwargs=kwargs):
                record = self.uncertain_record()
                before = copy.deepcopy(record)
                with self.assertRaises(sr.SchemeReviewApprovalError):
                    self.resolve(record, **kwargs)
                self.assertEqual(record, before)

    def test_blanket_resolution_is_rejected(self):
        record = self.uncertain_record()
        fid = record["verified"]["findings"][0]["finding_id"]
        for ids in ([], [fid, fid], "*", ["*"], ["unknown"]):
            with self.subTest(ids=ids), self.assertRaises(sr.SchemeReviewApprovalError):
                sr.resolve_findings(record, ids, self.context, reason="checked", evidence={})
        self.assertFalse(record["verified"]["findings"][0]["teacher_resolved"])

    def test_nonwaivable_blockers_reject_even_with_reason_and_evidence(self):
        record = review(maximum=5)
        self.bind(record)
        for finding in record["verified"]["findings"]:
            with self.subTest(rule=finding["rule_id"]), self.assertRaises(sr.SchemeReviewApprovalError):
                sr.resolve_findings(record, [finding["finding_id"]], self.context,
                                    reason="Teacher checked", evidence={"document_digest": "a" * 64,
                                                                         "page": 1, "excerpt": "B1 correct"})

    def test_manual_resolved_flag_is_not_authority(self):
        record = self.uncertain_record()
        record["verified"]["findings"][0]["teacher_resolved"] = True
        with self.assertRaises(sr.SchemeReviewApprovalError):
            self.approve(record)

    def test_changed_resolution_invalidates_approval(self):
        record = self.uncertain_record()
        self.resolve(record)
        self.approve(record)
        record["verified"]["findings"][0]["resolution_reason"] = "different"
        self.assert_not_approved(record)

    def test_acknowledgement_metadata_is_not_approval(self):
        self.record["teacher"]["acknowledged"] = True
        self.assert_not_approved()

    def test_logout_invalidates_existing_approval(self):
        self.approve()
        self.identity = None
        self.assert_not_approved()
        with self.assertRaises(sr.SchemeReviewApprovalError):
            self.approve()

    def test_explicit_revocation_clears_server_state(self):
        self.approve()
        self.authority.revoke()
        self.assertEqual(self.authority._reviews, {})
        self.assertEqual(self.authority._contexts, [])
        self.assert_not_approved()

    def test_account_switch_cannot_inherit_approval(self):
        self.approve()
        self.identity = ServerIdentity("teacher-b", "session-b", "assignment-v1")
        self.assert_not_approved()

    def test_same_account_new_login_invalidates_approval(self):
        self.approve()
        self.identity = ServerIdentity("teacher-a", "session-new", "assignment-v1")
        self.assert_not_approved()

    def test_verifier_error_is_content_free(self):
        def bad_verifier(assignment):
            raise RuntimeError("secret credential and student data")
        self.authority._verify = bad_verifier
        state, reason = sr.approval_state(self.record, self.context, self.record["scheme_revision"])
        self.assertEqual((state, reason), ("invalid", "authorization_unavailable"))

    def test_corrupt_records_fail_closed(self):
        for record in (None, [], {}, {"teacher": []}, {"teacher": {}, "verified": {"findings": [None]}}):
            with self.subTest(record=record):
                self.assertNotEqual(sr.approval_state(record, self.context, "x")[0], "approved")
                self.assertNotEqual(sr.determine_status(record, self.context), sr.STATUS_APPROVED)
                self.assertNotEqual(sr.review_summary(record, self.context)["status"], sr.STATUS_APPROVED)

    def test_non_json_and_nonfinite_content_fails_closed(self):
        for value in (float("nan"), float("inf"), object()):
            record = copy.deepcopy(self.record)
            record["unexpected"] = value
            self.assert_not_approved(record)
            with self.assertRaises(sr.SchemeReviewApprovalError):
                self.approve(record)

    def test_rejected_review_cannot_be_approved_without_regeneration(self):
        sr.reject_scheme_review(self.record, self.context)
        with self.assertRaises(sr.SchemeReviewApprovalError):
            self.approve()
        self.assert_not_approved()

    def test_unauthenticated_rejection_is_not_allowed(self):
        with self.assertRaises(sr.SchemeReviewApprovalError):
            sr.reject_scheme_review(self.record, "teacher-a")

    def test_read_only_maxima_warnings_work_without_context(self):
        record = sr.review_extracted_maxima({"1": 2}, question_numbers=["1", "2"])
        self.assertEqual(sr.review_summary(record)["status"], sr.STATUS_NEEDS_REVIEW)
        self.assertTrue(sr.review_summary(record)["messages"])
        record["assignment_id"] = "assignment-a"
        with self.assertRaises(sr.SchemeReviewApprovalError):
            self.bind(record)

    def test_approval_changes_only_teacher_metadata(self):
        before = copy.deepcopy(self.record)
        self.approve()
        after = copy.deepcopy(self.record)
        after["teacher"] = before["teacher"]
        self.assertEqual(after, before)
        self.assertNotIn("session_nonce", json.dumps(self.record))

    def test_registry_is_bounded(self):
        for index in range(1, 100):
            self.bind(review())
        with self.assertRaises(sr.SchemeReviewApprovalError):
            self.bind(review())


if __name__ == "__main__":
    unittest.main()
