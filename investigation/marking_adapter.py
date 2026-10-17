"""Evaluation-only adapter: ID-aware marking responses -> accuracy.evaluate() predictions.

Phase B4 Stage 2A. This module is deliberately isolated from production marking:
it never imports app5_1_automated.py, marking_requests.py or Gemini SDK code and
is never called by the application. It converts a fixture / evaluation-request
response dict (criterion_id-addressed) into the exact prediction dict shape
expected by investigation/accuracy.py.

Contract of the accepted evaluation response ("evaluation-only response"):

{
    "id": "<gold case id>",                       # required, valid, unique per batch
    "failed": false,                              # optional strict bool; true = explicit failure
    "uncertain": false,                           # required strict bool (model review signal)
    "seconds": null,                              # optional number|null, finite and >= 0
    "breakdown": [                                # required unless the response is failed
        {
            "criterion_id": "crit-alg-c-1",       # required non-empty string
            "awarded": true,                      # required STRICT bool (no 0/1/"true"/null)
            "unknown": false,                     # optional strict bool: "cannot adjudicate"
            "code": "M1",                         # optional; validated against the scheme
            "weight": 1,                          # optional; validated against the scheme
            "reason": "transcribed evidence"      # optional free text, never used for marks
        }, ...
    ]
}

Alignment and validation rules (fail-safe, never guessing):

- criterion_id is the ONLY alignment key. Repeated codes (2x "M1" in one
  question) remain distinct because IDs differ.
- One breakdown row is required per scheme criterion. Missing, duplicated or
  unknown IDs, extra rows, or mismatched optional code/weight values convert the
  whole response into an explicitly failed prediction:
      {"id": ..., "failed": True, "uncertain": ..., "failure_reason": "..."}
  The evaluator counts such a prediction in cases + failures and gives it zero
  score credit; no fabricated awards are produced.
- Non-boolean "awarded" values (0, 1, "true", None, lists) or a missing award
  field convert the response to a failed prediction; the adapter mirrors the
  strict validation of accuracy.validate/evaluate rather than the lenient
  bool() coercion used by production display normalisation.
- Award values are NEVER inferred from reasons, totals or mark codes.
- Missing awards are NEVER substituted with False. (An explicit model "false"
  award is data; an absent row is an incomplete answer.)
- Unknown/unreadable decisions use ONE strict representation: a row declares
  `"unknown": true` together with the canonical pair `"awarded": false`. Any
  combination of `unknown: true` with `awarded: true` is a contradiction and
  converts the response to a failed prediction (`unknown_award_contradiction`);
  it is never silently converted into a correct or incorrect marking decision
  in either direction. An unknown row missing the awarded field fails with
  `missing_award_field` (the flag pair must be explicit). For every accepted
  unknown row the adapter deterministically emits the award {"awarded": false}
  and records the criterion ID in the prediction's "declared_unknown" key
  (ignored by the evaluator, used for reporting).

Effects of unresolved predictions on existing accuracy metrics:
- Gold-null criteria (expected null, as in unreadable-final and
  speed-ambiguous-handwriting): the evaluator EXCLUDES them per criterion and
  whole-case from exact-score agreement, criterion-vector agreement and
  criterion-count denominators, so the deterministic false emission is
  metric-neutral there. The case still completes and counts in
  uncased/uncertainty metrics via its case-level uncertain flag.
- Gold-scorable criteria: there is no evaluator abstention channel, so the
  deterministic false emission IS counted as an incorrect_deduction (a
  withheld award) against that criterion. It is never hidden: declared_unknown
  makes the abstention visible and such cases must be reported separately in
  Stage 2B reporting; no accuracy change is made here.
- Golden labels never enter the adapter: it reads only scheme id/code/weight
  from the case dict. Feeding it cases stripped of "expected"/"uncertain"/
  "derivation" must behave identically (covered by tests).
- Structural malformations of the BATCH (non-dict response, unknown/missing or
  duplicate case id) are programming errors and raise ValueError instead of
  silently fabricating predictions; the evaluator already rejects unknown or
  duplicate fixture IDs batch-wide.
- "reason" text is optional evidence for reporting; its content never changes
  any award, mapping or metric.
"""
import math

EVALUATION_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "failed": {"type": "boolean"},
        "uncertain": {"type": "boolean"},
        "seconds": {"type": ["number", "null"]},
        "breakdown": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "criterion_id": {"type": "string"},
                    "awarded": {"type": "boolean"},
                    "unknown": {"type": "boolean"},   # requires awarded:false; contradiction -> failed prediction
                    "code": {"type": "string"},
                    "weight": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["criterion_id", "awarded"],
            },
        },
    },
    "required": ["id", "uncertain"],
}


def _strict_bool(value):
    return type(value) is bool


def _case_schemes(case_data):
    schemes = {}
    for case in case_data:
        cid = case.get("id") if isinstance(case, dict) else None
        scheme = case.get("scheme") if isinstance(case, dict) else None
        if type(cid) is not str or not cid.strip() or cid in schemes:
            raise ValueError("Adapter batch contains an invalid or duplicate case ID")
        if not isinstance(scheme, list) or not scheme:
            raise ValueError(f"Adapter batch case {cid} has no scheme")
        schemes[cid] = scheme
    return schemes


