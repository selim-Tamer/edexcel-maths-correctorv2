"""Stage C4 integration layer: mathematical claims + C2/C3 evidence.

Investigation-only. NOT imported by the production app; imports nothing
from it. Accepts explicitly provided mathematical claims, optionally
carrying Stage C3 PDF source references, and produces an auditable
envelope that keeps EVERYTHING separated:

- mathematical correctness (this stage) is never conflated with mark
  eligibility (the marking engine's business);
- original Stage C2 findings are passed through UNCHANGED (deep copies,
  nothing added, nothing removed, no state change);
- Stage C3 document hashes and page numbers are preserved exactly as
  supplied and can be cross-checked against supplied C3 document records;
- expressions are NEVER inferred from ambiguous PDF text: every
  mathematical input is an explicit caller-supplied claim, and PDF text
  is referenced, not absorbed.
"""

import copy

RESULT_SCHEMA = "investigation.math_verification/1"

ENVELOPE_SCHEMA = "investigation.math_claims/1"

SEPARATION_NOTES = (
    "mathematical correctness is reported separately from mark "
    "eligibility; no grade, mark or criterion decision is made here",
    "a contradicted mathematical claim never modifies any Stage C2 "
    "finding and never asserts that an official document is wrong",
    "PDF source references are preserved verbatim (hashes and pages); "
    "extracted PDF text was never parsed or absorbed as mathematics",
)

MAX_CLAIMS_PER_ENVELOPE = 200
MAX_REFERENCES_PER_CLAIM = 50


def validate_math_source_references(references, c3_documents=None):
    """Validate the C3-shaped source references of one claim.

    Each reference keeps ``document_id / document_role / sha256 / page /
    label`` (and an optional ``excerpt``). When C3 document records are
    supplied, EVERY identity field is cross-checked:

    - the digest must match at least one supplied record;
    - the ``document_id`` must match a record that shares the digest
      (a digest alone never binds a document);
    - if several supplied records share BOTH the digest and the
      ``document_id``, resolution would be ambiguous and the claim is
      rejected;
    - the role and page bound of the bound record must agree.

    Returns deep copies; the caller's structures are never mutated.
    """
    from investigation.math_verification import _validate_source_references
    validated = _validate_source_references(references)
    if len(validated) > MAX_REFERENCES_PER_CLAIM:
        raise ValueError("too many source references for one claim")
    if c3_documents is None:
        return copy.deepcopy(validated)
    if not isinstance(c3_documents, (list, tuple)):
        raise ValueError("c3_documents must be a list of C3 records")
    records = []
    for document in c3_documents:
        from investigation.pdf_evidence import validate_document_evidence
        records.append(validate_document_evidence(document))
    for reference in validated:
        digest_matches = [record for record in records
                          if record["sha256"] == reference["sha256"]]
        if not digest_matches:
            raise ValueError(
                "a source reference digest does not match any supplied "
                "C3 document record")
        bound = [record for record in digest_matches
                 if record["document_id"] == reference["document_id"]]
        if not bound:
            raise ValueError(
                "a source reference document_id does not match any "
                "supplied C3 record with the referenced digest")
        if len(bound) > 1:
            raise ValueError(
                "ambiguous C3 binding: multiple supplied records share "
                "the referenced digest and document_id, so the source "
                "cannot be resolved unambiguously")
        record = bound[0]
        if reference["document_role"] != record["document_role"]:
            raise ValueError(
                "a source reference role disagrees with its C3 "
                "document record")
        if reference["page"] > record["page_count"]:
            raise ValueError(
                "a source reference page is outside its C3 document "
                "record")
    return copy.deepcopy(validated)


def _claim_is_supported(claim):
    from investigation.math_verification import CHECK_KINDS
    return isinstance(claim, dict) and claim.get("kind") in CHECK_KINDS


def verify_math_claims(claims, c3_documents=None, c2_findings=None):
    """Verify claims and return the integration envelope.

    - ``claims``: caller-supplied claim dicts (never PDF text);
    - ``c3_documents``: optional C3 extracted document records used to
      cross-check reference hashes/pages;
    - ``c2_findings``: optional Stage C2 findings list; returned UNCHANGED
      as deep copies (input objects are never mutated).

    Nothing here reads, infers or rewrites grades, marks or criteria and
    nothing here asserts anything about any official document.
    """
    if not isinstance(claims, (list, tuple)):
        raise ValueError("claims must be a list of claim dicts")
    if len(claims) > MAX_CLAIMS_PER_ENVELOPE:
        raise ValueError("too many claims in one envelope")
    from investigation.math_verification import verify_claim
    snapshots = copy.deepcopy(claims)      # prove no input mutation
    finding_snapshots = copy.deepcopy(c2_findings)
    results = []
    claim_ids = set()
    for claim in claims:
        if not _claim_is_supported(claim):
            raise ValueError(
                "every claim must be a dict with a supported kind")
        claim_id = claim["claim_id"]
        if claim_id in claim_ids:
            raise ValueError(f"claim id repeats: {claim_id!r}")
        claim_ids.add(claim_id)
        validated_references = validate_math_source_references(
            claim.get("source_references"), c3_documents)
        working_claim = copy.deepcopy(claim)
        working_claim["source_references"] = validated_references
        results.append(verify_claim(working_claim))
    results.sort(key=lambda item: item["claim_id"])
    if claims != snapshots:
        raise AssertionError("input claims were mutated by verification")
    envelope = {
        "schema": ENVELOPE_SCHEMA,
        "claims_verified": len(results),
        "results": results,
        "c2_findings_passed_through": (
            [] if c2_findings is None else copy.deepcopy(c2_findings)),
        "notes": list(SEPARATION_NOTES),
    }
    if finding_snapshots is not None and c2_findings is not None \
            and c2_findings != finding_snapshots:
        raise AssertionError("input C2 findings were mutated")
    return envelope
