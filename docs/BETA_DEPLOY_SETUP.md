# Beta deployment setup packet (prepared, NOT applied)

Status: **prepared for review. Nothing in this document has been executed.**
No migration, provisioning, secret creation, Cloud Run deployment, DNS change,
paid engine call or real student data has occurred. Two gates must clear before
anything here is applied:

1. IR approval of the exact migration + rollback + configuration in this PR.
2. Explicit user authorization for the concrete infrastructure action.

The user-reported resources (Supabase project `ai-maths-marker-beta`, Free,
Frankfurt `eu-central-1`; existing Google OAuth Web client; Cloudflare Pages
project) are **reused, never recreated**. Values not supplied yet (project ref,
OAuth client ID, service URLs) are shown as placeholders, never invented.

---

## 1. What the backend needs (verified against official pricing pages)

| Item | Value | Source (read 2026-10-25) |
|---|---|---|
| Cloud Run free tier (request-based billing) | 2,000,000 requests, 180,000 vCPU-seconds, 360,000 GiB-seconds per month, **valued at Tier 1 (us-central1) pricing** | https://cloud.google.com/run/pricing |
| Cloud Run rates | $0.000024/vCPU-s, $0.0000025/GiB-s, $0.40/M requests (Tier 1 display rates) | https://cloud.google.com/run/pricing |
| Frankfurt region | `europe-west3`, **Tier 2 pricing** (higher than the Tier 1 rates above; region is fixed per service after the first deploy) | https://cloud.google.com/run/pricing |
| Cloud Run Jobs (the worker shape) | instance-based billing, separate free tier: 240,000 vCPU-seconds + 450,000 GiB-seconds/month (us-central1 basis) | https://cloud.google.com/run/pricing ("Jobs") |
| Cloud Build | 2,500 free build-minutes/month (e2-standard-2 default pool), then $0.006/min | https://cloud.google.com/build/pricing |
| Artifact Registry | 0.5 GiB-month free, then $0.000136986/GiB-hour; same-region pulls free | https://cloud.google.com/artifact-registry/pricing |
| Secret Manager | 6 free active secret versions, 10,000 free access ops/month, then $0.03/10k | https://cloud.google.com/secret-manager/pricing |
| Supabase Free quota | $0; 500 MB database (org-level fair use), 1 GB file storage, 5 GB egress, **50 MB** max upload | https://supabase.com/pricing |
| Supabase Free disk | **fixed 2 GB**, read-only mode at **95 % disk usage**, Postgres stops if the disk fills; WAL shares the disk (`max_wal_size` 1 GB) | https://supabase.com/docs/guides/platform/database-size |
| Supabase Free compute | Nano: shared CPU, up to 0.5 GB RAM, **60 direct connections**, **200 pooler clients** | https://supabase.com/pricing |
| Supabase Free durability | **no automatic backups, no PITR** (Pro feature); project **pauses after ~1 week of inactivity** | https://supabase.com/pricing |

Workload assumption used for the estimate below: **100 marking runs/month, one
1 vCPU / 1 GiB instance, ~60 s per run, 0 minimum instances**. That is
~6,000 vCPU-seconds and ~6,000 GiB-seconds - inside the free allowances, and
builds/secrets are inside their free tiers for a single small service.

Honest limits:

* the Cloud Run free tier is applied as a **spending-based discount valued at
  Tier 1 pricing**, and Frankfurt is a **Tier 2** region, so a non-zero bill is
  possible even at low volume; **no zero-cost promise is made**;
* **internet egress is not "same-region" traffic.** Cloud Run → Supabase leaves
  Google's network and is billed at Premium-tier internet egress from the first
  byte (the 1 GiB/month free egress allowance is North-America-based). Only
  Cloud Run ↔ Secret Manager ↔ Artifact Registry in `europe-west3` is free;
* Cloud Build, Artifact Registry, internet egress, Secret Manager access
  operations and Supabase overage are separate line items;
