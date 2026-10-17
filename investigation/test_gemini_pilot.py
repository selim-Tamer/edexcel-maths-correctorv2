"""Offline tests for the Stage 3B-1 Gemini pilot runner (mocked clients only).

No network, no Gemini SDK construction, no credentials: the live path is
exercised exclusively through dependency-injected mock client/config
factories. Deterministic fixtures are built from the golden benchmark's
labels (oracle-in-test convention; the runner itself never reads labels).
"""
import copy
import json
import os
import re
import tempfile
import threading
import unittest
from unittest import mock

import investigation.gemini_pilot as pilot
import investigation.marking_adapter as marking_adapter
import investigation.marking_metrics as marking_metrics
from investigation.accuracy import cases as _cases


def labels():
    return {c["id"]: c for c in _cases()}


def _oracle_response(case, opaque_id, overrides=None, abstain=(), uncertain=None):
    """Adapter-shape success body (labels drive awards: TEST ONLY)."""
    rows = []
    for c, exp in zip(case["scheme"], case["expected"]):
        row = {"criterion_id": c["id"], "awarded": False if exp is None else exp,
               "unknown": False}
        if c["id"] in abstain:
            row.update(awarded=False, unknown=True)
        rows.append(row)
    for row in (overrides or []):
        row.update({"criterion_id": row.get("criterion_id")})
        for target in rows:
            if target["criterion_id"] == row.get("criterion_id"):
                target.update({k: v for k, v in row.items() if k != "criterion_id"})
                break
    return {"id": opaque_id, "failed": False,
            "uncertain": bool(case.get("uncertain")) if uncertain is None else uncertain,
            "breakdown": rows}


def _wrapped(text, model_version="gemini-3.8-flash-001"):
    return "```json\n" + text + "\n```"


class _Exception404(Exception):
    code = 404

    def __init__(self):
        super().__init__(
            "models/gemini-3.8-flash is not found for API version v1beta")


class _Timeout(Exception):
    pass


class _MockResponse:
    def __init__(self, text=None, model_version="gemini-3.8-flash-001",
                 finish_reason=None, block_reason=None):
        self.text = text
        self.model_version = model_version
        self.usage_metadata = None
        self.prompt_feedback = (None if block_reason is None else
                                _block(block_reason))
        if finish_reason is None:
            self.candidates = []
        else:
            self.candidates = [_finish(finish_reason)]


def _finish(value):
    holder = type("C", (), {})()
    holder.finish_reason = value
    return holder


def _block(value):
    holder = type("P", (), {})()
    holder.block_reason = value
    return holder


class _MockModels:
    def __init__(self, behavior):
        self.behavior = behavior      # opaque_id -> ('text', str) / ('raise', exc)
        self.calls = []

    def generate_content(self, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config,
                           "call_number": len(self.calls) + 1})
        # Which case is this? Find the opaque ID in the prompt.
        opaque = next((k for k in self.behavior
                       if f"CASE ID: {k}" in contents[0]), None)
        if opaque is None:
            # Oracle-in-test default: a label-driven perfect response, so a
            # bare mocked client still yields well-formed per-case bodies.
            index = int(re.search(r"PILOT-(\d{2})", contents[0]).group(1)) - 1
            case = labels()[pilot.PILOT_CASE_IDS[index]]
            body = json.dumps(_oracle_response(case, f"PILOT-{index + 1:02d}"))
            return _MockResponse(text=body)
        kind, payload = self.behavior[opaque]
        if kind == "raise":
            raise payload
        return payload


class _MockClient:
    def __init__(self, behavior):
        self.models = _MockModels(behavior)
        self.config_fingerprints = []

    def check(self):
        return self


def _mock_factories():
    """Return (client_holder, config_capture) injectable factories."""
    state = {}

    def client_factory():
        return state["client"]

    def config_factory(**kwargs):
        state["last_config"] = kwargs
        return kwargs            # plain dict: no SDK dependency in tests

    return state, client_factory, config_factory


def _run_pilot(behavior, opt_in="1", budget=5, cases_data=None, model=None,
               campaign_ledger=None, checkpoint_writer=None):
    cases_data = cases_data if cases_data is not None else pilot.load_pilot_cases()
    manifest = marking_metrics.load_governance()
    state, client_factory, config_factory = _mock_factories()
    state["client"] = _MockClient(behavior)
    artifact = pilot.run_live(cases_data, manifest,
                              model=model or pilot.DEFAULT_MODEL,
                              client_factory=client_factory,
                              config_factory=config_factory,
                              opt_in=opt_in,
                              budget_confirmation=budget,
                              campaign_ledger=campaign_ledger,
                              checkpoint_writer=checkpoint_writer)
    return state, artifact


