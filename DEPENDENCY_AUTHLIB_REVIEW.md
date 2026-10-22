# PR #38: Authlib floor review

Reviewed 2026-10-22. Base `3ab1dc25d9ef262e3e9e637dfabedfbd5b2f72c5`
includes merged C6-B1 PR #45. Original PR head
`7ab6e541e46c2e1bbe350a54e6eae4af4299e54e` changed only the Authlib floor.
Latest main was merged normally into that history; no rebase or force push.

## Justification and compatibility

Retain `Authlib>=1.9.1,<2`. This is a security floor, not just acceptance of
whatever the resolver currently chooses. [Official release notes](https://docs.authlib.org/en/stable/upgrades/changelog.html)
record empty-HMAC-key rejection in 1.9.1 (GHSA-8m4g-j4wp-wgcg) and the
OIDC nonce-validation correction in 1.9.0. The latter is directly relevant to
an OIDC relying party. The 1.9.1 OAuth registration-server fix
(GHSA-c73q-w75p-j82v) does not establish an exploit in this application,
which does not implement that server endpoint.

[Published 1.9.1 metadata](https://pypi.org/pypi/Authlib/1.9.1/json) requires
Python >=3.10, cryptography >=45.0.1 and joserfc >=1.6.8; Python 3.11 and
3.14 are supported. 1.9.1 was also the newest release at inspection.
[Streamlit 1.66.1 metadata](https://pypi.org/pypi/streamlit/1.66.1/json)
allows Authlib >=1.3.2 in its auth extra. No conflicting constraint was found.
Streamlit uses Authlib's OAuth client and prefers joserfc for its provider
tokens; its legacy Authlib JOSE fallback still works. Authlib emits
deprecation warnings for legacy JOSE and httpx integration, not runtime
failures. Keep the <2 cap; do not suppress these warnings as a compatibility fix.

## CI failure and minimal correction

Original Actions run 39705300412, job 119583820486: 500 backend tests,
one failure in `test_frameworks_stay_within_ci_validated_window`, expecting
1.3.2 rather than 1.9.1. The other jobs passed on that older PR head.
The corrected assertion records the justified floor and now also enforces
the existing upper bounds. PDF pins and all other version windows remain
unchanged. CI explicitly installs the exact Authlib minimum on Python 3.11
and 3.14, plus latest compatible <2 on the third existing matrix entry.

Changed files: requirements.txt, tests/test_supply_chain.py,
tests/sdk/test_authlib_compatibility.py, .github/workflows/marking-tests.yml,
SECURITY_S5_SUPPLY_CHAIN.md and this report. No production Python changes.

## Independent local verification

Fresh Python 3.14.4 environment, Streamlit 1.66.1, Authlib 1.9.1,
joserfc 1.7.7, cryptography 50.0.2, google-genai 2.32.0. Production and
test manifests resolved together; `python -m pip check` passed.

| Command (python -m unittest) | Passing tests |
| --- | ---: |
| discover -s tests -p 'test_*.py' -v | 593 |
| discover -s tests/ui -p 'test_*.py' -v | 40 |
| discover -s tests/sdk -p 'test_*.py' -v | 22 |
| discover -s investigation -p 'test_*.py' -v | 640 |
| Total | **1,295** |

Eight new real-library tests exercise provider-token roundtrips, expiry,
foreign signatures, empty HMAC keys, signed OIDC identity validation,
nonce bypass rejection, expired ID tokens, and foreign issuer/audience.
Only metadata/key acquisition is mocked; signatures and claims are actually
validated. HTTP transport is blocked. Existing tests cover logout, account
switches, session revocation, teacher ownership and Classroom restrictions.

All ten `tests/benchmark_*.py` programs passed: automatic recovery, deadline
recovery, document pipeline, Gemini limits, local processing, marking requests,
parser deadline, PDF inspection, scheme review, service recovery.
These are offline performance checks, not live latency/accuracy measurements.
Compilation passed for the seven application/pipeline/authorization modules.
Both `python -m pip_audit --strict -r requirements.txt` and the same command
for `tests/pdf-requirements.txt` reported no known vulnerabilities. No
advisories were ignored. Audits describe current resolved sets, not a
guarantee about every future or historically allowed transitive version.

## Recommendation and limits

GO WITH CONDITIONS until Actions passes on the published corrected SHA,
including Python 3.11 (not installed locally). Local minimum and latest are
the same Authlib release today. No live Google login or provider request was
made; production OAuth configuration still needs the usual authorized smoke
test after an approved deployment. C6-B2 can proceed independently. This PR
does not change its authorization engine, marking, prompts, PDFs or grade
publication, and does not activate an approval interface.