* the Gemini engine is **off by default** and would add per-token charges that
  are out of scope for this packet;
* the Supabase Free 1 GB storage cap conflicts with 100 × 25 MB PDFs per
  month, so the retention proposal in section 4 is part of this packet, not an
  afterthought.

### 1.1 Billing activation is its own gate

Creating the Cloud Run service or the Artifact Registry repository requires an
**active billing account on the Google Cloud project**. That is a separate,
explicit user authorization, not part of code review, and the Cloud Run free
tier is not a guarantee against a non-zero invoice. Charges that can exist at
beta scale, each with the assumption that produces them:

| Line item | When it charges | Beta assumption |
|---|---|---|
| Cloud Run requests (Tier 2) | above 2M requests / 180k vCPU-s / 360k GiB-s, valued at Tier 1 | 100 runs ≈ 6k vCPU-s → inside free tier |
| Cloud Run Jobs | above 240k vCPU-s / 450k GiB-s | one worker task per run ≈ 60 s → inside free tier |
| Cloud Build | above 2,500 min/month | ~5 builds × ~3 min → inside free tier |
| Artifact Registry | above 0.5 GiB-month | 1–2 images ≈ 0.2–0.4 GiB; prune old revisions |
| Internet egress to Supabase | from the first byte | ~100 × 25 MB ≈ 2.5 GB/month, **not** covered by same-region free traffic |
| Secret Manager | above 6 versions / 10k access ops | 3 secrets, 1 version each; ops scale with instance starts |
| Gemini engine | any call once `AI_MARKER_ENGINE_REAL=1` | **0 in beta**; separate cost decision |
| Supabase overage | above 500 MB DB / 1 GB storage / 5 GB egress | retention + 25 MB cap keep it inside the free quota |

Record the real figures in the Cloud Billing console after the first
deployment; do not present this table as a quote.

---

## 2. Exact environment contract

Names are taken from `runtime_config.py`, which is pinned by
`tests/test_runtime_config.py`. `Secret Manager` values are injected at deploy
time and never written to the repository, a message, or a log.

| Env var | Component | Secret | Required in production | Source |
|---|---|---|---|---|
| `AI_MARKER_RUNTIME` | runtime | no | yes (`production`) | Cloud Run plain env |
| `AI_MARKER_PG_DSN` | database | **yes** | yes | Secret Manager `marker-pg-dsn` |
| `GOOGLE_OIDC_AUDIENCE` | identity | no | yes | Cloud Run plain env (public client ID) |
| `AI_MARKER_SUPABASE_URL` | storage | no | yes | Cloud Run plain env |
| `AI_MARKER_SUPABASE_SERVICE_KEY` | storage | **yes** | yes | Secret Manager `marker-supabase-service-key` |
| `AI_MARKER_STORAGE_BUCKET` | storage | no | yes (`marker-pdfs`) | Cloud Run plain env |
| `AI_MARKER_ENGINE_REAL` | engine | no | no (`0`/unset = off) | Cloud Run plain env |
| `AI_MARKER_MARK_SCHEME` | engine | no | only when engine on | image path `/app/scheme.pdf` |
| `GEMINI_API_KEY` | engine | **yes** | only when engine on | Secret Manager `gemini-api-key` |
| `AI_MARKER_ALLOWED_ORIGINS` | web (CORS) | no | **yes** | Cloud Run plain env: exact Pages origin(s), comma separated, https only |
| `AI_MARKER_WORKER_TENANTS` | worker | no | only for the worker | Cloud Run Jobs plain env: comma-separated tenant ids the worker serves (or repeat `--tenant`); discovery stays explicit |
| `PORT` | runtime | no | injected by Cloud Run | platform |