class PromptConstruction(unittest.TestCase):
    def test_rendered_prompt_uses_opaque_ids_not_canonical(self):
        prompts = pilot.render_prompts(pilot.load_pilot_cases())
        mapping = pilot.opaque_ids(pilot.load_pilot_cases())
        for prompt in prompts:
            self.assertEqual(prompt["opaque_case_id"],
                             mapping[prompt["canonical_case_id"]])
            # the canonical ID is never the model-facing case identifier
            # ("follow-through" may legitimately appear inside criterion text)
            self.assertNotIn("CASE ID: " + prompt["canonical_case_id"],
                             prompt["user_prompt"])
            self.assertIn("CASE ID: " + prompt["opaque_case_id"],
                          prompt["user_prompt"])
        self.assertEqual([p["opaque_case_id"] for p in prompts],
                         [f"PILOT-{i + 1:02d}" for i in range(5)])

    def test_golden_labels_never_reach_any_prompt(self):
        cases_by_id = labels()
        rendered = pilot.render_prompts(pilot.load_pilot_cases())
        for prompt in rendered:
            gold = cases_by_id[prompt["canonical_case_id"]]
            derivation = gold.get("derivation")
            if derivation:
                self.assertNotIn(derivation, prompt["user_prompt"])
                self.assertNotIn(derivation, prompt["system_prompt"])
            self.assertNotIn("expected", prompt["user_prompt"])
            self.assertNotIn("synthetic_provisional_rubric", prompt["user_prompt"])
            self.assertNotIn("human_review_required", prompt["user_prompt"])
            self.assertNotIn("previous prediction", prompt["user_prompt"].lower())
            # the golden uncertain FLAG value itself is never included
            self.assertNotIn(f"uncertain={gold.get('uncertain')}",
                             prompt["user_prompt"])
        # top-level fields the prompt exposes come from prompt_case only:
        exposed = pilot.accuracy.prompt_case(cases_by_id["algebra-correct"])
        self.assertEqual(sorted(exposed),
                         sorted(["id", "topic", "question", "student", "scheme"]))
        self.assertNotIn("derivation", exposed)

    def test_corrected_rubric_instructions_present(self):
        # Stage 3A prompt problems are corrected in the system prompt text.
        crt = pilot.SYSTEM_PROMPT.lower()
        self.assertIn("synthetic and provisional", crt)
        self.assertIn("criterion-specific convention", crt)
        self.assertIn("crossed-out work with no replacement remains eligible", crt)
        self.assertIn("not an uncertainty", crt)
        self.assertIn("or equivalent method", crt)
        self.assertIn('"unknown": true', crt)
        self.assertNotIn("never earns method marks", crt)


class SafetyGates(unittest.TestCase):
    def _prep(self):
        return {"MATH_MARKER_EVAL_OPT_IN": "1"}

    def test_missing_opt_in_blocks_everything(self):
        with self.assertRaises(RuntimeError):
            _run_pilot({}, opt_in=None)   # env unset -> gate refused

    def test_wrong_budget_refused(self):
        with self.assertRaises(RuntimeError):
            _run_pilot({}, budget=4)

    def test_over_budget_case_list_refused_before_any_call(self):
        state, client_factory, config_factory = _mock_factories()
        state["client"] = _MockClient({})
        with self.assertRaises(ValueError):
            pilot.run_live(pilot.load_pilot_cases() * 2 + [labels()["algebra-correct"]],
                           marking_metrics.load_governance(),
                           client_factory=lambda: state["client"],
                           config_factory=lambda **kw: kw,
                           opt_in="1", budget_confirmation=11)
        self.assertEqual(state["client"].models.calls, [])

    def test_invalid_model_id_refused(self):
        with self.assertRaises(ValueError):
            _run_pilot({}, model="not a model id; DROP TABLE")

    def test_dry_run_never_reads_environment_or_credentials(self):
        class ForbiddenEnv:
            def get(self, *a, **k):
                raise AssertionError("dry-run must not read the environment")

        with mock.patch.object(os, "environ", ForbiddenEnv()):
            preview = pilot.build_preview()          # no arg error, no env read
        self.assertEqual(preview["mode"], "dry-run")
        self.assertEqual(preview["status"], "dry-run-no-requests-made")

    def test_live_mode_aborts_on_unavailable_model(self):
        behavior = {
            "PILOT-01": ("text", _MockResponse(text=_wrapped(json.dumps(
                _oracle_response(labels()["algebra-correct"], "PILOT-01"))))),
            "PILOT-02": ("raise", _Exception404()),
        }
        state, artifact = _run_pilot(behavior)
        # attempt #1 succeeds, attempt #2 hits an unavailable model and the
        # runner stops immediately: cases 3-5 are never attempted.
        self.assertEqual(len(state["client"].models.calls), 2)
        self.assertEqual(artifact["aborted_after"], "model_unavailable")
        skipped = [c for c in artifact["cases"] if not c["attempted"]]
        self.assertEqual([c["failure_category"] for c in skipped],
                         ["aborted_after_model_unavailable"] * 3)
        self.assertEqual([c["outcome"] for c in skipped], ["skipped"] * 3)
        # one model only; no substitution occurred
        self.assertEqual({c["model"] for c in state["client"].models.calls},
                         {pilot.DEFAULT_MODEL})

    def test_timeout_does_not_abort_or_retry(self):
        responses = {}
        for index, cid in enumerate(pilot.PILOT_CASE_IDS):
            opaque = pilot.opaque_case_id(index)
            if cid == "probability-tree-arithmetic-error":
                responses[opaque] = ("raise", _Timeout())
                continue
            responses[opaque] = ("text", _MockResponse(text=_wrapped(json.dumps(
                _oracle_response(labels()[cid], opaque), sort_keys=True))))
        state, artifact = _run_pilot(responses)
        self.assertEqual(len(state["client"].models.calls), 5)  # every case once
        self.assertIsNone(artifact["aborted_after"])
        timed_out = next(c for c in artifact["cases"] if c["case_id"]
                         == "probability-tree-arithmetic-error")
        self.assertEqual(timed_out["failure_category"], "timeout")
        self.assertEqual(artifact["report"]["prediction_status"]["completed"], 4)

    def test_no_retry_no_fallback_single_model(self):
        responses = {}
        for index, cid in enumerate(pilot.PILOT_CASE_IDS):
            opaque = pilot.opaque_case_id(index)
            if cid == "algebra-partial":
                responses[opaque] = ("text", _MockResponse(text=None))   # empty
                continue
            responses[opaque] = ("text", _MockResponse(text=_wrapped(json.dumps(
                _oracle_response(labels()[cid], opaque)))))
        state, artifact = _run_pilot(responses)
        self.assertEqual(len(state["client"].models.calls), 5)
        counts = {}
        for call in state["client"].models.calls:
            counts[call["model"]] = counts.get(call["model"], 0) + 1
        self.assertEqual(counts, {pilot.DEFAULT_MODEL: 5})
        # Each failed case appears at most once with no second attempt:
        case_attempts = [c for c in artifact["cases"]
                         if c["case_id"] == "algebra-partial"]
        self.assertEqual(len(case_attempts), 1)
        self.assertEqual(case_attempts[0]["attempt_number"],
                         artifact["cases"].index(case_attempts[0]) + 1)

    def test_module_has_no_forbidden_imports(self):
        with open(pilot.__file__, encoding="utf-8") as handle:
            source = handle.read()
        module_lines = [ln for ln in source.splitlines()
                        if ln.startswith(("import ", "from "))]
        for forbidden in ("app5_1_automated", "marking_requests", "streamlit",
                          "from google", "import google"):
            self.assertTrue(all(forbidden not in ln for ln in module_lines),
                            f"found forbidden top-level import {forbidden}")


