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
| Cloud Run free tier (request-based billing) | 2,000,000 requests, 180,000 vCPU-seconds, 360,000 GiB-seconds per month | https://cloud.google.com/run/pricing |
| Cloud Run rates | $0.000024/vCPU-s, $0.0000025/GiB-s, $0.40/M requests (Tier 1 display rates) | https://cloud.google.com/run/pricing |
| Frankfurt region | `europe-west3`, **Tier 2 pricing** (higher than the Tier 1 rates above) | https://cloud.google.com/run/pricing |
| Cloud Build | 2,500 free build-minutes/month (e2-standard-2 default pool), then $0.006/min | https://cloud.google.com/build/pricing |
| Secret Manager | 6 free active secret versions, 10,000 free access ops/month, then $0.03/10k | https://cloud.google.com/secret-manager/pricing |
| Supabase Free | $0; 500 MB database, 1 GB file storage, 5 GB egress, 50 MB max upload, project pauses after 1 week of inactivity | https://supabase.com/pricing |
| Artifact Registry | **not verified in this pass** - image storage is billed separately; confirm the free tier and the pushed image size before deployment | - |

Workload assumption used for the estimate below: **100 marking runs/month, one
1 vCPU / 1 GiB instance, ~60 s per run, 0 minimum instances**. That is
~6,000 vCPU-seconds and ~6,000 GiB-seconds - inside the free allowances, and
builds/secrets are inside their free tiers for a single small service.

Honest limits:

* the Cloud Run free tier is applied as a **spending-based discount at Tier 1
  pricing**, and Frankfurt is a **Tier 2** region, so a non-zero bill is
  possible even at low volume; **no zero-cost promise is made**;
* Cloud Build, Artifact Registry, egress, Secret Manager access operations and
  Supabase overage are separate line items;
* the Gemini engine is **off by default** and would add per-token charges that
  are out of scope for this packet;
* the Supabase Free 1 GB storage cap conflicts with 100 × 25 MB PDFs per
  month, so the retention proposal in section 6 is part of this packet, not an
  afterthought.

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
2. Apply `schema/postgres_v1.sql` (tables + constraints).
3. Apply `schema/postgres_v2_tenant_rls.sql` (roles + least privilege + RLS).
4. Create the runtime login role, password generated once and stored only in
   Secret Manager:

   ```sql
   CREATE ROLE marker_runtime LOGIN PASSWORD '<generated>';
   GRANT marker_app TO marker_runtime;
   ```

   The migration creates `marker_app` / `marker_worker` as **NOLOGIN** group
   roles, so no credential exists in SQL source.

5. Runtime DSN: Supabase **transaction pooler** (`...pooler.supabase.com:6543`)
   with `?sslmode=require`, user `marker_runtime.<project-ref>`.
   Migrations use the session/direct connection, not the pooler.

Known follow-up (tracked, not hidden): psycopg3 against a transaction pooler
needs prepared statements disabled (`prepare_threshold=None`);
`postgres_store.open_store()` does not pass it yet. That belongs to the next
slice (job store + worker), and this packet does not claim pooler
compatibility until a test proves it.

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
transaction-local reset, DDL/TRUNCATE denial, worker DELETE denial).

---

## 4. Private PDF storage (Supabase Storage)

* Bucket `marker-pdfs`, **private** (no public bucket, no signed public URLs).
* No policies for `anon` / `authenticated`; access only through the backend
  with the service-role key.
* Object key: `<tenant_id>/<owner_subject>/<sha256>.pdf` - server-derived, so
  a client cannot choose or guess another owner's key.
* Upload validation stays in `document_limits` (signature, size, page cap,
  encryption) **before** any object write; the Free plan's 50 MB per-file cap
  is compatible with the existing 25 MB default limit.
* Retrieval: owner+tenant-scoped backend read, `Cache-Control: private,
  no-store`, `X-Content-Type-Options: nosniff`. No browser-visible bucket URL.
* The storage adapter itself is the **next implementation slice**; this packet
  fixes the bucket, key scheme and validation rules it must implement.

Retention/deletion proposal (for approval with the adapter):

* default retention 30 days after the job reaches `succeeded`/`failed`;
* deletion removes the object, keeps the digest/tombstone row for audit, and
  is idempotent;
* a teacher-initiated delete endpoint removes the object and records an audit
  event (no silent hard delete);
* purge runs as a bounded worker task (Supabase Free has no object lifecycle
  rules), with a dry-run mode first.

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
  --set-env-vars AI_MARKER_RUNTIME=production,GOOGLE_OIDC_AUDIENCE=<client-id>,AI_MARKER_SUPABASE_URL=https://<ref>.supabase.co,AI_MARKER_STORAGE_BUCKET=marker-pdfs \
  --set-secrets AI_MARKER_PG_DSN=marker-pg-dsn:latest,AI_MARKER_SUPABASE_SERVICE_KEY=marker-supabase-service-key:latest
```

* Service account `marker-runner` gets only
  `roles/secretmanager.secretAccessor` on the three secrets. No Editor, no
  Storage Admin (Supabase is external), no Cloud SQL.
* Public (`--allow-unauthenticated`) ingress is **not** part of this packet:
  the API performs its own Google ID-token verification, but exposure is a
  separate approval. Until then, use `--no-allow-unauthenticated` plus an
  operator-issued identity token for synthetic acceptance.
* CORS: the frontend origin must be added explicitly when the Pages URL is
  known; no wildcard.
* Startup must call `runtime_config.require()` so a missing component fails the
  revision instead of serving half-configured.

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
   project ID, and authorizes migration + deploy + billing.
2. Create the 3 secrets (values entered directly in the console/CLI, never in
   chat).
3. Apply v1, then v2; run the rollback drill on a scratch branch database if
   the plan allows (Free allows 2 projects).
4. Create `marker_runtime`, grant `marker_app`, test a read as that role.
5. Build and push the image; deploy with 0 minimum instances.
6. Run the synthetic acceptance steps that do not need the paid engine
   (identity, membership, upload, job, results, correction, audit, restart,
   isolation), with the engine still off.
7. Only after a separate cost decision: set `AI_MARKER_ENGINE_REAL=1` for one
   synthetic document.

## 8. What this packet does NOT prove

* No live Supabase/Cloud Run/Google round trip has run; everything above is a
  prepared, reviewable configuration.
* The private storage adapter and the durable Postgres job store are the next
  code slice; the current job executor is the development synchronous one.
* No zero-cost guarantee, no production-readiness claim.