Refused in production (any non-empty value fails startup): the development
SQLite paths `AI_MARKER_AUDIT_DEV_STORE`, `AI_MARKER_DOCUMENT_DB`,
`AI_MARKER_JOB_DB`, `AI_MARKER_INGEST_DB`, `AI_MARKER_MEMBERSHIP_DB`.

Required Secret Manager secrets (3 active versions total, inside the 6 free):

* `marker-pg-dsn` - Supabase pooler DSN, must contain `sslmode=require`.
* `marker-supabase-service-key` - service-role key, server-side only.
* `gemini-api-key` - only if/when the paid engine is authorized.

Rotation: create a new version, redeploy, then disable the old version. Never
paste a secret into a shell history; use the console or
`gcloud secrets versions add --data-file=-` with a local, git-ignored file.

---

## 3. Database: migration order, roles, RLS

Existing Supabase project `ai-maths-marker-beta` (Free, `eu-central-1`).
Data API disabled, automatic RLS enabled, **no schema applied yet**.

1. Connect as the project `postgres` role (SQL editor or `psql` with the
   database password from the dashboard - never committed).
2. **Take the pre-migration `pg_dump`** described in `schema/MIGRATIONS.md` §2,
   and run the **§2.1 backout drill** on the restored copy.
   Supabase Free has **no automatic backups and no PITR**, so the platform is
   not a backout. The empty-project shortcut applies **only** when the §2
   emptiness query is recorded and shows zero rows - it is never assumed from
   the project being new.
3. Apply `schema/postgres_v1.sql` (tables + constraints).
4. Apply `schema/postgres_v2_tenant_rls.sql` (roles + least privilege + RLS).
5. Create the runtime login role, password generated once and stored only in
   Secret Manager:

   ```sql
   CREATE ROLE marker_runtime LOGIN PASSWORD '<generated>';
   GRANT marker_app TO marker_runtime;
   ```

   The migration creates `marker_app` / `marker_worker` as **NOLOGIN** group
   roles, so no credential exists in SQL source.

6. Runtime DSN: Supabase **transaction pooler** (`...pooler.supabase.com:6543`)
   with `?sslmode=require`, user `marker_runtime.<project-ref>`.
   Migrations use the session/direct connection, not the pooler.

The ordered steps, per-step backout, verification queries and the executed
evidence are in **`schema/MIGRATIONS.md`** - that file is the runbook IR
reviews; this section is only the summary.

Known follow-up (tracked, not hidden): psycopg3 against a transaction pooler
needs prepared statements disabled (`prepare_threshold=None`);
`postgres_store.open_store()` now passes exactly that, pinned by
`tests/test_postgres_store_contract.py` (offline kwarg test + a real
PostgreSQL connection-kwargs test). The real Supabase transaction pooler is
still **unverified**: nothing has run through `pgbouncer` in transaction mode,
and this packet does not claim pooler compatibility until the deployment slice
does.

Rollback (reviewed exact inverse):
`schema/postgres_v2_tenant_rls_rollback.sql`. Disclosed asymmetry: it does not
restore Supabase `anon`/`authenticated` default grants that v2 revoked; those
tables are new to the project and the Data API stays disabled.

Isolation behaviour: every app transaction runs
`set_config('app.tenant_id', <verified tenant>, true)`. With no binding, RLS
returns zero rows (fail closed). `postgres_store` sets the binding in both of
its transaction helpers; the threat tests live in
`tests/test_postgres_rls_contract.py` and run against real PostgreSQL in CI
(bypass-role attributes, cross-tenant read/insert, unbound transaction,
transaction-local reset, DDL/TRUNCATE denial, worker DELETE denial,
**audit-trail UPDATE/DELETE denial for both runtime roles and no DELETE held by
any runtime role**, `SET ROLE` escalation, inherited-privilege check,
`SECURITY DEFINER` inventory, owner-under-FORCE vs `BYPASSRLS` probe).