class AdapterIntegration(unittest.TestCase):
    def _behavior_for(self, per_case):
        """per_case: canonical id -> response bytes builder callback."""
        behavior = {}
        for index, cid in enumerate(pilot.PILOT_CASE_IDS):
            opaque = pilot.opaque_case_id(index)
            builder = per_case.get(cid)
            if builder is None:
                builder = lambda c, op: _oracle_response(c, op)
            behavior[opaque] = ("text", _MockResponse(text=json.dumps(
                builder(labels()[cid], opaque))))
        return behavior

    def test_perfect_responses_all_five_cases(self):
        state, artifact = _run_pilot(self._behavior_for({}))
        report = artifact["report"]
        self.assertEqual(report["prediction_status"],
                         {"completed": 5, "explicit_model_failure": 0,
                          "schema_validation_failure": 0,
                          "missing_prediction": 0})
        summary = report["criteria"]
        self.assertEqual(summary["known_reference_criteria"], 12)
        self.assertEqual(summary["gold_null_criteria"], 0)
        self.assertEqual(summary["answered_criteria"], 12)
        self.assertEqual(summary["award_balance"],
                         {"correct_awards": 7, "correct_withholds": 5,
                          "false_awards": 0, "incorrect_deductions": 0})
        self.assertEqual(summary["selective_accuracy"], 1.0)
        self.assertEqual(summary["strict_all_attempt_exact_score_agreement"], 1.0)
        self.assertEqual(summary["exact_denominators"],
                         {"definitive_completions": 5, "abstained_cases": 0,
                          "failed_attempts": 0})
        self.assertEqual(report["governance"]["synthetic_unambiguous"]["cases"], 4)
        self.assertEqual(report["governance"]["synthetic_provisional_rubric"]["cases"], 1)
        self.assertEqual(artifact["legacy_evaluator"]["criterion_accuracy"], 1.0)

    def test_partially_correct_responses_reported(self):
        def partial(case, opaque):
            body = _oracle_response(case, opaque)
            # algebra-partial mirrors gold already (T,F): confirmed partial.
            return body
        state, artifact = _run_pilot(self._behavior_for({"algebra-partial": partial}))
        pred = next(p for p in artifact["predictions"]
                    if p["id"] == "algebra-partial")
        awards = {a["id"]: a["awarded"] for a in pred["awards"]}
        self.assertEqual(awards, {"crit-alg-p-1": True, "crit-alg-p-2": False})
        self.assertEqual(artifact["report"]["prediction_status"]["completed"], 5)

    def test_repeated_m1_codes_resolve_by_unique_ids(self):
        state, artifact = _run_pilot(self._behavior_for({}))
        pred = next(p for p in artifact["predictions"]
                    if p["id"] == "probability-tree-arithmetic-error")
        ids = [a["id"] for a in pred["awards"]]
        self.assertEqual(ids, ["crit-tree-1", "crit-tree-2", "crit-tree-3"])
        self.assertEqual([a["awarded"] for a in pred["awards"]], [True, True, False])

    def test_follow_through_and_dependent_marks(self):
        state, artifact = _run_pilot(self._behavior_for({}))
        ft = next(p for p in artifact["predictions"] if p["id"] == "follow-through")
        self.assertEqual([a["awarded"] for a in ft["awards"]], [True, False, True])
        conflic = next(p for p in artifact["predictions"]
                       if p["id"] == "geometry-conflicting-methods")
        self.assertEqual([a["awarded"] for a in conflic["awards"]], [False, False])
        # gold (F, F) answered exactly: a full definitive vector match
        self.assertTrue(artifact["report"]["cases"][-1]["criterion_vector_match"])

    def test_contradictory_unknown_award_fails_whole_response(self):
        def contradiction(case, opaque):
            body = _oracle_response(case, opaque)
            body["breakdown"][0]["unknown"] = True   # with awarded True default
            body["uncertain"] = True
            return body
        state, artifact = _run_pilot(self._behavior_for(
            {"algebra-correct": contradiction}))
        self.assertEqual(artifact["report"]["prediction_status"]
                         ["schema_validation_failure"], 1)
        entry = next(c for c in artifact["cases"] if c["case_id"] == "algebra-correct")
        self.assertEqual(entry["outcome"], "parsed")
        self.assertEqual(entry["adapter_failure_reason"],
                         "unknown_award_contradiction")
        self.assertEqual(artifact["legacy_evaluator"]["failures"], 1)

    def test_declared_abstention_flows_through_reporting(self):
        def abstain(case, opaque):
            return _oracle_response(case, opaque, abstain={"crit-alg-c-2"},
                                    uncertain=True)
        state, artifact = _run_pilot(self._behavior_for({"algebra-correct": abstain}))
        report = artifact["report"]
        self.assertEqual(report["criteria"]["known_reference_abstentions"], 1)
        self.assertEqual(report["criteria"]["gold_null_abstentions"], 0)
        self.assertIsNone(report["cases"][0]["criterion_vector_match"])
        self.assertEqual(report["cases"][0]["exact_score_ineligible_reason"],
                         "known_reference_abstention")
        self.assertEqual(report["prediction_status"]["completed"], 5)

    def test_missing_and_duplicate_criterion_rows_fail(self):
        def missing_row(case, opaque):
            body = _oracle_response(case, opaque)
            body["breakdown"] = body["breakdown"][:-1]
            return body

        def duplicate_middle_row(case, opaque):
            # follow-through has 3 criteria; duplicating the middle row keeps
            # the row count valid so the adapter's duplicate-ID check fires
            # (breakdown count check alone cannot surface duplicates).
            body = _oracle_response(case, opaque)
            body["breakdown"][2] = copy.deepcopy(body["breakdown"][1])
            return body
        behavior = self._behavior_for({"algebra-correct": missing_row,
                                       "follow-through": duplicate_middle_row})
        state, artifact = _run_pilot(behavior)
        reasons = {p["id"]: p.get("failure_reason") for p in artifact["predictions"]
                   if p.get("failed")}
        self.assertEqual(reasons, {"algebra-correct": "breakdown_count_mismatch",
                                   "follow-through": "duplicate_criterion_id"})
        self.assertEqual(artifact["report"]["prediction_status"]["completed"], 3)
        self.assertEqual(artifact["report"]["prediction_status"]
                         ["schema_validation_failure"], 2)

    def test_adaptation_no_seconds_in_model_schema(self):
        schema = pilot.evaluation_response_schema()
        self.assertIn("seconds", marking_adapter.EVALUATION_RESPONSE_SCHEMA
                      ["properties"])
        self.assertNotIn("seconds", schema["properties"])
        self.assertEqual(schema["required"], ["id", "uncertain"])

    def test_seconds_injected_from_measured_latency(self):
        state, artifact = _run_pilot(self._behavior_for({}))
        for entry in artifact["cases"]:
            self.assertIsNotNone(entry["latency_seconds"])
            self.assertGreaterEqual(entry["latency_seconds"], 0.0)
        for prediction in artifact["predictions"]:
            self.assertIsNotNone(prediction.get("seconds"))
            self.assertGreaterEqual(prediction["seconds"], 0.0)

    def test_wrong_opaque_id_echo_fails_case_without_guessing(self):
        def wrong_id(case, opaque):
            body = _oracle_response(case, opaque)
            body["id"] = "PILOT-99"
            return body
        state, artifact = _run_pilot(self._behavior_for(
            {"algebra-correct": wrong_id}))
        entry = next(c for c in artifact["cases"]
                     if c["case_id"] == "algebra-correct")
        self.assertEqual(entry["failure_category"], "id_mismatch")
        self.assertEqual(entry["outcome"], "failed")
        self.assertEqual(artifact["report"]["prediction_status"]
                         ["schema_validation_failure"], 0)
        self.assertEqual(artifact["report"]["prediction_status"]
                         ["explicit_model_failure"], 0)
        self.assertEqual(artifact["report"]["prediction_status"]["completed"], 4)

    def test_random_malformed_json_and_incomplete_output(self):
        responses = {}
        for index, cid in enumerate(pilot.PILOT_CASE_IDS):
            opaque = pilot.opaque_case_id(index)
            if cid == "algebra-correct":
                responses[opaque] = ("text", _MockResponse(text="{not json"))
            elif cid == "algebra-partial":
                responses[opaque] = ("text", _MockResponse(text=None))
            else:
                responses[opaque] = ("text", _MockResponse(text=json.dumps(
                    _oracle_response(labels()[cid], opaque))))
        state, artifact = _run_pilot(responses)
        outcomes = {c["case_id"]: (c["outcome"], c["failure_category"])
                    for c in artifact["cases"]}
        self.assertEqual(outcomes["algebra-correct"], ("failed", "invalid_json"))
        self.assertEqual(outcomes["algebra-partial"], ("failed", "empty_response"))
        self.assertEqual(artifact["report"]["prediction_status"]["completed"], 3)

    def test_artifact_metadata_and_hashes(self):
        state, artifact = _run_pilot(self._behavior_for({}))
        self.assertEqual(artifact["benchmark_sha256"], pilot.benchmark_sha256())
        self.assertEqual(artifact["governance_sha256"], pilot.governance_sha256())
        self.assertEqual(artifact["git_commit_sha"], pilot.git_head_sha())
        self.assertEqual(artifact["artifact_version"], "3b.2")
        self.assertEqual(len(artifact["opaque_id_map"]), 5)
        self.assertEqual(artifact["max_requests"], 5)
        # schema included & uses adapter schema minus seconds
        self.assertEqual(artifact["evaluation_response_schema"],
                         pilot.evaluation_response_schema())

    def test_all_five_attempts_recorded_with_model_version(self):
        state, artifact = _run_pilot(self._behavior_for({}))
        self.assertEqual([c["attempt_number"] for c in artifact["cases"]],
                         [1, 2, 3, 4, 5])
        self.assertTrue(all(c["attempted"] for c in artifact["cases"]))
        versions = {c["actual_model_version"] for c in artifact["cases"]}
        self.assertEqual(versions, {"gemini-3.8-flash-001"})
        # Probe the exact production-mirroring config kwargs (mock capture).
        config = state["last_config"]
        self.assertEqual(config["system_instruction"], pilot.SYSTEM_PROMPT)
        self.assertEqual(config["response_mime_type"], "application/json")
        # Stage 3B-2: temperature removed (deprecated for Gemini 3.x models);
        # determinism comes from the explicit-rules system instruction.
        self.assertNotIn("temperature", config)
        self.assertNotIn("top_p", config)
        self.assertNotIn("top_k", config)
        self.assertEqual(config["max_output_tokens"], 2048)
        self.assertEqual(config["thinking_config"],
                         {"thinking_level": "medium"})
        self.assertEqual(config["http_options"],
                         {"timeout": 120000, "retry_options": {"attempts": 1}})


