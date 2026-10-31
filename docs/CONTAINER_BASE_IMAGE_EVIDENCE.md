# Container base image pin - evidence

Prepared by CM, 2026-10-27. Artifact: `Dockerfile` on
`feature/container-packaging` (PR #73).

## Requirement

The packaging review requires the base image to be **digest-pinned**, with
registry evidence recorded - a mutable tag alone is not reproducible. The
earlier Dockerfile said the tag was "pinned by version, not digest", which did
not meet that bar.

## Resolved pin

| Item | Value |
|---|---|
| Image | `python:3.13-slim-bookworm` |
| Registry | `registry-1.docker.io` (`library/python`) |
| Resolved object | multi-arch **image index** |
| Index digest | `sha256:7f40274ed887f9b12d9b7a4a37343ebc783390f9be6e13d9e99ba1e2147e5f26` |
| Resolved on | 2026-10-27 (UTC date of the registry query) |
| Dockerfile line | `FROM python:3.13-slim-bookworm@sha256:7f40274ed887f9b12d9b7a4a37343ebc783390f9be6e13d9e99ba1e2147e5f26` |

Per-platform manifests inside that index (same query):

| Platform | Manifest digest |
|---|---|
| `linux/amd64` | `sha256:89c04d97d1f3f0a04ea357a27d000ba4efb18536d2d68a54611f76d5c1b5fe99` |
| `linux/arm64` | `sha256:fb12d5e51ef10ac4bc3c0b669166bbc16487cd918543ca7e6e0a899e22c550ae` |

Pinning the **index** digest pins both platform manifests: a build on the
`amd64` Cloud Run target resolves `linux/amd64` from it deterministically,
while an `arm64` developer machine gets the corresponding manifest. Pinning a
single platform digest instead would break builds on the other architecture.

## How it was resolved (reproducible)

Anonymous pull token, then a manifest HEAD with index media types:

```bash
TOKEN=$(curl -s "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/python:pull" | jq -r .token)
curl -sI \
  -H "Accept: application/vnd.oci.image.index.v1+json" \
  -H "Accept: application/vnd.docker.distribution.manifest.list.v2+json" \
  -H "Authorization: Bearer $TOKEN" \
  "https://registry-1.docker.io/v2/library/python/manifests/3.13-slim-bookworm"
```

Recorded response headers at resolution time:

```
HTTP/1.1 200 OK
Content-Type: application/vnd.oci.image.index.v1+json
docker-content-digest: sha256:7f40274ed887f9b12d9b7a4a37343ebc783390f9be6e13d9e99ba1e2147e5f26
```

To rotate deliberately: re-run the commands, confirm the index digest changed,
update `Dockerfile` and this file together, then re-run the packaging tests.

## What this evidence is - and is not

* It is **registry metadata**: the digest the registry reported for the tag at
  resolution time, plus the per-platform digests read from the index.
* Docker is **not installed on this machine**, so no image was built, pulled,
  scanned, pushed or deployed. No build- or runtime-level claim is made here
  beyond the pin itself.
* The pin is enforced offline by
  `tests/test_container_packaging.py::test_base_image_is_pinned_by_tag_and_digest`
  (exactly one `FROM`, name is `python:3.13-slim-bookworm`, non-empty
  `@sha256:` with 64 hex chars, no `:latest`).
* CI remains the authoritative build evidence for the branch head.
* The image's interpreter, **CPython 3.13**, is now in the CI-validated
  Streamlit/UI matrix (IR condition: the image runtime must be tested before
  the first operator build). The matrix entry uses the same pinned
  `streamlit==1.66.1` / `google-genai` window as the other rows.
* Still not evidence: an actual `docker build` or container start. That is
  the operator/CI build job IR may require before a first real image build.

No migration, deployment, infrastructure activation or paid action was
performed.
