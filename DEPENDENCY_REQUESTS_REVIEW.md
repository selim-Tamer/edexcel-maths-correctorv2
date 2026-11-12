# PR #36: Requests floor review

Reviewed 2026-10-22 against main
`3ab1dc25d9ef262e3e9e637dfabedfbd5b2f72c5`, including merged C6-B1 #45.
Original PR head `16000ada63309e7ca8464dc230f8d27adfb5d113` changed only
the Requests minimum. Main was merged normally into that history. This
branch does not include the Authlib policy changes from the separate #38.

## Why retain >=2.34.2,<3

This is a reviewed maintenance baseline incorporating earlier security
fixes, not a claim that 2.34.2 introduces a new security fix or is the only
possible secure minimum. Merely allowing new releases in the old range
still permits vulnerable 2.31 installations.

Relevant official advisories:

- [CVE-2024-35195 / GHSA-9wx4-h78v-vm56](https://github.com/psf/requests/security/advisories/GHSA-9wx4-h78v-vm56):
  TLS verification could remain disabled after a pooled request with
  verify=False; fixed in 2.32.0. The app does not disable TLS verification.
- [CVE-2024-47081 / GHSA-9hjg-9r4m-mvj7](https://github.com/psf/requests/security/advisories/GHSA-9hjg-9r4m-mvj7):
  netrc credential disclosure with crafted URLs; fixed in 2.32.4. This
  establishes an unsafe old floor, not confirmed exploitation in this app.
- [Official release history](https://requests.readthedocs.io/en/latest/community/updates/):
  CVE-2026-25645 was fixed in 2.33.0 and concerns direct calls to
  extract_zipped_paths, which this application does not make. 2.34.2 fixes
  header typing; earlier releases add Python 3.14 support and correct proxy
  matching, response history and URL path handling. No app API migration
  was needed. This review does not introduce custom adapters or retry policies.

[2.34.2 metadata](https://pypi.org/pypi/requests/2.34.2/json) confirms the
release exists, requires Python >=3.10 and supports 3.11/3.14. It was also
the newest release at inspection. Dependencies remain charset_normalizer
>=2,<4, idna >=2.5,<4, urllib3 >=1.26,<3 and certifi >=2023.5.7.
[Streamlit 1.66.1](https://pypi.org/pypi/streamlit/1.66.1/json) permits
requests >=2.27,<3. Freshly resolved installed versions satisfy both
production and test manifests; no transitive conflict was found.

## Original CI failure and corrections

Actions run 39705287433, job 119583779747: 500 backend tests, one assertion
failure comparing 2.34.2 with the obsolete expected 2.31 floor. The other
jobs passed on the old head. Update that reviewed expectation and assert
the existing upper bounds as well. Do not change the PDF pins, Authlib
floor or unrelated windows. CI selects exact Requests 2.34.2 for its
Python 3.11/SDK 1.40 and Python 3.14/SDK 2.30.1 entries, and latest <3 for
Python 3.14/SDK 2.31. Its subsequent non-upgrade install retains that
selection; pip check verifies the final resolved environment.

Files changed: requirements.txt, tests/test_supply_chain.py,
tests/sdk/test_requests_compatibility.py, .github/workflows/marking-tests.yml,
SECURITY_S5_SUPPLY_CHAIN.md and this report. No production Python changes.

## Offline results

Tests ran from this separate worktree using the fresh shared review venv:
Python 3.14.4, Requests 2.34.2, Streamlit 1.66.1, google-genai 2.32.0,
urllib3 2.8.0, certifi 2026.10.19, idna 3.20, charset-normalizer 3.5.2.
A separate manifest resolution check passed, as did pip check. Minimum
and latest Requests are identical today. Python 3.11 is delegated to CI.

| python -m unittest command | Passing tests |
| --- | ---: |
| discover -s tests -p 'test_*.py' -v | 593 |
| discover -s tests/ui -p 'test_*.py' -v | 40 |
| discover -s tests/sdk -p 'test_*.py' -v | 23 |
| discover -s investigation -p 'test_*.py' -v | 640 |
| Total | **1,296** |

The nine new compatibility tests execute the production HTTP/download
functions with real Requests request preparation, response parsing and
redirect handling. Only the HTTP adapter is intercepted. They cover bearer
headers/TLS verification/timeouts, legacy query translation, error-body
privacy, no timeout retry, empty/malformed JSON, cross-origin credential
stripping, repeated pagination, streamed limits and response cleanup.
netrc and environment proxy discovery are isolated from local credentials.
Existing regression tests cover download deadlines, authorization, teacher
sessions, grade-publication restrictions, PDF security and marking.

All ten tests/benchmark_*.py programs passed: automatic recovery, deadline
recovery, document pipeline, Gemini limits, local processing, marking
requests, parser deadline, PDF inspection, scheme review, service recovery.
Seven application/pipeline/authorization modules compiled successfully.
Both `python -m pip_audit --strict -r requirements.txt` and the same command
for tests/pdf-requirements.txt passed with no known vulnerabilities. No
advisories were suppressed. The audit covers today's resolved sets, not
every historically allowed transitive version or future release.

## Remaining limits and recommendation

GO WITH CONDITIONS until the updated head passes Actions. This update
does not prove live Classroom connectivity or Google login. No live
Gemini, Drive or Classroom requests were made, and no grades were published.
The existing download aggregate deadline is checked between reads and can
overrun by one bounded socket read; the dependency change neither introduces
nor fixes that documented limitation. Environment-specific proxy/CA setups
are not certified by intercepted tests.

Prefer #38 first for its directly relevant OIDC security changes, then
#36. Recheck the second PR against the resulting main and preserve both
dependency selections in CI and both policy floors. Keep both originals
open for review; no replacement PR is required. C6-B2 can continue
independently because production authorization and marking are unchanged.