**What RLS does not do:** `FORCE` binds the table owner, but a superuser or a
`BYPASSRLS` role always bypasses RLS. No claim is made otherwise: the runtime
roles are verified `NOSUPERUSER NOBYPASSRLS`, and the privileged path (operator
console, migration connection) is authorised by explicit tenant predicates in
application code. Both directions are exercised by the probe tests.

Free-plan capacity risks that belong to the operator: the disk is fixed at
**2 GB** and the database switches to **read-only at 95 % usage** (WAL shares
the disk), the database-size quota is **500 MB**, and the project **pauses
after ~1 week of inactivity**. A paused or read-only project is recovered in
the dashboard, not by this runbook; retention (section 4) is what keeps the
project inside both limits.

---

## 4. Private PDF storage (Supabase Storage)

* Bucket `marker-pdfs`, **private** (no public bucket, no signed public URLs).
* No policies for `anon` / `authenticated`; access only through the backend
  with the service-role key.
* Object key: `<tenant_id>/<owner_subject>/<sha256>.pdf` - server-derived, so
  a client cannot choose or guess another owner's key.
* Upload validation stays in `document_limits` (signature, size, page cap,
  encryption) **before** any object write. The application cap stays at its
  existing **25 MB** default (`MARKER_MAX_DOCUMENT_MIB`); the Supabase Free
  **50 MB** per-file limit is a platform ceiling, **not** a target, and it is
  never used to relax the application limit. A file above 25 MB is refused by
  the app before the bucket is touched.
* Retrieval: owner+tenant-scoped backend read, `Cache-Control: private,
  no-store`, `X-Content-Type-Options: nosniff`. No browser-visible bucket URL,
  no public object, no long-lived signed URL: any signed access is short-lived,
  issued only after the same owner+tenant check, and never logged.
* The 1 GB Free file-storage quota and the 5 GB egress quota are part of the
  retention design below, not a separate concern.
* The storage adapter itself is the **next implementation slice**; this packet
  fixes the bucket, key scheme and validation rules it must implement.

Retention/deletion proposal (for approval with the adapter):

* default retention 30 days after the job reaches `succeeded`/`failed`;
* deletion removes the object, keeps the digest/tombstone row for audit, and
  is idempotent;
* a teacher-initiated delete endpoint removes the object and records an audit
  event (no silent hard delete);
* purge runs as a bounded worker task (Supabase Free has no object lifecycle
  rules), with a dry-run mode first;
* **purge uses a separate elevated role**, `marker_purge` (schema 0004,
  `NOLOGIN` group role with `SELECT, UPDATE` on `pdf_references` only - the
  tombstone). The runtime roles hold no DELETE on any table
  (`schema/MIGRATIONS.md` §4.1);
  widening `marker_app`/`marker_worker` is explicitly rejected, and the app
  records deletions as audit events rather than hard-deleting history.

---

## 5. Cloud Run service configuration (prepared command, not run)

Service: `marker-api`, region `europe-west3` (Frankfurt), image
`europe-west3-docker.pkg.dev/<project>/marker/marker-api:<sha>`.

```bash
gcloud run deploy marker-api \
  --image europe-west3-docker.pkg.dev/<project>/marker/marker-api:<sha> \
  --region europe-west3 \
  --port 8080 \
  --cpu 1 --memory 1Gi \
  --min-instances 0 --max-instances 2 \
  --concurrency 8 --timeout 300 \
  --execution-environment gen2 \
  --service-account marker-runner@<project>.iam.gserviceaccount.com \
  --set-env-vars AI_MARKER_RUNTIME=production,GOOGLE_OIDC_AUDIENCE=<client-id>,AI_MARKER_SUPABASE_URL=https://<ref>.supabase.co,AI_MARKER_STORAGE_BUCKET=marker-pdfs,AI_MARKER_ALLOWED_ORIGINS=https://<pages-project>.pages.dev \
  --set-secrets AI_MARKER_PG_DSN=marker-pg-dsn:latest,AI_MARKER_SUPABASE_SERVICE_KEY=marker-supabase-service-key:latest
```