class SystemInstructionDelivery(unittest.TestCase):
    """Reviewer fix: the system prompt must actually reach the live request."""

    def _opaque_of(self, user_prompt, mapping):
        return next(o for o in mapping.values()
                    if f"CASE ID: {o}" in user_prompt)

    def test_live_request_delivers_system_instruction_and_user_prompt(self):
        state, artifact = _run_pilot({})
        prompts = pilot.render_prompts(pilot.load_pilot_cases())
        by_opaque = {p["opaque_case_id"]: p for p in prompts}
        mapping = {v: k for k, v in pilot.opaque_ids(pilot.load_pilot_cases()).items()}
        self.assertEqual(len(state["client"].models.calls), 5)
        derivation_by_id = labels()
        for call in state["client"].models.calls:
            user_prompt = call["contents"][0]
            opaque = self._opaque_of(user_prompt, pilot.opaque_ids(pilot.load_pilot_cases()))
            case = by_opaque[opaque]
            self.assertEqual(call["contents"], [case["user_prompt"]])
            # the marking rules travel as system instructions, not as contents
            self.assertEqual(call["config"]["system_instruction"],
                             case["system_prompt"])
            self.assertEqual(call["model"], pilot.DEFAULT_MODEL)
            # temperature deprecated for Gemini 3.x: never sent anymore
            self.assertNotIn("temperature", call["config"])
            self.assertEqual(call["config"]["thinking_config"],
                             {"thinking_level": "medium"})
            self.assertEqual(call["config"]["max_output_tokens"], 2048)
            self.assertEqual(call["config"]["http_options"],
                             {"timeout": 120000, "retry_options": {"attempts": 1}})
            self.assertEqual(call["config"]["response_mime_type"],
                             "application/json")
            self.assertEqual(call["config"]["response_schema"],
                             pilot.evaluation_response_schema())
            # neither component leaks golden data; opaque IDs preserved
            gold = derivation_by_id[mapping[opaque]]
            for component in (case["user_prompt"], case["system_prompt"]):
                if gold.get("derivation"):
                    self.assertNotIn(gold["derivation"], component)
                self.assertNotIn("expected", component)
                self.assertNotIn("synthetic_provisional_rubric", component)
                self.assertNotIn("human_review_required", component)
            self.assertIn(f"CASE ID: {opaque}", user_prompt)
            self.assertNotIn("CASE ID: " + mapping[opaque], user_prompt)

    def test_dry_run_and_live_use_identical_prompt_components(self):
        preview = pilot.build_preview()
        state, artifact = _run_pilot({})
        preview_by_opaque = {p["opaque_case_id"]: p
                             for p in preview["prompts_rendered"]}
        for call in state["client"].models.calls:
            user_prompt = call["contents"][0]
            opaque = self._opaque_of(user_prompt, pilot.opaque_ids(pilot.load_pilot_cases()))
            rendered = preview_by_opaque[opaque]
            self.assertEqual(user_prompt, rendered["user_prompt"])
            self.assertEqual(call["config"]["system_instruction"],
                             rendered["system_prompt"])
        # per-ledger reproducibility hashes cover BOTH prompt components
        for entry in artifact["cases"]:
            expected_user = _sha256(preview_by_opaque[entry["opaque_case_id"]]["user_prompt"])
            expected_system = _sha256(preview_by_opaque[entry["opaque_case_id"]]["system_prompt"])
            self.assertEqual(entry["prompt_fingerprint"], expected_user)
            self.assertEqual(entry["system_prompt_fingerprint"], expected_system)