def _failed_prediction(case_id, uncertain, reason):
    prediction = {"id": case_id, "failed": True, "uncertain": _strict_bool(uncertain)}
    if reason:
        prediction["failure_reason"] = reason
    return prediction


def prediction_from_response(response, case_schemes):
    """Convert one evaluation response dict into an accuracy.evaluate() prediction.

    Raises ValueError for batch/structural programmer errors; converts
    model-level validation problems into explicit failed predictions.
    """
    if not isinstance(response, dict):
        raise ValueError("Adapter response is not a dict")
    case_id = response.get("id")
    if type(case_id) is not str or not case_id.strip():
        raise ValueError("Adapter response has an invalid case id field")
    if case_id not in case_schemes:
        raise ValueError(f"Adapter response references an unknown case: {case_id}")
    uncertain = response.get("uncertain")
    if not _strict_bool(uncertain):
        return _failed_prediction(case_id, False, "missing_or_invalid_uncertain_flag")
    failed_raw = response.get("failed", False)
    if type(failed_raw) is not bool:
        raise ValueError(f"Adapter response {case_id} has a non-boolean failed flag")
    failed = failed_raw

    seconds = response.get("seconds")
    if seconds is not None:
        valid = type(seconds) in (int, float) and not isinstance(seconds, bool)
        try:
            valid = valid and math.isfinite(seconds) and seconds >= 0
        except OverflowError:
            valid = False
        if not valid:
            return _failed_prediction(case_id, uncertain, "invalid_seconds")

    scheme = case_schemes[case_id]
    if failed:
        prediction = _failed_prediction(case_id, uncertain, response.get("failure_reason")
                                        if isinstance(response.get("failure_reason"), str) else "explicit_failure")
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
            prediction["seconds"] = seconds
        return prediction

    breakdown = response.get("breakdown")
    if not isinstance(breakdown, list):
        return _failed_prediction(case_id, uncertain, "missing_breakdown")
    if len(breakdown) != len(scheme):
        return _failed_prediction(case_id, uncertain, "breakdown_count_mismatch")

    scheme_by_id = {}
    for criterion in scheme:
        crit_id = criterion.get("id") if isinstance(criterion, dict) else None
        if type(crit_id) is not str or not crit_id.strip() or crit_id in scheme_by_id:
            raise ValueError(f"Adapter gold scheme for {case_id} is malformed")
        scheme_by_id[crit_id] = criterion

    awards = []
    declared_unknown = []
    seen = set()
    for row in breakdown:
        if not isinstance(row, dict):
            return _failed_prediction(case_id, uncertain, "malformed_breakdown_row")
        crit_id = row.get("criterion_id")
        if type(crit_id) is not str or not crit_id.strip():
            return _failed_prediction(case_id, uncertain, "invalid_criterion_id")
        if crit_id not in scheme_by_id:
            return _failed_prediction(case_id, uncertain, "unknown_criterion_id")
        if crit_id in seen:
            return _failed_prediction(case_id, uncertain, "duplicate_criterion_id")
        seen.add(crit_id)
        unknown_flag = row.get("unknown", False)
        if not _strict_bool(unknown_flag):
            return _failed_prediction(case_id, uncertain, "invalid_unknown_flag")
        awarded = row.get("awarded", "missing")
        if not _strict_bool(awarded):
            return _failed_prediction(case_id, uncertain,
                                      "missing_award_field" if awarded == "missing" else "non_boolean_award")
        if unknown_flag and not uncertain:
            return _failed_prediction(case_id, uncertain, "uncertainty_not_declared")
        if unknown_flag and awarded:
            # Abstention contradicts an award assertion. Never silently convert
            # it into a marking decision for either direction.
            return _failed_prediction(case_id, uncertain, "unknown_award_contradiction")
        code = row.get("code")
        scheme_code = scheme_by_id[crit_id].get("code")
        if code is not None and (type(code) is not str or code.strip() != scheme_code):
            return _failed_prediction(case_id, uncertain, "code_mismatch")
        weight = row.get("weight")
        if weight is not None:
            scheme_weight = scheme_by_id[crit_id].get("weight")
            if type(weight) is not int or isinstance(weight, bool) or weight != scheme_weight:
                return _failed_prediction(case_id, uncertain, "weight_mismatch")
        awards.append({"id": crit_id, "awarded": False if unknown_flag else awarded})
        if unknown_flag:
            declared_unknown.append(crit_id)

    prediction = {"id": case_id, "uncertain": uncertain, "awards": awards}
    if declared_unknown:
        prediction["declared_unknown"] = declared_unknown
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
        prediction["seconds"] = seconds
    return prediction


def predictions_from_responses(responses, gold_cases):
    """Convert a list of evaluation responses into evaluator predictions.

    gold_cases: the golden case list (labels are IGNORED by this module; only
    scheme id/code/weight are read). Order of predictions follows the responses.
    """
    schemes = _case_schemes(gold_cases)
    seen_cases = set()
    predictions = []
    for response in responses:
        if isinstance(response, dict):
            case_id = response.get("id")
            if type(case_id) is str and case_id in seen_cases:
                raise ValueError(f"Adapter batch has duplicate responses for case: {case_id}")
            seen_cases.add(case_id if type(case_id) is str else repr(case_id))
        predictions.append(prediction_from_response(response, schemes))
    return predictions
