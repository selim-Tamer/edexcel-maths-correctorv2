# AI Maths Marker — Agent instructions

## Mission
Maintain a trustworthy, free-tier-conscious Streamlit maths marker. Work autonomously on
coding, tests, debugging and pull requests. Never silently alter student grades to make
a test pass. Teacher verification remains necessary for ambiguous handwriting.

## Every change
1. Read `app5_1_automated.py` and the relevant test cases.
2. Reproduce the issue with a test; prefer a small regression fixture.
3. Run `python -m py_compile app5_1_automated.py`.
4. Run `python -m unittest discover -s tests -p 'test_*.py' -v`.
5. Keep changes focused and preserve manual uploads and Google Classroom paths.
6. Propose fixes via pull requests; summarize root cause, evidence, tests and risks.
7. Do not merge automatically. Teacher/user approval is required before production deployment.

## Critical marking invariants
- Do not invent maximum marks, transcribed handwriting, official answers or available Gemini models.
- Never grant marks based solely on a correct model solution.
- Flag disputed results for teacher review; never silently rewrite totals.
- Regressions to cover: Q77 conflicting list with full marks; Q25 incorrect arithmetic;
  Q21 intermediary vs final fractions; Q47 scientific notation; labels Q1/Sum of 5;
  Q6 open-ended interval explanations; Q49 self-corrections and contradictory totals.
- Prefer offline deterministic Python tests; do not require tokens/credits for CI.
- Actual accuracy claims require verified student scans, mark schemes and teacher-approved scores.
- Never commit student files, OAuth credentials, API keys or confidential Classroom data.

## Optional integration tests
Use mocked Gemini and mocked Classroom API first. Live Gemini/Classroom end-to-end tests
must be explicitly enabled by an authorized maintainer, use test-only accounts and
non-sensitive fixtures, obey quota limits, and must never write grades automatically.

## GitHub workflow
`.github/workflows/marking-tests.yml` runs offline checks on each PR/push and daily.
On failures on main or scheduled runs it opens/updates a GitHub issue. Codex may take
that issue, implement a regression fix in a branch, and open a PR for review.
GitHub Actions does not automatically invoke Codex; repository/app connection and
agent permissions have to be configured separately in Codex.
