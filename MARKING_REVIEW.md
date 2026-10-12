# Evidence-based marking review

An arithmetic warning does not establish that the student made a mistake. It may describe an incorrect AI explanation, uncertain transcription, a student intermediate calculation error, or a final-answer discrepancy. Method marks, error-carried-forward rules and scheme tolerances must be checked against the original pages.

The optional second-pass audit now receives the original files (including the separate question paper when supplied), the complete marking evidence, validator warnings and independent scheme extraction. It returns one review per low-confidence question with an explicit classification, source/page evidence, proposed transcript and solution, and a complete criterion-by-criterion mark proposal. These citations are model claims for a teacher to inspect, not authenticated proof.

The application checks that each proposal has all evidence fields, unique criterion identifiers, valid M/A/B point codes, and matching proposed totals/maximum marks. Missing, duplicate, malformed and failed reviews stay unresolved. Unfamiliar mark-code formats remain unresolved rather than being guessed. No extra repair calls or parallel model requests are added; configured model fallback remains bounded.

**Audits no longer silently change awarded totals.** They preserve the original result and individual mark decisions. The teacher compares the proposal with the original pages and uses the existing override control if needed. AI audit completion never certifies a score, even when it proposes no change. Warnings and proposals survive normalization and appear in the per-question UI, Markdown and CSV exports. The report displays review status before its numeric score. Existing Classroom approval controls remain required.

The arithmetic parser no longer backtracks into part of a decimal before punctuation. Final-answer discrepancies are categorized as requiring an original-source check, rather than automatically attributed to transcription. Neutral wording such as “student wrote” alone is no longer treated as a self-correction.

## Validation

`tests/test_evidence_review.py` uses minimal numerical reproductions and synthetic marking evidence, without private PDFs or reports. It distinguishes intermediate/transcription/feedback errors from final-answer discrepancies, keeps justified method marks in review proposals, checks incomplete/failed audits and prevents scalar-only score changes. Streamlit interaction coverage verifies that an unresolved full score remains visibly unverified and unapproved.

The existing offline suite covers PDF reuse/expiry/isolation, Classroom processing and teacher overrides. No live Gemini or Classroom calls are required. Run the full suites and controlled benchmark with:

```
python -m py_compile app5_1_automated.py document_pipeline.py
python -m unittest discover -s tests -p 'test_*.py' -v
python -m unittest discover -s tests/ui -p 'test_*.py' -v
python tests/benchmark_document_pipeline.py
```

These tests establish application behavior under mocked responses, not real-world model marking accuracy. Generated narrative can still be wrong. Existing cached reports are not retroactively repaired; version 3.3.14 invalidates versioned result-cache identities for subsequent marking.

## Decimal approximation checks

The arithmetic validator treats `...` and `…` after a decimal as a truncated prefix. Thus `1690 / 60 = 28.16... = 28.2` is valid, while an incorrect prefix or final rounded value still produces a warning. Explicit recurring notation such as `0.(3)` and `0.1(6)` is evaluated as an exact fraction. Ellipses alone do not assert a particular repeating cycle.

Finite decimal results use half a unit in the last displayed decimal place; an exact halfway case is accepted without choosing a tie-breaking convention. Integers retain exact comparison unless an approximate operator (`≈`, `≃`, `~=`) or an unfinished decimal explicitly indicates approximation. Exact fractions avoid floating-point boundary errors. The original value is retained across a numeric equality chain to detect incorrect final rounding and double rounding. If only an unfinished decimal is available, overlapping possible values are treated conservatively rather than inventing hidden digits.

These checks support short, plain numeric expressions and the notations above. They are not a general symbolic proof system or a LaTeX parser. Existing independent final-answer comparisons, marking evidence checks and teacher-review rules remain in place. The rounding regressions exercise both the arithmetic helper and the complete explanation/award validators; warnings never directly change marks.
