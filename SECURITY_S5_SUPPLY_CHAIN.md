# AI Maths Marker — Stage S5 Supply-Chain Security Report (summary)

Findings addressed: **F-10** (dependency version management), **F-11**
(GitHub Actions security), **F-12** (unused dependencies).
Base commit: `da2521809e3e23d664a6920cfca5b0f3a52f0f08` (merge of PR #29).

## Direct production dependency inventory (requirements.txt)

| Package | Proposed constraint | Purpose | Directly imported | Also required transitively by |
|---|---|---|---|---|
| streamlit | `>=1.64.0,<1.67` | UI framework | yes (production app) | — |
| Authlib | `>=1.3.2,<2` | OIDC session tokens (S2 auth) | **no** — needed by Streamlit `st.login()` | Streamlit `auth` extra (`authlib.jose` in `streamlit/auth_util.py`) |
| google-genai | `>=1.40,<3` | Gemini SDK | yes | — |
| pandas | `>=2.0,<4` | report tables | yes | Streamlit (floor `>=1.4.0`) |
| requests | `>=2.31,<3` | Classroom/Drive REST transport, bounded downloads | yes | google-genai, streamlit |
| pypdf | `==6.20.1` | untrusted-PDF metadata/limits (S3) | yes (lazy import in `document_limits.py`) | — |
| pypdfium2 | `==5.14.0` | PDF rendering (PDFium) | yes (`local_pdf.py`) | — |
| Pillow | `==12.3.0` | image decoding surface | no direct import | Streamlit (floor `>=7.1.0`), pypdfium2 image export |

Development-only: `reportlab==5.0.1` (synthetic fixtures) and `sympy==1.14.0`
(offline verification research/benchmarks) in `tests/pdf-requirements.txt`.
The deployed app performs plain Python arithmetic; sympy is never imported in
production. Constraint policy live in `requirements.txt` comments and
TESTING.md "Dependency policy and supply chain".

## Vulnerability assessment (pip-audit 2.10.1, PyPA advisory data)

Audited 2026-10-20 from this repo, `--strict`:

- `requirements.txt` resolved full tree (55 packages incl. transitives): **no
  known advisories**.
- `tests/pdf-requirements.txt`: **no known advisories**.
- Local CI-equivalent environment (87 packages; streamlit 1.64.0,
  google-genai 2.24.0, pandas 3.0.6, requests 2.34.2, sympy 1.14.0): only
  **pip itself** (tooling, not an application dependency) reports advisories —
  PYSEC-2026-196/2875/2876 (fixed 26.1/26.1.2) and PYSEC-2026-3721 (fixed
  26.2). CI now upgrades pip before installs, dropping these.
- Older manifest floors (e.g. pandas 2.0.3) cannot be built under Python 3.14
  locally; the CI 3.11 window covers the enabled resolution space and pip
  resolves latest-in-range. Recorded as a C-category detail, not a deployed
  exposure.

No finding required a package upgrade; the PDF stack pins remain unchanged
and no S3/S4 limits or Gemini policy are altered.

## GitHub Actions hardening (F-11)

- `actions/checkout` → `34e114876b0b11c390a56381ad16ebd13914f8d5` # v4.3.1
- `actions/setup-python` → `a26af69be951a213d495a4c3e4e4022e16d87065` # v5.6.0
- `actions/github-script` → `f28e40c7f34bde8b3046d885e986cb6290c5673b` # v7.1.0

SHAs verified via the GitHub REST API (git refs/refs/tags → commit object);
none invented. Every checkout sets `persist-credentials: false`. Permissions
stay least-privilege: workflow `contents: read`; `report-scheduled-failure`
keeps `issues: write, actions: read`; new `dependency-audit` job is
`contents: read`. No `pull_request_target`, no `secrets` access anywhere.
`tests/test_supply_chain.py` pins all of these guarantees.

## Automated vulnerability checking (Phase 9)

- New `dependency-audit` CI job: installs current pip, runs
  `pip-audit --strict` against `requirements.txt` and
  `tests/pdf-requirements.txt`. The job fails on any advisory or resolution
  error; a skipped or incomplete scan can never be reported green.
- New `.github/dependabot.yml`: weekly reviewable PRs for pip (root + tests)
  and github-actions ecosystems; advisory alerts come from GitHub's
  dependency graph (no paid service, no extra tokens).

## Reproduction evidence (Phase 10)

- Clean venv on Python 3.14.4 (local): `pip install -r requirements.txt`
  resolves streamlit 1.66.0, google-genai 2.31.0, pandas 3.0.6,
  requests 2.34.2, authlib 1.9.1, all PDF pins exact — inside the declared
  windows; production modules import and compile; full offline suite passes
  against the pre-existing install (streamlit 1.64.0) → the constrained range
  covers both ends with passing tests.
- CI matrix (3.11/3.14 × streamlit 1.65/1.66 × google-genai 1.40.0–2.31.0)
  unchanged; pip-audit job adds the 3.11 audit environment.
- No lockfile on purpose: Streamlit Community Cloud installs only
  `requirements.txt`, so a lockfile would be ignored in deployment; audit +
  pinned windows + Dependabot give reviewability instead.

## Verification results (2026-10-20, offline only)

- Backend `tests` suite: **400 passed** (was 383 after S4 + 16 new
  supply-chain tests + 1 added on main since the S4 report).
- Streamlit UI suite: **34 passed**. SDK suite: **14 passed**.
- Investigation suite: **493 passed**. Total: **941 passed, 0 failed**.
- 8/8 offline benchmarks pass (document pipeline, local processing, PDF
  inspection, marking requests, service recovery, automatic recovery,
  deadline recovery, gemini limits).
- `py_compile` on all production modules: OK. No live Gemini/Classroom
  requests anywhere (HTTP/Gemini boundaries fail-closed in tests).

## Remaining limitations

- Local machine runs Python 3.14 only; Python 3.11 reproducibility is
  asserted by CI, not re-executed locally.
- pip-audit reflects advisory knowledge at scan time; the CI job keeps it
  current on every PR/push/schedule.
- `streamlit[auth]` extras are not used; Authlib is declared explicitly so
  `st.login()` works on Streamlit Cloud (`httpx` arrives transitively via
  google-genai; `authlib.jose` crypto via google-auth).
