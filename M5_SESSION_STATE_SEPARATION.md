# M5 — Separation from `st.session_state`: what a FastAPI worker actually needs

**Status: ANALYSIS of existing behavior — no code is moved, refactored or
removed by this PR.** Milestone M5. Baseline: main `3cfef2a`.

## 1. The honest headline

Swapping `audit_store.py` for a Postgres/Supabase adapter does **not** make
the application API-ready. It makes the *storage* portable. The transactional
*orchestration* that decides **when** to commit, **what** to roll back and
**which** session values to restore is still written against
`st.session_state` inside `app5_1_automated.py`. A FastAPI worker has no
Streamlit session, so that orchestration must be extracted — not merely
re-backed.

This document records the measured boundary so the extraction is scoped
against evidence rather than assumption.

## 2. Measured coupling (whole module)

| Module | Real `st.` references | Verdict |
|---|---|---|
| `audit_store.py` | 0 | Pure — already a library. |
| `teacher_authorization.py` | 0 | Pure — takes a `state` **mapping parameter**, never `st.session_state`. |
| `question_review.py` | 0 | Pure. |
| `scheme_review.py` | 0 | Pure (3 substring hits were docstrings, e.g. "suggested"). |
| `marking_requests.py` | 0 | Pure (hits were "cost", "rejected"). |
| `document_pipeline.py` | 0 | Pure. |
| `pipeline_metrics.py` | 0 | Pure. |
| `app5_1_automated.py` | **86** `st.session_state` | The single coupling site. |

The domain layer is already Streamlit-free. That is good news and it is also
why the fix is *not* "swap the adapter": the coupling lives one level up, in
the app's orchestration.

## 3. Function-level boundary on the decision path

Measured by scanning each function body for `st.`:

### 3.1 Pure — extractable to a framework-free module as-is
| Function | `st.` refs | Note |
|---|---|---|
| `dev_audit_store()` (2835) | 0 | Opt-in store resolution; module-level cache only. |
| `durable_result_identity(row)` (2866) | 0 | Fail-closed identity; pure function of the row. |
| `audit_persistence_plan(row, actor)` (2970) | 0 | Reads revision **before** any write. |
| `commit_audit_decision(...)` (2998) | 0 | (One grep hit was the phrase "stored decision" in a comment.) The single atomic commit+event primitive. |
| `row_review_scope(row)` (2559) | 0 | |

### 3.2 Session-coupled — must be refactored for a worker
| Function | `st.` refs | What it needs from a worker instead |
|---|---|---|
| `question_review_evidence_digest(row)` (2662) | 1 | Reads `st.session_state["last_manual_context"]` (line 2672) to build the manual document digest. A worker must receive this context as an explicit argument, not read it from a session. |
| `decision_session_snapshot(...)` (2902) | 4 | Reads `teacher_overrides`, `teacher_override_reasons`, `classroom_grade_approvals`, and the approval checkbox to snapshot what a failed commit must roll back. A worker has no session to snapshot; the "current state" must be read from durable storage or passed in. |
| `apply_question_review_decision(...)` (3047) | 3 | The orchestrator: pops `question_review_notice`, gates, then writes through the session override system (`save_teacher_override`). This is the function a FastAPI handler would most want to call, and it is the one that is session-bound. |

## 4. Transactional boundary a FastAPI worker would call

The atomicity guarantee that must survive the move is already localized:

```
store, plan, err = audit_persistence_plan(row, actor)   # revision BEFORE write
store.register_result(...)                              # immutable original
store.commit_correction(expected_revision=plan[...],    # optimistic + atomic
                        idempotency_key=..., payload=..., event=...)
```

In Postgres this becomes one transaction (`SELECT … FOR UPDATE` in place of
`BEGIN IMMEDIATE`); the failure codes
(`stale_revision`, `stream_sequence_conflict`, `idempotency_key_conflict`,
`idempotency_key_payload_mismatch`) are already emitted by the adapter and are
preserved verbatim in `m5_api_contract.json`.

**What is genuinely missing for a worker, and is real work:**

1. **A session-free orchestration entrypoint.** `apply_question_review_decision`
   must be split into a pure "decide + commit" core (no `st`) and a thin
   Streamlit adapter that keeps today's UI behavior byte-for-byte.
2. **Explicit context instead of `last_manual_context`.** The manual digest
   path must take its context as a parameter.
3. **Durable "current state" instead of a session snapshot.** Rollback for a
   failed commit must read from storage, not from a per-user session dict.
4. **A real tenant/actor principal.** Today `actor` is a session-scoped owner
   string. See `M5_TENANT_IDENTITY_SCHEMA.md`.
5. **An auth boundary.** `teacher_authorization` is already pure and takes a
   `state` mapping; a worker must supply that mapping from a verified
   principal and re-verify fresh at each privileged action.

## 5. What this PR does and does not do

**Does:** publish the contract (`m5_api_contract.json`), the tenant/identity
and migration design (`M5_TENANT_IDENTITY_SCHEMA.md`), this boundary analysis,
and offline **contract checks** that pin the contract to the real source
vocabulary.

**Does not:** move or edit any of the functions above; add FastAPI (or any
dependency); create a worker; provision anything; touch the Streamlit
behavior. The extraction itself is not performed here — it exists separately
in PR #52 as `decision_core.py`, where the functions above remain
session-coupled *adapters* over the extracted core.

## 6. Consequence for ordering

Because the session-coupled orchestration is the real gap, the next
*implementation* slice was the extraction of a session-free decision core —
**not** the adapter swap. Doing the adapter swap first would leave a worker
with a portable store it still cannot drive, which is precisely the
"polishing infrastructure we must replace" risk this plan is meant to avoid.

**Status update:** that extraction now exists — PR #52 adds
`decision_core.py` (gate sequence behind explicit context injection) and
converts the three functions named in section 4 into session-coupled
adapters, so this document's coupling map still describes them accurately.
The adapter swap, the worker, and the Postgres/Supabase adapter remain
future work. This document still marks every listed endpoint
`SPEC_NOT_IMPLEMENTED`; the first running subset (`/v1/healthz`, the
decision endpoint and the events read) is delivered separately by PR #53,
which records its deviations from this contract in its PR body.
