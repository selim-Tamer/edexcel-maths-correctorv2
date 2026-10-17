"""Advanced offline marking-accuracy reporting layer (Phase B4 Stage 2B).

Evaluation infrastructure ONLY: no network, no Gemini SDK imports, no
production marking code, no golden answer generation, no prompt construction.
It never writes grades and is never called by the application.

Formulas and denominators (rates are None on empty denominators, and every
rate is always accompanied by its raw counts):

  known_reference_criteria   gold criteria with non-null expected, counted
                             over all reported cases
  gold_null_criteria         gold criteria with null expected, over all
                             reported cases
  answered_criteria          known-reference criteria actually answered
                             (abstention-free), completed predictions only
  known_reference_abstentions declared abstentions on known-reference
                             criteria (gold-null abstentions counted
                             separately and excluded from every denominator)
  false_award_rate           false_awards / answered_criteria
  incorrect_deduction_rate   incorrect_deductions / answered_criteria
  selective_accuracy         (correct_awards + correct_withholds) /
                             answered_criteria
  coverage                   answered_criteria /
                             (answered_criteria + known_reference_abstentions)
  conservative_accuracy      (correct_awards + correct_withholds) /
                             (answered_criteria + known_reference_abstentions)
  Coverage is a completed-attempt measure: a missing prediction or failure
  never counts as an abstention; it is exposed through prediction_status and
  the per-case status instead.
  Category and combined-group variants use the same formulas restricted to
  their own criteria. Combined groups:
    method_all      = method_ordinary + method_dependent
    accuracy_all    = accuracy_ordinary + accuracy_follow_through
    b_or_independent = independent_b

Abstention semantics (never silently reinterpreted):
  - An abstention NEVER counts as a correct marking decision in any metric.
  - Selective accuracy excludes abstentions from the denominator, so it is
    paired with coverage; conservative accuracy keeps them in the denominator
    as non-credit, so abstaining can only lower it.
  - The False award the adapter emits for compatibility with the legacy
    evaluator is neutralized here via declared_unknown: it is compared
    against the reference only when it was NOT declared unknown, and it
    never contributes to predicted weighted totals.
  - This differs from the legacy accuracy.evaluate() semantics; legacy
    output remains authoritative and untouched.

  exact_score_agreement      cases whose total weighted marks match / cases
                             with every criterion expected non-null
                             ("fully scorable") among completed predictions
  criterion_vector_agreement cases whose known-criterion award vector matches
                             exactly / fully-scorable completed cases.
                             A declared abstention on a known criterion
                             breaks both vector and exact agreement when a
                             weighted difference results.
  Partial known-gold cases (some criterion expected null) are excluded from
  exact metrics and listed separately; they are never treated as fully
  scoreable.

Case statuses (failures are never completions):
  completed | explicit_model_failure | schema_validation_failure |
  missing_prediction
  Adapter diagnostic reasons (ADAPTER_SCHEMA_REASONS) map to
  schema_validation_failure; failed=True with reason 'explicit_failure' or a
  model-declared failure text maps to explicit_model_failure.

Mark-code categories (explicit normalization; unfamiliar codes are reported
as 'unknown', never guessed): M<n> -> method_ordinary, dM<n> ->
method_dependent, A<n> -> accuracy_ordinary, accuracy codes with a trailing
'ft' -> accuracy_follow_through, B<n> -> independent_b.

Latency is recorded only from completed predictions with a valid
non-negative finite timing; missing/invalid timings are counted, never
fabricated.
"""
import json
import math
import statistics
from pathlib import Path

REPORT_VERSION = "2b.2"

UNKNOWN_CATEGORY = "unknown"
CATEGORIES = (
    "method_ordinary",
    "method_dependent",
    "accuracy_ordinary",
    "accuracy_follow_through",
    "independent_b",
    UNKNOWN_CATEGORY,
)
COMBINED_CATEGORY_GROUPS = {
    "method_all": ("method_ordinary", "method_dependent"),
    "accuracy_all": ("accuracy_ordinary", "accuracy_follow_through"),
    "b_or_independent": ("independent_b",),
}

