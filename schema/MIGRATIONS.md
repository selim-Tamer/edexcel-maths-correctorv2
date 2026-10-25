# Migration + rollback runbook (prepared, NOT applied)

Status: **prepared artifact for IR review. Nothing here has been executed
against the Supabase project.** Applying any step needs (1) IR approval of the
exact commit and (2) explicit user authorization for that concrete action.

Target: project `ai-maths-marker-beta` (Free, Frankfurt `eu-central-1`).
Reuse it; do not recreate it.

---

## 0. Rules that make the rest of this safe

1. **Roll forward from the reviewed base id.** The initial state is
   `schema/postgres_v1.sql` (reviewed in PR #56). Migrations build on that id;
   none replaces or edits v1 in place.
2. **Additive only.** No step in this ledger drops a column, table or index
   that holds data. A destructive change needs its own data-preservation plan
   and a separate approval.
3. **Every step is idempotent and has a named backout.** Re-running a step is
   safe; a failed step is rolled back by the file named next to it.
4. **Migrations run on a direct/session connection, never the transaction
   pooler.** The application uses the transaction pooler; DDL does not.
5. **Back up before every migration on a project that holds real data.**
   Supabase **Free has no automatic backups and no PITR** (Pro feature), so the
   platform cannot be the backout. The `pg_dump` in step 2 is mandatory from
   the first real row onward.
6. **Free-plan hard limits are part of the design, not an afterthought:** fixed
   **2 GB disk**, read-only mode at **95 % disk usage**, Postgres stops if the
   disk fills, **500 MB** database-size quota (org-level fair use), **60**
   direct connections, **200** pooler clients, **1 GB** file storage, **50 MB**
   max upload, **5 GB** egress/month, project **pauses after ~1 week of
   inactivity**. A project pause is recovered in the dashboard, not by SQL.

---

## 1. Ledger

| Id | File | Effect | Version marker | Backout |
|---|---|---|---|---|
| 0001 | `schema/postgres_v1.sql` | Creates `schema_meta` + 5 tenant tables, constraints, indexes. Empty initial state. | `schema_meta.schema_version = '1'` | No backout file: v1 is the base. Before any real data, drop the 5 tables in dependency order (`audit_events`, `pdf_references`, `results`, `tenant_membership`, `tenants`, then `schema_meta`); after real data, a dump + approved plan is required. |
| 0002 | `schema/postgres_v2_tenant_rls.sql` | Creates NOLOGIN group roles `marker_app` / `marker_worker` (attributes **verified**, not altered), least-privilege grants, `anon`/`authenticated` revoke, RLS **ENABLE + FORCE** and `tenant_isolation` on all 5 tenant tables. | `schema_meta.rls_version = '1'` (the adapter's `schema_version` stays `'1'`: table shape unchanged) | `schema/postgres_v2_tenant_rls_rollback.sql` |

Backout asymmetry (disclosed in the rollback file): it restores the v1 shape
but cannot restore Supabase `anon` / `authenticated` default grants it revoked.
Those tables are new to this project and the Data API stays disabled; if that
ever changes, re-grant explicitly and record why.

There is **no 0003 yet**. The durable job store and the private storage adapter
are the next slice and will land as new numbered files, not edits to 0002.

---

## 2. Pre-migration backup (mandatory once the project holds real data)

Run as the project `postgres` role, from the machine that will apply the
migration, with the connection URI copied from **Dashboard → Connect** (direct
or session connection, port 5432). Never paste the password into a command
line or a chat; use `PGPASSFILE` or the interactive prompt.

```bash
# 1. Take the dump (custom format, restorable and listable).
pg_dump --format=custom --no-owner --no-privileges \
        --file "pre-0002-$(date -u +%Y%m%dT%H%M%SZ).dump" \
        --dbname "postgresql://postgres:<ref>:<password>@<direct-host>:5432/postgres?sslmode=require"

# 2. Prove the dump is readable.
pg_restore --list pre-0002-<timestamp>.dump | head

# 3. Restore drill into a scratch target (Free allows a second project), then
#    compare row counts of the 5 tables before touching the real project.
pg_restore --clean --if-exists --no-owner --no-privileges \
           --dbname "<scratch connection URI>" pre-0002-<timestamp>.dump
```

Store the dump encrypted, outside the repository, and delete it only after the
post-migration verification below passes and the agreed retention window ends.
For the **first** apply (empty project, no data) the dump is trivial but still
recorded, so the habit and the evidence exist before the first real row.

---

## 3. Ordered execution (each step: verify, then continue)

| # | Action | Verify | Backout |
|---|---|---|---|
| 1 | Confirm target: project ref, region `eu-central-1`, `schema_meta` absent or empty, Data API disabled | `select to_regclass('public.schema_meta');` → `NULL` on a fresh project | none needed |
| 2 | `pg_dump` (§2) | `pg_restore --list` succeeds | delete the dump only after §4 passes |
| 3 | Apply `postgres_v1.sql` on a direct connection | `select value from schema_meta where key='schema_version';` → `1`; `\dt public.*` shows 5 tables | drop the 5 tables in dependency order (no data yet) |
| 4 | Apply `postgres_v2_tenant_rls.sql` | §4 verification block: `rls_version='1'`, `relrowsecurity` + `relforcerowsecurity` true on all 5, 5 policies, roles present, `anon`/`authenticated` revoked | `postgres_v2_tenant_rls_rollback.sql` → then §4 shape check → re-apply 0002 |
| 5 | Create runtime logins (operator, once per environment): `CREATE ROLE marker_runtime LOGIN PASSWORD '<generated>'; GRANT marker_app TO marker_runtime;` (and, later, `marker_worker_login` → `marker_worker`) | `select current_user, session_user;` as the login shows the login, and `select rolname from pg_roles where pg_has_role(current_user, oid, 'member')` shows `marker_app` | `REVOKE marker_app FROM marker_runtime; DROP ROLE marker_runtime;` — do this **before** the 0002 backout, which refuses to drop a role with dependents |
| 6 | Smoke test as the login: one `BEGIN; select set_config('app.tenant_id','<tenant>',true); select count(*) from results; COMMIT;` and one unbound `select count(*) from results;` | bound sees that tenant only; unbound sees `0` | none (read only) |
| 7 | Record evidence: dump id, verification output, who applied what, errors | §4 | — |

The generated password for step 5 is created once, stored only in Secret
Manager, and never written to this repository, a message or a log.

---

## 4. Verification block (paste after step 4, and after any re-apply)

```sql
-- Version markers: adapter contract unchanged, hardening recorded separately.
select key, value from schema_meta order by key;        -- schema_version=1, rls_version=1

-- RLS enabled AND forced on every tenant table (5 rows, all true).
select relname, relrowsecurity, relforcerowsecurity
from pg_class
where relname in ('tenants','tenant_membership','results','audit_events','pdf_references')
order by relname;

-- Exactly one policy per table, and it is the tenant predicate.
select tablename, policyname, cmd, qual is not null as has_using
from pg_policies where schemaname = 'public' order by tablename;

-- Runtime roles exist, carry no escalation attributes, and are NOLOGIN.
select rolname, rolcanlogin, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole
from pg_roles where rolname like 'marker\_%' order by rolname;

-- Least privilege: no DDL/TRUNCATE/TRIGGER/REFERENCES on the data tables.
select table_name, privilege_type
from information_schema.role_table_grants
where grantee in ('marker_app','marker_worker')
order by table_name, grantee, privilege_type;

-- Supabase API roles hold nothing on these tables (0 rows when present).
select grantee, table_name, privilege_type
from information_schema.role_table_grants
where grantee in ('anon','authenticated') and table_schema = 'public';

-- SECURITY DEFINER inventory (must be empty for public; run on the real
-- project because Supabase's autoRLS helper / extensions may live there).
select n.nspname, p.proname
from pg_proc p join pg_namespace n on n.oid = p.pronamespace
where p.prosecdef and n.nspname not in ('pg_catalog','information_schema')
order by 1,2;
```

Expected evidence after a correct apply: 2 markers, 5 forced tables, 5
`tenant_isolation` policies, 2 NOLOGIN non-privileged roles, **exactly 12
grant rows** (`schema_meta` SELECT; `results` SELECT/INSERT/UPDATE;
`audit_events` SELECT/INSERT - each for both runtime roles), **zero DELETE
grants**, no `anon`/`authenticated` grants, and no unexpected `SECURITY DEFINER`
function.

### 4.1 Per-grant justification (every runtime privilege)

IR required every UPDATE/DELETE grant to name a real product code path. The
shipped adapter (`postgres_store.py`) is the only PostgreSQL client, and its
statements are the evidence:

| Table | Verb | `marker_app` | `marker_worker` | Product code path |
|---|---|---|---|---|
| `schema_meta` | SELECT | yes | yes | `open_store()` reads `schema_version` |
| `results` | SELECT | yes | yes | `original_record()`, revision/event reads |
| `results` | INSERT | yes | yes | register the immutable original result |
| `results` | UPDATE | yes | yes | `UPDATE results SET result_revision` (revision advancement) |
| `results` | DELETE | **no** | **no** | none shipped - deferred to the elevated purge role |
| `audit_events` | SELECT | yes | yes | `events()`, `stream_revision()` |
| `audit_events` | INSERT | yes | yes | `commit()` appends the decision event |
| `audit_events` | UPDATE | **no** | **no** | none: the audit trail is append-only, and a runtime credential must not rewrite history |
| `audit_events` | DELETE | **no** | **no** | none - deferred to the elevated purge role |
| `tenants` | any | no | no | no shipped PG statement; the membership slice adds its own grant |
| `tenant_membership` | any | no | no | no shipped PG statement; the membership slice adds its own grant |
| `pdf_references` | any | no | no | no shipped PG statement; the documents slice adds its own grant |

Consequences that are deliberate, not omissions:

* **No DELETE is granted to any runtime role.** `tests/test_postgres_rls_contract.py::test_no_runtime_role_holds_delete_anywhere`
  asserts the denial on all five tables.
* **`audit_events` cannot be updated by a runtime role.**
  `test_audit_trail_cannot_be_rewritten_by_a_runtime_role` asserts the denial
  for both `marker_app` and `marker_worker`.
* A future membership or documents slice adds SELECT/INSERT for its own tables
  in a new numbered migration, with the same evidence standard.
* **Retention/purge is not a runtime verb.** The storage/retention slice must
  create a separate elevated role and grant it only what a purge needs. Widening
  `marker_app`/`marker_worker` back to DELETE is explicitly rejected here.

---

## 5. What RLS does and does not do here

* `tenant_id = NULLIF(current_setting('app.tenant_id', true), '')` — with no
  binding the comparison is `NULL`, so a query returns **zero rows** and an
  insert is refused. Fail closed, not fail open.
* The binding is **transaction-local** (`set_config(..., true)`) and is set by
  `postgres_store` from the verified membership's tenant, never from a request
  field. A session-level binding would survive into the next pooled request;
  the test suite demonstrates that leak explicitly to justify the flag.
* **FORCE** makes the policy apply to the table owner. It does **not** bind a
  superuser or a `BYPASSRLS` role — PostgreSQL always bypasses RLS for those.
  The runtime roles are therefore verified `NOSUPERUSER NOBYPASSRLS`, and any
  privileged path (operator console, migration connection) is authorised in
  the application/tooling by explicit tenant predicates. The test suite proves
  both directions: a non-superuser owner under FORCE is bound, a `BYPASSRLS`
  role sees every tenant.
* Supabase's "AutoRLS on" and "Data API off" are **not** isolation evidence;
  the policies, grants and tests in this ledger are.

---

## 6. Executed evidence (scratch Postgres 17, not Supabase)

The CI `postgres-tests` job applies **0001 → 0002 → 0002 again (idempotency) →
threat tests → backout → v1-shape assertions → re-apply**, in an isolated
`marker_rls_ci` database inside the digest-pinned `postgres:17-alpine`
service:

* `tests/test_postgres_rls_contract.py` — owner/`BYPASSRLS` privilege probe,
  cross-tenant read/insert, unbound fail-closed, pooled-request identity leak
  demonstration, `SET ROLE` escalation, inherited-privilege check,
  `SECURITY DEFINER` inventory, DDL/TRUNCATE/worker-DELETE denial,
  **audit-trail UPDATE/DELETE denial for both runtime roles**, **no DELETE held
  by any runtime role on any table**, `schema_meta` read-only,
  rollback-then-re-apply.
* `tests/test_postgres_store_contract.py` — the adapter contract on the same
  schema.

The job log for the head under review is the executed evidence; it is linked
from the PR. This is a scratch container, **not** the Supabase project: no
Supabase-specific behaviour (pooler, extension set, Supabase roles) is proven
by it, and the runbook is what carries those checks to the real project.

---

## 7. Known gaps carried into the next slice

1. **Pooler compatibility.** `psycopg` sends prepared statements by default;
   the Supabase transaction pooler needs `prepare_threshold=None`. The adapter
   does not pass it yet, and no test claims it works through the pooler.
2. **Durable worker.** The job executor is still the synchronous development
   one; Cloud Run cancels in-flight requests on scale-down, so the production
   shape is Cloud Run Jobs reusing the existing `job_store` lease/attempt
   semantics. Not implemented in 0002.
3. **Private storage adapter.** Bucket/key/validation/retention rules are
   specified in `docs/BETA_DEPLOY_SETUP.md` §4; the adapter is not written yet.
   Its retention/purge path needs a **separate elevated role**: the runtime
   roles intentionally hold no DELETE (`§4.1`), and adding one back to
   `marker_app`/`marker_worker` is rejected.
4. **Membership and documents are still development stores.** They hold no
   PostgreSQL grant yet; each adds its own numbered migration with the same
   justification table when its code lands.
5. **No live round trip** through Supabase, Cloud Run or Google has run.