* Container command: `uvicorn api.server:application --host 0.0.0.0 --port $PORT`.
  `api/server.py` calls `runtime_config.require()` at import time, so an
  incomplete composition fails the revision instead of serving fail-closed
  503s. `fastapi`/`uvicorn` are in `requirements.txt` for this image.
* Service account `marker-runner` gets only
  `roles/secretmanager.secretAccessor` on the three secrets. No Editor, no
  Storage Admin (Supabase is external), no Cloud SQL.
* Public (`--allow-unauthenticated`) ingress is **not** part of this packet:
  the API performs its own Google ID-token verification, but exposure is a
  separate approval. Until then, use `--no-allow-unauthenticated` plus an
  operator-issued identity token for synthetic acceptance.
* CORS: `AI_MARKER_ALLOWED_ORIGINS` is an **exact-origin allowlist** parsed by
  `runtime_config.parse_allowed_origins` (`api/app.py` adds Starlette's
  `CORSMiddleware`). Production requires `https`, refuses a wildcard, a path,
  a trailing slash or `http`, allows only `GET/POST/OPTIONS` with
  `Authorization/Content-Type/Accept`, and sends no credentials. A missing or
  invalid allowlist **fails startup** - there is no wildcard fallback and no
  silent "allow nothing". Preview origins are refused unless explicitly named.
  The final Pages origin is supplied by FCM; until then the value is a
  placeholder.
* Runtime contract and browser surface are pinned by
  `tests/test_runtime_config.py`, `tests/test_api_browser_surface.py` and
  `tests/test_minimum_contract.py`.

---

## 5.1 Browser surface and the shared contract

The live-beta audit found no shared route contract and no CORS, so the browser
could not reach the API at all. This slice fixes the CM-owned half:

* **CORS is implemented** (`api/app.py`, exact-origin allowlist above) and
  exercised in-process by `tests/test_api_browser_surface.py`: allowed origin
  echoed on success *and* on the 401 envelope, another origin refused, no
  `access-control-allow-credentials`, wildcard/path/http refused at startup.
* **The runtime entrypoint exists** (`api/server.py`) and refuses an incomplete
  composition before serving.
* **The contract is written down once**:
  [`docs/MINIMUM_CONTRACT.md`](MINIMUM_CONTRACT.md) and its machine-readable
  twin [`contract/minimum_v1.json`](../contract/minimum_v1.json).
  `tests/test_minimum_contract.py` fails if the artifact and the real route
  table diverge, if a live frontend call disappears, if a client-owned call
  grows a server route, or if an error code is not a real backend code.
* **Status is `PROPOSED_AWAITING_FCM_CONFIRMATION`.** It becomes `AGREED` only
  with FCM's confirming message id. The client changes it implies (one upload
  instead of authorize+confirm, a `POST /v1/jobs` call it does not have today,
  `question_number` as a string, string question numbers, 403 ≠ expiry,
  device-local drafts) are in the contract doc, not invented here.

---

## 5.2 Cloud Run Jobs worker (prepared command, not run)

The durable worker is `worker.py`: one job per invocation (`--once`, the Jobs
task shape), executing each job through the API's own reviewed handler
(`app.state.run_marking_job`), so claim/lease/attempts/registration are the
same code as `POST /v1/jobs/{job_id}/run`. Prepared, never executed:

```bash
gcloud run jobs create marker-worker \
  --image europe-west3-docker.pkg.dev/<project>/marker/marker-api:<sha> \
  --region europe-west3 \
  --set-env-vars "AI_MARKER_RUNTIME=production,GOOGLE_OIDC_AUDIENCE=<web-client-id>,AI_MARKER_ALLOWED_ORIGINS=<exact-pages-origin>,AI_MARKER_WORKER_TENANTS=<tenant-a,tenant-b>" \
  --set-secrets "AI_MARKER_PG_DSN=marker-pg-dsn:latest,AI_MARKER_SUPABASE_SERVICE_KEY=marker-supabase-service-key:latest" \
  --max-retries 0 \
  --task-timeout 900 \
  --parallelism 1 \
  --cpu 1 --memory 512Mi \
  --command python --args worker.py,--once
```