# Failure reasons emitted by investigation/marking_adapter.py for its own
# validation failures (as opposed to model-declared explicit failures).
ADAPTER_SCHEMA_REASONS = frozenset({
    "missing_or_invalid_uncertain_flag", "invalid_seconds",
    "missing_breakdown", "breakdown_count_mismatch",
    "malformed_breakdown_row", "invalid_criterion_id",
    "unknown_criterion_id", "duplicate_criterion_id",
    "invalid_unknown_flag", "missing_award_field",
    "non_boolean_award", "uncertainty_not_declared",
    "unknown_award_contradiction", "code_mismatch", "weight_mismatch",
})

_PLAIN_HEADS = {
    "M": "method_ordinary",
    "A": "accuracy_ordinary",
    "B": "independent_b",
}


def classify_code(code):
    """Map a raw mark code to its metric category, or None when unfamiliar."""
    if type(code) is not str:
        return None
    code = code.strip()
    if not code:
        return None
    if code.endswith("ft") and len(code) > 2:
        # Follow-through variants are only category-splittable when the base
        # code is an ordinary accuracy mark (A1ft, A2ft, ...).
        base = code[:-2]
        return "accuracy_follow_through" if _plain_category(base) == "accuracy_ordinary" else None
    return _plain_category(code)


def _plain_category(code):
    if code.startswith("dM") and len(code) > 2 and code[2:].isdigit() and int(code[2:]) > 0:
        return "method_dependent"
    head, rest = code[0], code[1:]
    if head in _PLAIN_HEADS and rest.isdigit() and int(rest) > 0:
        return _PLAIN_HEADS[head]
    return None


def load_governance(path=None):
    """Load the governance manifest JSON (offline; default sits beside this file)."""
    file = Path(__file__).with_name("case_governance.json") if path is None else Path(path)
    manifest = json.loads(file.read_text(encoding="utf-8"))
    if not isinstance(manifest.get("definitions"), dict) or not manifest["definitions"]:
        raise ValueError("Governance manifest has no classification definitions")
    if not isinstance(manifest.get("cases"), dict):
        raise ValueError("Governance manifest has no cases map")
    return manifest


def governance_classifications(manifest, gold_cases):
    """Return {case_id: classification}; every gold case must be classified exactly once."""
    cases_map = manifest["cases"]
    for raw in cases_map.values():
        if not isinstance(raw, dict) or raw.get("classification") not in manifest["definitions"]:
            raise ValueError("Governance manifest entry lacks a defined classification")
    result = {}
    for case in gold_cases:
        case_id = case.get("id")
        if type(case_id) is not str or not case_id.strip():
            raise ValueError("Gold case list has an invalid case ID")
        if case_id in result:
            raise ValueError("Gold case list has duplicate case IDs")
        entry = cases_map.get(case_id)
        if entry is None:
            raise ValueError(f"Governance manifest is missing case: {case_id}")
        result[case_id] = entry["classification"]
    return result


def _rate(numerator, denominator):
    return None if denominator == 0 else numerator / denominator


def _empty_counts():
    return {"answered": 0, "correct_awards": 0, "correct_withholds": 0,
            "false_awards": 0, "incorrect_deductions": 0, "abstentions": 0}


def _add(counts, detail):
    for key, value in detail.items():
        counts[key] += value


def _finalize(counts):
    """Fold raw bucket counts into reported rates (None on empty denominators)."""
    correct = counts["correct_awards"] + counts["correct_withholds"]
    answered = counts["answered"]
    abstentions = counts["abstentions"]
    return {
        **counts,
        "known_criteria": answered + abstentions,
        "false_award_rate": _rate(counts["false_awards"], answered),
        "incorrect_deduction_rate": _rate(counts["incorrect_deductions"], answered),
        "selective_accuracy": _rate(correct, answered),
        "conservative_accuracy": _rate(correct, answered + abstentions),
        "coverage": _rate(answered, answered + abstentions),
    }


