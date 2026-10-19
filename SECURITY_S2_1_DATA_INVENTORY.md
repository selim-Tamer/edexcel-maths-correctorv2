# Stage S2.1 — Sensitive session-state ownership inventory

Recorded before implementation, from the code at base `ce20eed`
(`origin/main`, PR #26 merged). Every entry is in-memory Streamlit session
state; nothing here is written to disk or a database.

| State key | Created by | Sensitive? | Owner-bound at creation? | Owner checked on read? | Survives rerun | Survives logout / account switch | Cross-teacher risk before S2.1 |
|---|---|---|---|---|---|---|---|
| `last_batch_results` | Manual marking run (`app5_1_automated.py` ~3867) | **Yes** — student names, awarded marks, per-question reports, override data | Rows carry `report_owner` via `new_review_row()` | **No** — rendered for whoever reruns the session (~3916) | Yes | **Yes** | **F01/F02: Teacher B (or an anonymous session) sees Teacher A's current results, summaries and downloads** |
| `last_manual_context` | Manual marking run (~3868) | Low — mark-scheme digest | No | No | Yes | Yes | Low (digest only), cleared with the batch |
| `last_mark_scheme_name` | Manual marking run (~3869) | Low — filename | No | No | Yes | Yes | Low, cleared with the batch |
| `previous_result_snapshots` | `preserve_result_snapshot()` (~586) | **Yes** — full historical reports | Yes — only rows with `report_owner == owner` are stored | Yes — filtered by `report_owner` (~626) | Yes | **Yes** (store persists; reads are filtered) | Low after filtering; store itself is not cleared on logout |
| `teacher_overrides` / `teacher_override_reasons` | `save_teacher_override()` (~751) | **Yes** — teacher mark changes and reasons | Keyed by `review_key` (random UUID), not by owner | Implicitly, via `review_key` uniqueness | Yes | **Yes** | **Medium: Teacher B can apply overrides to Teacher A's still-rendered rows (F01 override path); store never cleared** |
| `classroom_grade_approvals` | Approval checkbox on_change (~3658) | Review/approval state | Keyed by `review_key` | Implicit | Yes | **Yes** | Medium — approval flags persist across accounts |
| `classroom_approval_checkbox_{review_key}` widgets | Streamlit widgets (~3653) | Approval state | Keyed by `review_key` | n/a | Yes | **Yes** | Low — stale widget values can only re-attach to the same `review_key` |
| `override_q_/override_mark_/override_reason_/apply_override_` widgets | Override controls (~709–734) | Input state | Keyed by `review_key` | n/a | Yes | **Yes** | Low, same `review_key` argument |
| `classroom_jobs` | `classroom_job_state()` (~2388) | **Yes** — processed rows with student data, revision fingerprints | Context key embeds `st.user.sub` (~3479) | Implicitly, via context key | Yes | **Yes** (dict persists across logout) | Low–medium; entries of a previous account remain in memory |
| `document_session` | `document_session()` (~105) | Uploaded/reference PDF caches, parsed results | Scope digest includes `sub` (or `"manual"`) | Scope compared on read | Yes | Cleared by the logout button only (~3387); scope change also clears | Low |
| `last_document_timings` | `show_document_progress()` (~156) | No — content-free timings | No | n/a | Yes | Yes | None |
| `manual_report_owner` | `result_owner()` (~583) | Opaque anonymous-session token | n/a | n/a | Yes | **Yes** — reused by the anonymous session after logout | None direct; reset for hygiene |
| `snapshot_history_trimmed` | History trim flag (~615) | No | n/a | n/a | Yes | Yes | None |
| `classroom_course_id` / `classroom_assignment_id` widgets | Course/assignment pickers (~3418–3448) | Course/assignment IDs (context, not student data) | No | n/a | Yes | Yes | Low |

## Verified root causes

- **F01** — `last_batch_results` is rendered unconditionally at ~3916
  (`if "last_batch_results" in st.session_state:`). The rows carry
  `report_owner`, but nothing verifies it before rendering the class summary,
  individual reports, CSV/Markdown exports, or before
  `prepare_review_row()` applies overrides. Teacher B signing into the same
  browser session after Teacher A logs out sees Teacher A's student records
  and can apply overrides to them.
- **F02** — the logout handler (~3386–3389) clears only `document_session`.
  `last_batch_results`, `previous_result_snapshots`, `teacher_overrides`,
  `teacher_override_reasons`, `classroom_grade_approvals`, `classroom_jobs`
  and override/approval widget states all survive logout and account
  switches. There is also no reconciliation when authentication expires or
  the Google subject changes without an explicit logout.
- **F03** — `redact_secret_values()` strips only configured Gemini keys.
  Classroom error paths redact the Gemini key but never the OAuth access
  token; `Could not load Google Classroom data: {classroom_load_error}`
  (~3672) renders a completely raw exception; per-student marking failures
  store and render `str(exception)` (~3847, ~3853, ~4027), which can echo
  student content; the saved-key removal error renders a raw `OSError`
  (~3327) that can include local paths.
- **F04** — `render_result_snapshots()` (~634) still states that "Google
  authentication is not required for manual marking", contradicting the S2
  policy that every marking run requires an authenticated Google session.

## Error surfaces audited (Phase 6 input)

| Location | Current behaviour | Risk |
|---|---|---|
| ~1818 `test_api_key` | Redacts Gemini key; message not shown raw (caller shows a generic error) | Acceptable |
| ~3327 saved-key removal | Raw `OSError` text | Local path disclosure; replace with generic message |
| ~3559 Classroom marking error | Redacts Gemini key only | OAuth access token not redacted; unbounded text |
| ~3672 Classroom load error | Fully raw exception | Credentials/student references possible |
| ~3847/3853/4027 per-student failures | Raw `str(exception)` stored and rendered | Can echo student content; replace with content-free category |
| ~3879–3905 manual batch failure | Redacts Gemini key; unbounded; shown twice | Bound length and add OAuth token redaction |