* `--max-retries 0`: retries are the job row's `attempts`/`max_attempts`
  business, not the platform's. A failed attempt settles in the store and is
  requeued there; the platform must not stack a second retry policy on top.
* `--parallelism 1`: one job at a time per task. The lease already prevents
  double execution across tasks; parallelism is a cost knob, not a safety one.
* `AI_MARKER_WORKER_TENANTS` is required (or repeated `--tenant`): discovery
  is tenant-scoped and explicit. The worker is deliberately not given a
  privileged cross-tenant read, so it needs **no extra database grant**.
* Triggering (`gcloud run jobs execute marker-worker`) is an operator action
  scheduled outside this packet - a Cloud Scheduler trigger is NOT set up
  here and would be its own reviewed step with its own cost line.

---

## 6. Google OAuth requirements (for the reviewed sign-in flow)

* Existing Web OAuth client, reused. Authorized JavaScript origin: the
  Cloudflare Pages URL (not yet confirmed by the user).
* Authorized redirect URI: only if the selected flow needs one (popup/token
  flow does not); Classroom scopes only if Classroom import is in scope - it is
  not required for manual PDF marking.
* `GOOGLE_OIDC_AUDIENCE` = the Web client ID. Test accounts: the two synthetic
  tenants used for the two-teacher isolation step.
* Identity stays Google-verified server-side; **no Supabase Auth substitution**
  and no client-supplied tenant/owner values.

---

## 7. Order of operations at approval time

1. User supplies: Supabase project ref, OAuth client ID, Pages URL, GCP
   project ID, and authorizes migration + deploy + billing (billing activation
   is its own gate - see §1.1).
2. Create the 3 secrets (values entered directly in the console/CLI, never in
   chat).
3. Take the pre-migration `pg_dump` (`schema/MIGRATIONS.md` §2).
4. Apply v1, then v2; run the rollback drill on a scratch project first (Free
   allows 2 projects) and keep the output with the PR.
5. Create `marker_runtime`, grant `marker_app`, test a read as that role
   (unbound must return zero rows).
6. Build and push the image; deploy with 0 minimum instances.
7. Run the synthetic acceptance steps that do not need the paid engine
   (identity, membership, upload, job, results, correction, audit, restart,
   isolation), with the engine still off.
8. Only after a separate cost decision: set `AI_MARKER_ENGINE_REAL=1` for one
   synthetic document.

## 8. What this packet does NOT prove

* No live Supabase/Cloud Run/Google round trip has run; everything above is a
  prepared, reviewable configuration.
* The private storage adapter and the durable Cloud Run Jobs worker are the
  next code slice; the current job executor is the development synchronous one,
  and Cloud Run cancels in-flight requests on scale-down.
* **RLS does not constrain a superuser or a `BYPASSRLS` role.** The runtime
  roles are verified `NOBYPASSRLS`; the privileged path is authorised in
  application code, and both directions are tested.
* **Supabase Free has no backups/PITR and pauses after ~1 week of inactivity.**
  The dump in `schema/MIGRATIONS.md` §2 and a user-accepted pause policy are the
  only mitigations; neither is a platform guarantee.
* **The shared contract is proposed, not agreed.** FCM has not confirmed the
  mapping, so the client half (one-shot upload, `POST /v1/jobs`, string
  `question_number`, device-local drafts) is unproven against the real UI.
* **CORS is proven in-process only.** No Pages origin, browser preflight against
  Cloud Run, or real GIS token has been exercised.
* No zero-cost guarantee, no production-readiness claim.
