# PDF dependency licensing review — PR #9

Reviewed 12 October 2026. Recommendation: **replace PyMuPDF with pypdf + pypdfium2**,
retain the optional local inspector, and do not buy a commercial licence.
This is a practical engineering/licensing assessment, not a legal opinion about
the complete application or any past deployment.

## Fit for this project

PyMuPDF/MuPDF use AGPLv3 or a separate Artifex commercial licence. The AGPL permits
commercial use without a licence fee if its terms are met; charging teachers does
not, by itself, require buying a licence. Conversely, being a personal project,
non-commercial or hosted on a free cloud plan does not exempt a deployment from
the licence. Private local testing and making a service available to others are
different situations.

AGPL section 13 requires covered modified versions with remote interaction to
offer their corresponding source to those users. Artifex's published position
applies AGPL source-disclosure obligations to networked products using its AGPL
build. How the combined application is classified is a legal question; keeping
the library unmodified should not be assumed to avoid obligations for an app
integrating it. A public GitHub repository alone is not proof of compliance:
licensing permissions, the corresponding source and a suitable source offer matter.
This review does not change or select the application's own licence.

An AGPL-compatible open-source service is a possible no-fee route, but it adds
obligations and constrains a later closed-source product. Given the stated goal of
avoiding those obligations and paid licensing, PyMuPDF is not the best dependency
for this optional feature. Streamlit Community Cloud does not change this analysis.

Sources: [PyMuPDF licensing](https://pymupdf.io/licensing),
[AGPLv3 text, especially sections 0, 2, 5 and 13](https://opensource.org/license/agpl-3-0).

## Current alternatives and exact tested versions

| Component | Licence verified | Role and conclusion |
|---|---|---|
| pypdf 6.21.2 | BSD-3-Clause | Copies selected pages, resources, ink annotations and form fields. Cannot render a preview by itself. |
| pypdfium2 5.14.0 | Wrapper: Apache-2.0 OR BSD-3-Clause; PDFium and bundled dependencies have their own notices | Fast digital-text extraction and annotated/form-aware preview rendering. Pair with pypdf for excerpts. |
| Pillow 13.0.0 | MIT-CMU, with bundled component notices | Lossless PNG preview encoding. Explicit runtime dependency, already used by Streamlit. |
| ReportLab 5.0.1 | BSD | Test-only synthetic fixture generation; not added to deployment requirements. |

Checked upstream licence descriptions and the installed distributions' metadata
and licence directories. The PDFium build's licence file includes BSD-style and
Apache-2.0 text; it is **not sufficient to label the entire native wheel simply
BSD or to discard its third-party notices**. The default wheels used here are the
ordinary non-V8/non-XFA builds. This review concerns these packages/builds, not
every optional build or dependency in the entire application.

Sources: [pypdf licence](https://raw.githubusercontent.com/py-pdf/pypdf/main/LICENSE),
[pypdfium2 licensing](https://github.com/pypdfium2-team/pypdfium2#licensing),
[PDFium licence and dependency notices](https://github.com/pypdfium2-team/pypdfium2/tree/main/BUILD_LICENSES),
[Pillow licence](https://github.com/python-pillow/Pillow/blob/main/LICENSE),
[ReportLab package/licence](https://pypi.org/project/reportlab/5.0.1/).

These licences do not impose AGPL-style network source disclosure or require
commercial licence payments for this use. They still carry notice, disclaimer,
attribution and non-endorsement requirements; applicable Apache terms also remain.
Keep each wheel's licence/notice material when deploying or redistributing it.
For a future container, executable or bundled package, include the installed
packages' full licence directories, especially PDFium's platform-specific
`BUILD_LICENSES` and Pillow's bundled-code notices. Do not replace them with this
summary. Standard pip installations retain these files. No licence key is needed.

## Functionality preserved

The inspector still extracts bounded digital text, previews the first chosen
original page and downloads an explicitly selected 1–10-page PDF excerpt.
The excerpt is copied as PDF page objects, not rasterized or downsampled. All
original bytes sent to Gemini are unchanged. There is no OCR, no new Gemini call,
no student cache and no automatic change to marks, audits or teacher review.

pypdfium2 alone was also tried: `import_pages` produced an excerpt whose canonical
AcroForm field tree was empty in the six-field fixture. Therefore pypdf's `append`
handles page copying while PDFium handles rendering/text. Regression tests verify
both canonical form values and the rendered appearance, not merely visible text.

Preservation checks pass for digital text, vector pen strokes, diagrams, scanned
pages, genuine ink annotations, form fields, page order, cropping and rotation.
Within the same renderer, source and excerpt pixels match exactly for the test
cases. Different renderers can differ in antialiasing; text extraction order can
also differ. Extracted text remains a convenience, never authoritative maths
transcription. Invalid/encrypted PDFs fail with a safe message; teachers can still
use the complete original PDF for marking. Unusual real-world PDFs need testing.

PDFium is not thread-safe, so the existing process-wide lock now serializes its
calls. Native documents/pages/text/bitmaps and Python writers are explicitly
closed. The inspector remains opt-in and off the Gemini marking path.

## Same-input benchmark

Windows, Python 3.12.14, 20 warm measurements per implementation. Both implementations
received identical synthetic six-page PDFs and selected original pages 3 and 1.
Baseline is PR #9 `dd9b1636984edbd0d30a6aaaa18e9050d71e2f9b` with PyMuPDF 1.26.7.

| Complete local inspection | PyMuPDF baseline | Replacement | Change |
|---|---:|---:|---:|
| Digital text, strokes and ink | 30.330 ms | 24.063 ms | 21% faster |
| Image-only scan | 43.008 ms | 33.184 ms | 23% faster |
| Form fields and ink | 31.792 ms | 28.654 ms | 10% faster |

PNG previews use lossless low-compression encoding, trading roughly 22–33 KB
baseline previews for 51–63 KB previews in these cases. Excerpt size stays similar
for digital/scanned cases; forms increase from 5.6 to 6.6 KB. No image quality is
discarded. These measurements include extraction, copying and preview encoding;
they do not measure browser transfer, Gemini speed or production Cloud performance.

Raw results: `tests/pdf_replacement_benchmark_results.json`. Reproduce the current
implementation with `python tests/benchmark_pdf_inspection.py`. For historical
comparison only, obtain `local_pdf.py` from the baseline commit into a temporary
file and supply `--baseline-module PATH` in an isolated environment containing
PyMuPDF 1.26.7. The default benchmark, app, fixtures, CI and requirements no longer
import or install PyMuPDF. No baseline library is downloaded by the benchmark.

All validation remains offline. No merge, deployment, paid licence, API purchase,
live Gemini request, real student data or grade publication is authorized here.

Local validation passed: **154 regression tests, 8 Streamlit interaction tests,
4 intercepted-SDK tests**, syntax checks, both existing performance benchmarks
and the new same-input inspector benchmark. The original four PDF regression
tests also passed against the replacement before removing their PyMuPDF fixture
generator. The replacement adds form-tree/appearance, rotation, crop and bounded
preview coverage. The final test run used no PyMuPDF dependency path.