def _valid_seconds(value):
    if type(value) not in (int, float) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _prediction_status(prediction, scheme_ids):
    """Return the strict status label for one adapter-shape prediction."""
    failure_reason = prediction.get("failure_reason")
    failed = prediction.get("failed", False)

    if type(prediction.get("uncertain")) is not bool:
        return "schema_validation_failure"
    if type(failed) is not bool:
        return "schema_validation_failure"
    seconds = prediction.get("seconds")
    if seconds is not None and not _valid_seconds(seconds):
        return "schema_validation_failure"

    if failed:
        if isinstance(failure_reason, str) and failure_reason in ADAPTER_SCHEMA_REASONS:
            return "schema_validation_failure"
        return "explicit_model_failure"

    if isinstance(failure_reason, str):
        return "schema_validation_failure"
    awards = prediction.get("awards")
    if not isinstance(awards, list):
        return "schema_validation_failure"
    seen = set()
    for award in awards:
        if not isinstance(award, dict):
            return "schema_validation_failure"
        award_id = award.get("id")
        if type(award_id) is not str or not award_id.strip():
            return "schema_validation_failure"
        if award_id not in scheme_ids or award_id in seen:
            return "schema_validation_failure"
        seen.add(award_id)
        if type(award.get("awarded")) is not bool:
            return "schema_validation_failure"
    if seen != scheme_ids:
        return "schema_validation_failure"
    declared = prediction.get("declared_unknown")
    declared = [] if declared is None else declared
    if not isinstance(declared, list) or any(a not in scheme_ids for a in declared):
        return "schema_validation_failure"
    return "completed"