class _CodedError(Exception):
    """Provider-shaped exception with an HTTP code (content-free message)."""

    def __init__(self, code, message="content-free provider error"):
        super().__init__(message)
        self.code = code


class StrictAbortPolicy(unittest.TestCase):
    """Stage 3B-2: auth/permission/quota/config/ambiguous errors abort."""

    def _assert_stops(self, code, category):
        behavior = {"PILOT-02": ("raise", _CodedError(code))}
        state, artifact = _run_pilot(behavior)
        # one success, one failure, then the remaining cases are skipped
        self.assertEqual(len(state["client"].models.calls), 2)
        self.assertEqual(artifact["aborted_after"], category)
        attempted = [c for c in artifact["cases"] if c["attempted"]]
        self.assertEqual([c["outcome"] for c in attempted],
                         ["parsed", "failed"])
        self.assertEqual([c["failure_category"] for c in attempted[1:]],
                         [category])
        skipped = [c for c in artifact["cases"] if not c["attempted"]]
        self.assertEqual([c["failure_category"] for c in skipped],
                         [f"aborted_after_{category}"] * 3)
        self.assertEqual([c["outcome"] for c in skipped], ["skipped"] * 3)
        # one model only; no substitution and no second attempt
        self.assertEqual({c["model"] for c in state["client"].models.calls},
                         {pilot.DEFAULT_MODEL})

    def test_http_401_authentication_stops_pilot(self):
        self._assert_stops(401, "request_rejected")

    def test_http_403_permission_stops_pilot(self):
        self._assert_stops(403, "request_rejected")

    def test_http_400_configuration_error_stops_pilot(self):
        self._assert_stops(400, "request_rejected")

    def test_http_429_quota_stops_pilot(self):
        self._assert_stops(429, "quota_limited")

    def test_ambiguous_transport_error_stops_pilot_without_retry(self):
        behavior = {"PILOT-02": ("raise", RuntimeError("connection reset"))}
        state, artifact = _run_pilot(behavior)
        self.assertEqual(len(state["client"].models.calls), 2)
        self.assertEqual(artifact["aborted_after"],
                         "transport_or_client_error")
        # The failed case was attempted exactly once: no retry, ever.
        case2 = [c for c in artifact["cases"]
                 if c["case_id"] == "algebra-partial"]
        self.assertEqual(len(case2), 1)
        self.assertEqual(case2[0]["outcome"], "failed")

    def test_service_unavailable_continues_but_never_retries(self):
        # 5xx is outside the abort list (approved planning) and must never
        # trigger a second request for the same case.
        behavior = {"PILOT-02": ("raise", _CodedError(503))}
        state, artifact = _run_pilot(behavior)
        self.assertEqual(len(state["client"].models.calls), 5)
        self.assertIsNone(artifact["aborted_after"])
        counts = {}
        for call in state["client"].models.calls:
            counts[call["model"]] = counts.get(call["model"], 0) + 1
        self.assertEqual(counts, {pilot.DEFAULT_MODEL: 5})

    def test_timeout_continues_but_never_retries(self):
        behavior = {"PILOT-02": ("raise", TimeoutError())}
        state, artifact = _run_pilot(behavior)
        self.assertEqual(len(state["client"].models.calls), 5)
        self.assertIsNone(artifact["aborted_after"])


