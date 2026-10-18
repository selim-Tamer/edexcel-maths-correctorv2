# Independent diagnostic tools

These modules are not imported by the production app. Run from the repository root. Standard repository development/test dependencies provide httpx, ReportLab and PDF test libraries. Nothing discovers keys from the environment, Streamlit or local credential files.

## Offline (safe default)

```sh
python -m investigation.diagnose
python -m investigation.diagnose --level C --variant strokes
python -m investigation.profile
python -m investigation.pdf_fixtures
python -m unittest discover -s tests -p 'test_*.py'
python -m unittest discover -s tests/sdk -p 'test_*.py'
python -m unittest discover -s tests/ui -p 'test_*.py'
```

PDF generation writes three deterministic synthetic files to `output/pdf/`. The stroke example is hand-drawn vector glyphs, not real handwriting. The source fixture generator is versioned; generated outputs need not be committed.

## Stage C3 verification modules (offline)

`investigation.pdf_evidence`, `investigation.pdf_question_map` and
`investigation.verification_evidence` extract digital text from teacher-supplied
PDFs offline, map question headings conservatively, and link Stage C2 findings
to page evidence. They are not imported by the production app.

Extraction mirrors the production bounds in `local_pdf.py` (50 MB document cap,
2000-page cap, 20k chars/page, 2 MB aggregate text budget; 8-bit raw streams)
and never repairs extracted text: page output states are only `extracted`,
`empty_text_layer`, `extraction_failed`, `ambiguous_layout`,
`unsupported_content` or `budget_exhausted`. Question mapping requires a
caller-supplied question id list and only accepts anchored prefix headings
(`Question N` / `Q N`); bare numbers are opt-in. Cross-page duplicate headings
are reported as ambiguous; spanning-page inference is opt-in and labelled
`heuristic_inference`. Excerpts in an evidence reference must be verbatim
substrings of the page text with a span that bounds them, so fabricated spans
are structurally impossible. Alignment findings reuse the Stage C2 state
vocabulary and can only report suspected issues or evidence gaps — never a
`verified_structural_inconsistency`, because extraction limitations can always
hide real content.

Run their tests with:

```sh
python -m unittest investigation.test_pdf_evidence investigation.test_pdf_question_map investigation.test_verification_evidence -v
```

## Stage C4 mathematical verification modules (offline)

`safe_math.py`, `math_verification.py`, `math_claims.py` and
`math_benchmark.py` add an offline, deterministic mathematical
verification engine for the investigation. It is investigation-only:
the production app never imports it and it imports nothing from the
production app.

- `safe_math.py` parses untrusted text with a validated-AST whitelist
  (operators, bounded numbers, whitelisted variables, `sqrt`/`abs` only)
  and builds Sympy objects node by node. No `eval`/`exec` and no
  unrestricted Sympy parsing ever touches raw text; refusals are
  explicit (`unsupported_syntax`, `unknown_symbol`, `expression_too_long`,
  `expression_too_complex`, `number_too_large`, `exponent_too_large`).
  Length/node/depth caps, integer and exponent magnitude caps, and a
  combinatorial expand bound computed without expanding bound all
  symbolic work.
- `math_verification.py` verifies caller-supplied claims into a fixed
  result contract (`verified` / `contradicted` / `inconclusive` /
  `unsupported`; no numeric confidence anywhere). Checks cover exact
  arithmetic, algebraic identities (expansion, factorisation, rational
  equivalence with domain/pole notes), surd equality, bounded
  polynomial equation solving (degree cap 4; quadratic checks cap 2),
  exact substitution, assumption-scoped refinement (`refine`) and
  50-digit numeric comparison ONLY for explicitly approximate claims.
  A nonzero canonical difference proves a contradiction only for
  constant/rational/polynomial classes; symbolic surd/abs forms stay
  inconclusive. Equivalence is never claimed from numeric sampling,
  and a contradiction never claims any official document is wrong —
  every result carries that scope note.
- `math_claims.py` integrates with the other stages: claim
  `source_references` keep C3 `document_id`/`role`/`sha256`/`page`
  verbatim, and can be cross-checked against C3 document records.
  C2 findings pass through as untouched deep copies. Mathematical
  correctness and mark eligibility are reported in separate sections.
- `math_benchmark.py` is a 28-case synthetic benchmark (`MBC-01`..)
  independent of the C2 accuracy/case-governance fixtures: it runs the
  public `verify_claim` path and asserts expected outcomes plus
  review-required flags (`python -m investigation.math_benchmark`).

Stage C4 corrections for two C3 findings (narrow scope, contracts kept):

1. `pdf_evidence.py` now reports `partial_extraction` at document level
   when per-page truncation or aggregate text-budget exhaustion occurs,
   instead of silently claiming a fully `extracted` document;
   `test_only_budget_exhaustion_is_reported_incomplete` asserts the
   strengthened status.
2. `pdf_question_map.py` never attaches a continuation page that bears
   ANY question heading (matched or unmatched number): a new question
   starts there, so heuristic continuation cannot cross an unmatched
   heading boundary. Regression tests cover both the boundary and the
   unchanged positive continuation path.

Run their tests with:

```sh
python -m unittest investigation.test_safe_math investigation.test_math_verification investigation.test_math_claims investigation.test_math_benchmark investigation.test_math_security -v
```

## Live (not executed for this investigation)

Requires explicit human authorization, a separate test-only project with **no billing attached**, and rechecking the current Google free-tier pricing/access. A free-eligible model can charge a billed project. The script cannot check billing; do not use it when billing state is uncertain. Do not supply a production key. Do not place a key in a command line, environment variable, output file or chat.

After those checks, run in an interactive terminal, replacing the verification date with today's local date:

```sh
python -m investigation.diagnose --live --model gemini-3.8-flash --level A --timeout 30 --confirm-unbilled-test-project --pricing-verified-on YYYY-MM-DD
```

Type the requested attestation and enter the test-only key at the hidden prompt. Choose B for the small text marking task or C for two small inline PDFs. C accepts `--variant digital` or `--variant strokes`. The allowlist also includes `gemini-3.6-flash` and `gemini-3.5-flash-lite`; there is no all-model loop. Each command sends at most one generation, with no retries or fallback. Running another command is another request and needs to remain within the separately authorized budget. A timeout may still consume remote computation; do not repeat it automatically.

The script prints only sanitized metadata. `completed` means a complete structured response, not agreement with the expected marks; inspect the agreement flags. A valid shape is not proof that the evidence was understood. C bypasses Files API upload and does not validate production upload/caching. A/B/C are availability smoke tests, not an examination accuracy benchmark.

## Offline accuracy scoring

`accuracy_cases.json` supplies original synthetic rubrics and gold criterion awards. Do not send its gold awards or derivations to a model; use `accuracy.prompt_case`. No live multi-case evaluator is supplied. After separately authorized evaluation, normalize synthetic predictions into a JSON list:

```json
[{"id":"algebra-correct","awards":[true,true],"uncertain":false,"seconds":1.5}]
```

Use the exact IDs and criterion order in the fixture file. Missing results count as failures; `failed: true` can represent an unsuccessful attempt. Then run:

```sh
python -m investigation.accuracy synthetic_predictions.json
```

Only aggregate metrics are printed. Gold identity tests prove evaluator behavior, not AI accuracy. Review gold criteria independently and expand diagram/handwriting coverage before using this benchmark to select a grading provider. Teacher review remains mandatory.