def build_report(gold_cases, predictions, manifest):
    """Produce the deterministic Stage 2B report dict (JSON-serializable).

    gold_cases   golden benchmark cases. Labels (expected/uncertain) are read
                 for scoring ONLY; this module never constructs prompts.
    predictions  adapter-shape prediction dicts; duplicates, unknown case IDs
                 and non-dict entries are programmer errors (ValueError).
    manifest     governance manifest (dict from load_governance()).
    """
    schemes = {}
    cases = {}
    for case in gold_cases:
        case_id = case.get("id")
        if type(case_id) is not str or not case_id.strip():
            raise ValueError("Gold case list has an invalid case ID")
        if case_id in schemes:
            raise ValueError("Gold case list has duplicate case IDs")
        scheme = case.get("scheme")
        expected = case.get("expected")
        if not isinstance(scheme, list) or not isinstance(expected, list) \
                or len(scheme) != len(expected):
            raise ValueError(f"Gold case {case_id} has a malformed scheme/expected pair")
        pairs = []
        for criterion, exp in zip(scheme, expected):
            if not isinstance(criterion, dict) or type(criterion.get("id")) is not str \
                    or type(criterion.get("code")) is not str \
                    or not isinstance(criterion.get("weight"), int) \
                    or criterion.get("weight", 0) <= 0 \
                    or (exp is not None and type(exp) is not bool):
                raise ValueError(f"Gold case {case_id} has a malformed criterion")
            pairs.append((criterion, exp))
        schemes[case_id] = pairs
        cases[case_id] = case

    classifications = governance_classifications(manifest, gold_cases)

    pred_by_id = {}
    for prediction in predictions:
        if not isinstance(prediction, dict):
            raise ValueError("Prediction list contains a non-dict entry")
        case_id = prediction.get("id")
        if type(case_id) is not str or not case_id.strip():
            raise ValueError("Prediction list contains an invalid case ID")
        if case_id not in schemes:
            raise ValueError(f"Prediction references an unknown gold case: {case_id}")
        if case_id in pred_by_id:
            raise ValueError(f"Duplicate predictions for case: {case_id}")
        pred_by_id[case_id] = prediction

    status_counts = {"completed": 0, "explicit_model_failure": 0,
                     "schema_validation_failure": 0, "missing_prediction": 0}
    case_results = []
    category_counts = {name: _empty_counts() for name in CATEGORIES}
    combined_counts = {name: _empty_counts() for name in COMBINED_CATEGORY_GROUPS}
    award_balance = {"correct_awards": 0, "correct_withholds": 0,
                     "false_awards": 0, "incorrect_deductions": 0}
    abstentions = {"known_reference": 0, "gold_null": 0,
                   "by_category": {name: 0 for name in CATEGORIES}}
    exact_matches = exact_scorable = vector_matches = vector_scorable = 0
    partial_known_gold_case_ids = []
    uncertainty = {"expected": 0, "detected": 0, "unnecessary": 0,
                   "uncertain_completions": 0}
    latency = {"completed_timings": [], "missing_or_invalid_on_completed": 0,
               "non_completed_with_valid_timing": 0}
    gold_marks_total = predicted_marks_total = 0

    for case_id, pairs in schemes.items():
        classification = classifications[case_id]
        prediction = pred_by_id.get(case_id)
        known_count = sum(1 for _, exp in pairs if exp is not None)
        null_count = len(pairs) - known_count
        if prediction is None:
            status_counts["missing_prediction"] += 1
            case_results.append({
                "id": case_id,
                "classification": classification,
                "status": "missing_prediction",
                "known_reference_criteria": known_count,
                "gold_null_criteria": null_count,
            })
            continue

        status = _prediction_status(prediction, {c["id"] for c, _ in pairs})
        status_counts[status] += 1
        case_result = {
            "id": case_id,
            "classification": classification,
            "status": status,
            "failure_reason": prediction.get("failure_reason"),
            "known_reference_criteria": known_count,
            "gold_null_criteria": null_count,
        }

        seconds = prediction.get("seconds")
        if status == "completed":
            if seconds is None:
                latency["missing_or_invalid_on_completed"] += 1
            else:
                latency["completed_timings"].append(seconds)
        elif seconds is not None:
            latency["non_completed_with_valid_timing"] += 1

        if status != "completed":
            case_results.append(case_result)
            continue

        abstained = list(prediction.get("declared_unknown") or [])
        known_ids = [c["id"] for c, exp in pairs if exp is not None]
        null_ids = [c["id"] for c, exp in pairs if exp is None]
        case_known_abstained = [i for i in known_ids if i in abstained]
        case_null_abstained = [i for i in null_ids if i in abstained]
        pred_map = {a["id"]: a["awarded"] for a in prediction["awards"]}

        case_counts = {name: _empty_counts() for name in CATEGORIES}
        gold_score = predicted_score = 0
        vector_match = True
        for criterion, exp in pairs:
            c_id = criterion["id"]
            category = classify_code(criterion["code"])
            bucket = category if category in category_counts else UNKNOWN_CATEGORY
            if exp is None:
                if c_id in abstained:
                    abstentions["gold_null"] += 1
                    abstentions["by_category"][bucket] += 1
                continue
            gold_score += criterion["weight"] if exp is True else 0
            if c_id in abstained:
                abstentions["known_reference"] += 1
                abstentions["by_category"][bucket] += 1
                category_counts[bucket]["abstentions"] += 1
                for group_name, members in COMBINED_CATEGORY_GROUPS.items():
                    if bucket in members:
                        combined_counts[group_name]["abstentions"] += 1
                vector_match = False
                continue
            category_counts[bucket]["answered"] += 1
            for group_name, members in COMBINED_CATEGORY_GROUPS.items():
                if bucket in members:
                    combined_counts[group_name]["answered"] += 1
            pred = pred_map[c_id]
            predicted_score += criterion["weight"] if pred is True else 0
            if pred is exp:
                detail = {"correct_awards": 1} if exp else {"correct_withholds": 1}
            elif pred is True:
                detail = {"false_awards": 1}
            else:
                detail = {"incorrect_deductions": 1}
            _add(category_counts[bucket], detail)
            _add(case_counts[bucket], detail)
            for group_name, members in COMBINED_CATEGORY_GROUPS.items():
                if bucket in members:
                    _add(combined_counts[group_name], detail)
            _add(award_balance, detail)
            if pred is not exp:
                vector_match = False

        case_result["abstentions"] = {"known_reference": case_known_abstained,
                                      "gold_null": case_null_abstained}
        case_result["answered_criteria"] = len(known_ids) - len(case_known_abstained)
        case_result["category_counts"] = case_counts
        gold_uncertain = bool(cases[case_id].get("uncertain"))
        pred_uncertain = prediction["uncertain"]
        case_result["uncertain_flag"] = {"gold": gold_uncertain,
                                         "prediction": pred_uncertain}
        uncertainty["expected"] += 1 if gold_uncertain else 0
        uncertainty["detected"] += 1 if (gold_uncertain and pred_uncertain) else 0
        uncertainty["unnecessary"] += 1 if (pred_uncertain and not gold_uncertain) else 0
        uncertainty["uncertain_completions"] += 1 if pred_uncertain else 0
        gold_marks_total += gold_score
        predicted_marks_total += predicted_score

        if not null_ids:
            exact_scorable += 1
            vector_scorable += 1
            exact_matches += 1 if gold_score == predicted_score else 0
            vector_matches += 1 if vector_match else 0
            case_result["exact_score_match"] = gold_score == predicted_score
            case_result["criterion_vector_match"] = vector_match
        else:
            partial_known_gold_case_ids.append(case_id)
        case_result["gold_total_marks"] = gold_score
        case_result["predicted_total_marks"] = predicted_score
        case_results.append(case_result)

    answered_known = sum(c["answered"] for c in category_counts.values())
    known_abstentions_total = abstentions["known_reference"]
    correct_total = award_balance["correct_awards"] + award_balance["correct_withholds"]

    criteria_summary = {
        "known_reference_criteria": sum(c["known_reference_criteria"] for c in case_results),
        "gold_null_criteria": sum(c["gold_null_criteria"] for c in case_results),
        "answered_criteria": answered_known,
        "known_reference_abstentions": known_abstentions_total,
        "gold_null_abstentions": abstentions["gold_null"],
        "abstention_by_category": abstentions["by_category"],
        "award_balance": award_balance,
        "rate_denominator": ("answered known-reference criteria "
                             f"({answered_known})"),
        "false_award_rate": _rate(award_balance["false_awards"], answered_known),
        "incorrect_deduction_rate": _rate(award_balance["incorrect_deductions"], answered_known),
        "coverage": _rate(answered_known, answered_known + known_abstentions_total),
        "selective_accuracy": _rate(correct_total, answered_known),
        "conservative_accuracy": _rate(correct_total,
                                       answered_known + known_abstentions_total),
        "weighted_totals": {
            "gold_marks_earned": gold_marks_total,
            "predicted_marks_earned": predicted_marks_total,
            "difference": predicted_marks_total - gold_marks_total,
        },
        "exact_score_agreement": _rate(exact_matches, exact_scorable),
        "criterion_vector_agreement": _rate(vector_matches, vector_scorable),
        "fully_scorable_cases": exact_scorable,
        "partial_known_gold_case_ids": partial_known_gold_case_ids,
        "category_accuracy": {name: _finalize(category_counts[name])
                              for name in CATEGORIES},
        "combined_category_accuracy": {
            name: _finalize(combined_counts[name]) for name in COMBINED_CATEGORY_GROUPS
        },
    }

    timings = sorted(latency["completed_timings"])
    latency_report = {
        "observations": len(timings),
        "median_seconds": statistics.median(timings) if timings else None,
        "p90_seconds": timings[math.ceil(0.9 * len(timings)) - 1]
        if len(timings) >= 10 else None,
        "missing_or_invalid_on_completed": latency["missing_or_invalid_on_completed"],
        "non_completed_with_valid_timing": latency["non_completed_with_valid_timing"],
    }

    per_class = {}
    for classification in sorted(set(classifications.values())):
        sub_cases = [c for c in case_results if c["classification"] == classification]
        sub_answered = 0
        sub_known_abstentions = 0
        sub_correct = 0
        sub_exact = sub_scorable = sub_vector = 0
        for c in sub_cases:
            if c["status"] != "completed":
                continue
            sub_answered += c["answered_criteria"]
            sub_known_abstentions += len(c["abstentions"]["known_reference"])
            for bucket in c["category_counts"].values():
                sub_correct += bucket["correct_awards"] + bucket["correct_withholds"]
            if c.get("criterion_vector_match") is not None:
                sub_exact += 1 if c["exact_score_match"] else 0
                sub_vector += 1 if c["criterion_vector_match"] else 0
                sub_scorable += 1
        per_class[classification] = {
            "cases": len(sub_cases),
            "case_ids": sorted(c["id"] for c in sub_cases),
            "completed_attempts": sum(1 for c in sub_cases if c["status"] == "completed"),
            "failed_attempts": sum(1 for c in sub_cases if c["status"] != "completed"),
            "answered_criteria": sub_answered,
            "known_reference_abstentions": sub_known_abstentions,
            "selective_accuracy": _rate(sub_correct, sub_answered),
            "coverage": _rate(sub_answered, sub_answered + sub_known_abstentions),
            "conservative_accuracy": _rate(sub_correct,
                                           sub_answered + sub_known_abstentions),
            "exact_score_agreement": _rate(sub_exact, sub_scorable),
            "criterion_vector_agreement": _rate(sub_vector, sub_scorable),
        }

    return {
        "report_version": REPORT_VERSION,
        "attempted_cases": len(schemes),
        "prediction_status": status_counts,
        "criteria": criteria_summary,
        "cases": case_results,
        "uncertainty": uncertainty,
        "latency": latency_report,
        "governance": per_class,
        "legacy_evaluator_note": ("accuracy.evaluate() legacy metrics remain authoritative "
                                  "and untouched; Stage 2B abstention-aware metrics are "
                                  "additive and are not comparable with historical output."),
    }