class GenerationSettingsTests(unittest.TestCase):
    """Stage 3B-2: no temperature; explicit medium thinking; SDK-compatible."""

    def test_no_sampling_parameter_reaches_any_request(self):
        state, artifact = _run_pilot({})
        for call in state["client"].models.calls:
            self.assertNotIn("temperature", call["config"])
            self.assertNotIn("top_p", call["config"])
            self.assertNotIn("top_k", call["config"])

    def test_dry_run_preview_carries_no_temperature(self):
        preview = pilot.build_preview()
        self.assertNotIn("temperature", json.dumps(preview))
        self.assertEqual(preview["live_gates"]["explicit_thinking_level"],
                         "medium")

    def test_installed_sdk_constructs_the_exact_config_offline(self):
        # Offline construction only (no request): proves the runner's exact
        # kwargs are valid for the installed google-genai package, including
        # dict-form thinking_config coercion.
        from google.genai import types
        config = types.GenerateContentConfig(
            system_instruction=pilot.SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=pilot.evaluation_response_schema(),
            **pilot.PILOT_GENERATION_SETTINGS,
            http_options={"timeout": 120000, "retry_options": {"attempts": 1}},
        )
        self.assertIs(config.thinking_config.thinking_level,
                      types.ThinkingLevel.MEDIUM)
        self.assertIsNone(config.temperature)
        self.assertEqual(config.max_output_tokens, 2048)
        self.assertEqual(config.http_options.retry_options.attempts, 1)


class CampaignBudgetTests(unittest.TestCase):
    """Persistent ledger: five requests for the whole campaign, not per run."""

    def _fresh_ledger(self, directory):
        ledger = pilot.CampaignLedger(directory)
        ledger.ensure_openable()
        return ledger

    def test_campaign_budget_shared_across_two_consecutive_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._fresh_ledger(tmp)
            state1, artifact = _run_pilot({}, campaign_ledger=ledger)
            self.assertEqual(ledger.count_used(), 5)
            self.assertEqual(artifact["campaign_requests_used"], 5)
            self.assertEqual(artifact["campaign_requests_remaining"], 0)
            with self.assertRaises(pilot.CampaignLedgerError):
                _run_pilot({}, campaign_ledger=ledger)
        # a truly independent ledger starts fresh: the refusal was campaign
        # state, not a code-level constant
        with tempfile.TemporaryDirectory() as tmp:
            ledger2 = self._fresh_ledger(tmp)
            state2, artifact2 = _run_pilot({}, campaign_ledger=ledger2)
            self.assertEqual(artifact2["campaign_requests_used"], 5)

    def test_attempted_cases_cannot_replay_accidentally(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._fresh_ledger(tmp)
            ledger.reserve("algebra-correct", "PILOT-01")
            # the run refuses EITHER because the budget is short or because
            # a listed case was already attempted — both fail closed
            with self.assertRaises(pilot.CampaignLedgerError) as ctx:
                _run_pilot({}, campaign_ledger=ledger)
            self.assertRegex(str(ctx.exception),
                             r"(replay refused|budget exhausted)")

    def test_hard_ceiling_of_five_across_the_campaign(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._fresh_ledger(tmp)
            for i in range(5):
                ledger.reserve(f"other-benchmark-case-{i}",
                               f"PILOT-{i + 1:02d}")
            self.assertEqual(ledger.count_remaining(), 0)
            with self.assertRaises(pilot.CampaignLedgerError):
                ledger.reserve("one-more-case", "PILOT-06")
            with self.assertRaises(pilot.CampaignLedgerError):
                _run_pilot({}, campaign_ledger=ledger)

    def test_concurrent_launches_cannot_exceed_the_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._fresh_ledger(tmp)
            barrier = threading.Barrier(8)
            allowed, refused = [], []
            def worker(index):
                barrier.wait()
                try:
                    ledger.reserve(f"case-{index}", f"PILOT-{index:02d}")
                    allowed.append(index)
                except pilot.CampaignLedgerError:
                    refused.append(index)
            threads = [threading.Thread(target=worker, args=(i,))
                       for i in range(8)]
            for t in threads: t.start()
            for t in threads: t.join()
            self.assertEqual(len(allowed), 5)
            self.assertEqual(len(refused), 3)
            self.assertEqual(ledger.count_used(), 5)

    def test_interrupted_run_attempts_stay_consumed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._fresh_ledger(tmp)
            # simulate a process death between reserve and complete:
            ledger.reserve("algebra-correct", "PILOT-01")
            # a new process re-opens the same ledger directory:
            reopened = pilot.CampaignLedger(tmp)
            with self.assertRaises(pilot.CampaignLedgerError):
                reopened.check_run_permitted(
                    [c for c in pilot.PILOT_CASE_IDS])

    def test_ledger_identity_mismatch_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._fresh_ledger(tmp)
            forged = json.loads(ledger.path.read_text(encoding="utf-8"))
            forged["campaign_id"] = "someone-elses-campaign"
            pilot._atomic_write_json(ledger.path, forged)
            reopened = pilot.CampaignLedger(tmp)
            with self.assertRaises(pilot.CampaignLedgerError):
                reopened.check_run_permitted(list(pilot.PILOT_CASE_IDS))

    def test_missing_ledger_with_existing_artifacts_refuses_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            # a previous live artifact exists (a completed run) ...
            pilot._atomic_write_json(
                os.path.join(tmp, "20260101T000000Z-gemini-pilot.json"),
                {"artifact_version": "3b.2", "mode": "live"})
            # ... but the ledger is gone (deleted/copy mistake). No silent
            # restart: the runner demands manual review instead.
            reopened = pilot.CampaignLedger(tmp)
            with self.assertRaises(pilot.CampaignLedgerError) as ctx:
                reopened.check_run_permitted(list(pilot.PILOT_CASE_IDS))
            self.assertIn("manual review required", str(ctx.exception))

    def test_fresh_ledger_without_artifacts_is_openable(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = pilot.CampaignLedger(tmp)
            ledger.check_run_permitted(list(pilot.PILOT_CASE_IDS))
            self.assertEqual(ledger.count_remaining(), 5)


class ArtifactSafetyTests(unittest.TestCase):
    def test_claim_exclusive_path_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "20260101T000000Z-gemini-pilot.json")
            pilot._atomic_write_json(base, {"previous": True})
            claimed = pilot.claim_exclusive_path(
                os.path.join(tmp, "20260101T000000Z-gemini-pilot.json"))
            self.assertNotEqual(os.path.abspath(str(claimed)),
                                os.path.abspath(base))
            self.assertTrue(claimed.name.startswith(
                "20260101T000000Z-gemini-pilot"))
            self.assertTrue(str(claimed).endswith(".json"))
            self.assertEqual(json.loads(open(base, encoding="utf-8").read()),
                             {"previous": True})

    def test_atomic_write_json_is_durable_and_leaves_no_temp_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "artifact.json")
            pilot._atomic_write_json(target, {"n": 1})
            pilot._atomic_write_json(target, {"n": 2})
            self.assertEqual(json.loads(open(target, encoding="utf-8").read()),
                             {"n": 2})
            leftovers = [f for f in os.listdir(tmp) if ".tmp" in f]
            self.assertEqual(leftovers, [])

    def test_checkpoint_recorded_after_every_attempted_case(self):
        snapshots = []
        behavior = {"PILOT-03": ("raise", _CodedError(429))}
        state, artifact = _run_pilot(behavior, checkpoint_writer=snapshots.append)
        aborted_index = 3        # PILOT-03 = third case
        self.assertEqual(len(snapshots), aborted_index)
        for order, snapshot in enumerate(snapshots):
            self.assertEqual(snapshot["attempted_count"], order + 1)
        self.assertEqual(snapshots[-1]["attempted_count"], 3)
        self.assertEqual(snapshots[-1]["skipped_count"], 0)
        self.assertEqual(snapshots[-1]["aborted_after"], "quota_limited")
        self.assertEqual(snapshots[-1]["completed_count"], 2)
        # skipped cases are recorded in the final artifact, separately:
        self.assertEqual([c["outcome"] for c in artifact["cases"]],
                         ["parsed", "parsed", "failed",
                          "skipped", "skipped"])
        self.assertEqual([c["failure_category"] for c in artifact["cases"]
                          if c["outcome"] == "skipped"],
                         ["aborted_after_quota_limited"] * 2)
        # snapshots grow monotonically and never shrink
        self.assertEqual([s["attempted_count"] for s in snapshots],
                         [1, 2, 3])

    def test_checkpoints_and_artifact_never_leak_provider_messages(self):
        canary = "SECRET-CANARY-DO-NOT-LEAK"
        snapshots = []
        behavior = {"PILOT-01": ("raise", _CodedError(401,
                                                      message=canary))}
        try:
            _run_pilot(behavior, checkpoint_writer=snapshots.append)
        except Exception as exc:
            self.fail(f"run_live must classify errors, not raise them: {exc}")
        chained = json.dumps(snapshots)
        self.assertNotIn(canary, chained)

    def test_no_secrets_or_env_values_in_artifacts(self):
        state, artifact = _run_pilot({})
        blob = json.dumps(artifact)
        self.assertNotIn("GEMINI_API_KEY", blob)
        self.assertNotIn("api_key", blob.lower())
        self.assertNotIn(OPTIN_VALUE := "MATH_MARKER_EVAL_OPT_IN", blob)


def _sha256(text):
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    unittest.main()
