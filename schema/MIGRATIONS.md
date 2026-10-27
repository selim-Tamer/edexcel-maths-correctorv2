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
| 0003 | `schema/postgres_v3_jobs.sql` | Durable `marking_jobs` (the `job_store.py` port): same columns, lease/attempt semantics and idempotency key rules, RLS **ENABLE + FORCE** + `tenant_isolation`, `SELECT, INSERT, UPDATE` for both runtime roles, **no DELETE**. | `schema_meta.jobs_version = '1'` | `schema/postgres_v3_jobs_rollback.sql` (drops the table and therefore every job row - dump first) |
| 0004 | `schema/postgres_v4_documents.sql` | Document metadata access for the private storage adapter (`pdf_references` `SELECT, INSERT` for both runtime roles - **no UPDATE, no DELETE**) plus the **separate elevated role `marker_purge`** (`NOLOGIN`, `SELECT, UPDATE` on `pdf_references` only - the tombstone - and **no DELETE anywhere**, tenant-bound like everything else, never a bypass role). | `schema_meta.docs_version = '1'` | `schema/postgres_v4_documents_rollback.sql` (restores the 0002 posture; does NOT touch bucket objects or existing rows - disclosed) |
| 0005 | `schema/postgres_v5_membership_access.sql` | Production membership **resolution**: a second SELECT-only RLS policy on `tenant_membership` keyed by `app.subject`, plus column-level `SELECT` (five columns) for `marker_app`. **No INSERT/UPDATE/DELETE** on membership rows for any runtime role - the API cannot grant itself a tenant. | `schema_meta.membership_access_version = '1'` | `schema/postgres_v5_membership_access_rollback.sql` (drops the policy + grant, leaves membership rows intact) |

Backout asymmetry (disclosed in the rollback file): it restores the v1 shape
but cannot restore Supabase `anon` / `authenticated` default grants it revoked.
Those tables are new to this project and the Data API stays disabled; if that
ever changes, re-grant explicitly and record why.

**0003 is a DRAFT artifact** (`schema/postgres_v3_jobs.sql`): the file is
reviewed code, not an applied change. Applying it needs IR approval of the
exact SQL **and** separate explicit user authorization, exactly like 0001 and
0002. Its backout DROPS the job table and every job row in it, so the §2 dump
and the §2.1 drill are mandatory before it touches a project that holds data.
**0004 is likewise a DRAFT artifact** (`schema/postgres_v4_documents.sql`):
runtime access to `pdf_references` plus the separate elevated `marker_purge`
role. Its backout removes ACCESS only - it deletes neither bucket objects nor
existing metadata rows. Creating the bucket and its service key is operator
work in `docs/BETA_DEPLOY_SETUP.md`, never code.

**Numbering after 0005.** `0006` is **RESERVED** for the still-open security
fix that narrows the `UPDATE` privilege on `results`. Today `marker_app` and
`marker_worker` hold table-wide `UPDATE` (used by `UPDATE results SET
result_revision`), and the column-level narrowing to `result_revision` has NOT
been applied. Until 0006 lands, the wider grant recorded in 4.1 is the real,
open state - it is a tracked blocker, never a closed item. The assignments
slice therefore numbers its migration `0007` so it cannot collide with the
reserved `0006`.

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

**The empty-project exception is conditional, never assumed.** It may be used
only when this query proves there is nothing to lose, and its output is pasted
into the evidence record (step 7). "The project is new" is not evidence.

```sql
select 'schema_meta' as table_name, to_regclass('public.schema_meta') is not null as exists,
       coalesce((select count(*) from schema_meta), 0) as rows
union all select 'tenants', to_regclass('public.tenants') is not null, coalesce((select count(*) from tenants), 0)
union all select 'tenant_membership', to_regclass('public.tenant_membership') is not null, coalesce((select count(*) from tenant_membership), 0)
union all select 'results', to_regclass('public.results') is not null, coalesce((select count(*) from results), 0)
union all select 'audit_events', to_regclass('public.audit_events') is not null, coalesce((select count(*) from audit_events), 0)
union all select 'pdf_references', to_regclass('public.pdf_references') is not null, coalesce((select count(*) from pdf_references), 0);
```

If any row reports `exists = true` with `rows > 0`, the exception does **not**
apply: take the dump, then run the drill below before touching the project.

### 2.1 Backout drill (mandatory for every migration after 0002)

A backout that has never been executed is not a backout. Before a migration
touches a project that holds data, rehearse it on a restored copy:

1. restore the dump from §2 into a scratch database or a second Free project;
2. apply the forward file there and run the §4 verification block, capturing the output;
3. apply the backout file, then re-assert the pre-migration shape **and** compare row counts with step 1;
4. re-apply the forward file (idempotency), and only then apply it to the real project.

Record the drill's commands and outputs with the dump id in step 7. For 0002 on
an empty project the drill is still run against the scratch copy, so the
rollback path is known-good before it is ever needed.

---

## 3. Ordered execution (each step: verify, then continue)

| # | Action | Verify | Backout |
|---|---|---|---|
| 1 | Confirm target: project ref, region `eu-central-1`, Data API disabled, and **judge emptiness with the §2 query** (output recorded, never assumed) | `select to_regclass('public.schema_meta');` → `NULL` on a fresh project, and all six §2 rows either absent or `rows = 0` | none needed |
| 2 | `pg_dump` (§2) **and** the §2.1 backout drill on the restored copy | `pg_restore --list` succeeds; drill step 4 (re-apply) passes | delete the dump only after §4 passes |
| 3 | Apply `postgres_v1.sql` on a direct connection | `select value from schema_meta where key='schema_version';` → `1`; `\dt public.*` shows 5 tables | drop the 5 tables in dependency order (no data yet) |
| 4 | Apply `postgres_v2_tenant_rls.sql` | §4 verification block: `rls_version='1'`, `relrowsecurity` + `relforcerowsecurity` true on all 5, 5 policies, roles present, `anon`/`authenticated` revoked | `postgres_v2_tenant_rls_rollback.sql` → then §4 shape check → re-apply 0002 |
| 5 | Create runtime logins (operator, once per environment): `CREATE ROLE marker_runtime LOGIN PASSWORD '<generated>'; GRANT marker_app TO marker_runtime;` (and, later, `marker_worker_login` → `marker_worker`) | `select current_user, session_user;` as the login shows the login, and `select rolname from pg_roles where pg_has_role(current_user, oid, 'member')` shows `marker_app` | `REVOKE marker_app FROM marker_runtime; DROP ROLE marker_runtime;` — do this **before** the 0002 backout, which refuses to drop a role with dependents |
| 6 | Smoke test as the login: one `BEGIN; select set_config('app.tenant_id','<tenant>',true); select count(*) from results; COMMIT;` and one unbound `select count(*) from results;` | bound sees that tenant only; unbound sees `0` | none (read only) |
| 7 | Apply `postgres_v5_membership_access.sql` on the **direct/session (5432)** connection as the project owner | `schema_meta.membership_access_version = '1'`; `pg_policies` shows **6** rows incl. `membership_self_read` (SELECT) on `tenant_membership`; `information_schema.role_column_grants` shows exactly the five resolver columns for `marker_app`; `role_table_grants` for `tenant_membership` is **0 rows**; an **unbound** `select count(*) from tenant_membership` as the runtime login returns **0** | `postgres_v5_membership_access_rollback.sql` -> re-run the section 4 shape check -> re-apply 0005 |
| 8 | **First-membership seeding (operator, once per tenant/subject)** - section 4.4, **document only, not executed**: run as the project owner/superuser on the direct 5432 connection | the seeding verification in 4.4 returns exactly one `active = true` row for the subject; the runtime resolver probe returns that tenant; an unbound or other-subject probe returns nothing | deactivate the seeded row as the owner (`update ... set active = false`); no runtime role holds UPDATE/DELETE on membership |
| 9 | Record evidence: dump id, the section 2 emptiness output (or the drill transcript), section 4 verification output, the 0005 verification output, the seeding record (who/when/tenant/subject - never the token), who applied what, errors | section 4 + 4.2 + 4.4 | - |

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

-- Exactly one policy per tenant table, plus the 0005 subject-scoped SELECT
-- policy on tenant_membership (six rows total when 0005 is applied).
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
function. **With 0003 applied too, the same query returns 18 rows**: the
`marking_jobs` SELECT/INSERT/UPDATE for each runtime role, still zero DELETE.
**With 0004 as well, 22 rows**: plus `pdf_references` SELECT/INSERT for each
runtime role - and the separate `marker_purge` role (queried by grantee) holds
exactly `pdf_references` SELECT + UPDATE and nothing else. **With 0005**, the
table-grant row count is unchanged (the membership grant is column-level, so it
appears in `information_schema.role_column_grants`, not
`role_table_grants`), and `pg_policies` shows **6 rows**: the five
`tenant_isolation` policies plus `membership_self_read` (FOR SELECT) on
`tenant_membership`. **No role in the schema holds any DELETE at all**: purge
tombstones the row and removes the object, so history cannot be trimmed.

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
| `tenant_membership` | SELECT (5 columns) | yes | no | `postgres_membership_store.resolve()` reads tenant_id, subject, role, active, updated_at for the verified subject |
| `tenant_membership` | INSERT/UPDATE/DELETE | **no** | **no** | none: provisioning is an operator/admin step; the API cannot grant itself a tenant |
| `pdf_references` (0004) | SELECT | yes | yes | `find_document()` / `put_document()` prior-row check in `supabase_document_store` |
| `pdf_references` (0004) | INSERT | yes | yes | `put_document()` records digest + object key + size after a successful upload |
| `pdf_references` (0004) | UPDATE | **no** | **no** | none: the metadata row is immutable |
| `pdf_references` (0004) | DELETE | **no** | **no** | none: retention runs as the separate elevated `marker_purge` role |
| `pdf_references` (0004, `marker_purge`) | SELECT, UPDATE | elevated only | - | the retention/purge task: find the tenant's rows and tombstone them (`storage_key = ''`) after the object is deleted. Tenant-bound like everything else; **no DELETE, ever**. |
| `marking_jobs` (0003) | SELECT | yes | yes | `get_job()`, `find_authorizable_job()`, `claim_job()` |
| `marking_jobs` (0003) | INSERT | yes | yes | `create_job()` |
| `marking_jobs` (0003) | UPDATE | yes | yes | `claim_job()` lease/attempt bumps, `succeed_job()`, `settle_failure()` |
| `marking_jobs` (0003) | DELETE | **no** | **no** | none: job rows are history. Purge belongs to the elevated role. |

Consequences that are deliberate, not omissions:

* **No DELETE is granted to any runtime role.** `tests/test_postgres_rls_contract.py::test_no_runtime_role_holds_delete_anywhere`
  asserts the denial on all five tables, and
  `tests/test_postgres_job_store_contract.py` repeats it for `marking_jobs`.
* **`audit_events` cannot be updated by a runtime role.**
  `test_audit_trail_cannot_be_rewritten_by_a_runtime_role` asserts the denial
  for both `marker_app` and `marker_worker`.
* A future membership or documents slice adds SELECT/INSERT for its own tables
  in a new numbered migration, with the same evidence standard.
* **Retention/purge is not a runtime verb.** The storage/retention slice must
  create a separate elevated role and grant it only what a purge needs. Widening
  `marker_app`/`marker_worker` back to DELETE is explicitly rejected here.

---

### 4.2 0005 verification (paste after step 7, and after any re-apply)

```sql
-- Marker: 0005 applied.
select key, value from schema_meta
where key = 'membership_access_version';                       -- 1 row: '1'

-- Policies: five tenant_isolation + one membership_self_read.
select tablename, policyname, cmd
from pg_policies where schemaname = 'public'
order by tablename, policyname;                                -- 6 rows; membership_self_read / SELECT

-- Column-level grant: exactly the five resolver columns for marker_app.
select grantee, table_name, column_name, privilege_type
from information_schema.role_column_grants
where grantee = 'marker_app' and table_name = 'tenant_membership'
order by column_name;                    -- 5 rows: active, role, subject, tenant_id, updated_at

-- No table-level membership grant ...
select grantee, table_name, privilege_type
from information_schema.role_table_grants
where table_name = 'tenant_membership'
  and grantee in ('marker_app','marker_worker');                -- 0 rows

-- ... and no write verb at column level either.
select grantee, table_name, column_name, privilege_type
from information_schema.role_column_grants
where table_name = 'tenant_membership'
  and grantee in ('marker_app','marker_worker')
  and privilege_type <> 'SELECT';                               -- 0 rows

-- Unbound fail-closed: 0 rows even though a membership row exists.
begin;
select set_config('app.subject', '', true);
select count(*) from tenant_membership;                         -- 0
rollback;

-- Subject-bound: only that subject's rows.
begin;
select set_config('app.subject', '<seeded-subject>', true);
select tenant_id, subject, role, active from tenant_membership; -- that subject only
rollback;
```

Run the unbound/bound probes as the **runtime login** (a member of
`marker_app`), not as the owner: the owner is bound only because RLS is
FORCEd, while a superuser bypasses RLS entirely. The owner probe proves the
policy exists; the runtime probe proves the API path.

### 4.3 Backout ordering (0005 in context)

Roll back in **reverse apply order**, and **0005 before 0002** - always:

1. `0005` rollback drops `membership_self_read`, the five-column
   `marker_app` grant and the `membership_access_version` marker. It leaves
   membership rows intact (they are the authorization record).
2. `0004`, then `0003`, then `0002`. The `0002` rollback drops
   `marker_app` / `marker_worker`; a role with dependent privileges cannot be
   dropped, so any surviving 0003/0004/0005 grant makes it fail. The CI shape
   test applies the 0005 column-grant rollback before the v2 rollback for
   exactly this reason (commit `4bb3f7a`).
3. Rolling back 0005 alone is safe for authorization: the resolver then fails
   closed with `membership_unavailable`, and requests are refused rather than
   resolved to a tenant. Re-applying 0005 restores access without touching
   rows.
4. `0002` keeps its own backout rules (roles with dependents, Supabase default
   grants not restored - disclosed in the 0002 rollback file).

### 4.4 First-membership seeding runbook (document only - NOT executed)

There is **no supported self-service path** to create a membership yet: no
runtime role holds INSERT/UPDATE/DELETE on `tenant_membership`, and the API
has no route that grants a tenant. Seeding is a manual **operator** step.

Preconditions, all of which must hold:

* 0001-0005 applied and verified (section 4 + 4.2) on the target project;
* the credential used is the project **owner/superuser** (Supabase `postgres`)
  on a **direct or session connection, port 5432** - never the transaction
  pooler, and never a runtime login;
* the password comes from `PGPASSFILE` / the interactive prompt, never a
  command line, log or chat;
* Free-plan connection limits respected: 60 direct connections, 200 pooler
  clients. Close the session when done.

`subject` is the Google ID-token `sub` claim, not an email address. The
resolver requires **exactly one active tenant per subject**; two active rows
are refused as `membership_ambiguous` and the API never picks one.

```sql
-- Operator, project owner/superuser, direct 5432. Placeholders in <>.
-- One transaction: tenant first (FK), then the membership.
begin;

insert into tenants (tenant_id) values ('<tenant-id>')
on conflict (tenant_id) do nothing;

-- Idempotent: a re-run does not create a second row and does not silently
-- change an already-active row's role.
insert into tenant_membership (tenant_id, subject, role, active, updated_at)
values ('<tenant-id>', '<google-sub>', 'teacher', true, now())
on conflict (tenant_id, subject) do update
  set role = excluded.role,
      active = true,
      updated_at = now()
where tenant_membership.active is not true
   or tenant_membership.role is distinct from excluded.role;

-- Hard requirement: exactly one ACTIVE tenant for this subject.
select subject, count(*) filter (where active) as active_rows
from tenant_membership where subject = '<google-sub>' group by subject; -- active_rows = 1

commit;
```

Verify through the API's own path, as the runtime login, then roll back:

```sql
-- As the runtime login (member of marker_app), direct 5432.
begin;
select set_config('app.subject', '<google-sub>', true);
select tenant_id, subject, role, active
from tenant_membership;                       -- exactly the seeded row
rollback;

begin;
select set_config('app.subject', 'some-other-subject', true);
select count(*) from tenant_membership;       -- 0
rollback;

select count(*) from tenant_membership;       -- 0 (unbound)
```

Application expectation (not exercised by this document): with
`AI_MARKER_PG_DSN` set, a request carrying that subject's verified Google ID
token resolves the seeded tenant; an unseeded subject is refused `403`; a
database without 0005 is refused `503 membership_unavailable`.

Removal is **deactivation**, not deletion (owner only):

```sql
update tenant_membership set active = false, updated_at = now()
where tenant_id = '<tenant-id>' and subject = '<google-sub>';
```

If a subject must move tenants, deactivate the old row **in the same
transaction** as the new insert, or the resolver refuses the ambiguous state.

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
* **`app.subject` (0005) is a lookup key, not an authenticity proof.** The
  resolver sets it from the API's verified token subject, transaction-locally.
  A compromised database credential could set the same GUC and read that
  subject's membership; RLS narrows what a leaked *runtime* credential can
  reach, it does not make a stolen credential harmless. The membership test
  asserts that limit instead of claiming it away, and the credential itself is
  runtime-only, revocable and never given to a client.

### 5.1 In-tenant subject visibility and connection limits (disclosed)

0005 narrows **what a query needs** to resolve a subject; it does not make
membership rows invisible to a tenant-bound connection, and this is recorded
rather than implied away:

* **Tenant-bound reads.** The 0002 `tenant_isolation` policy already lets any
  connection that sets `app.tenant_id` read **every** `tenant_membership` row
  of that tenant - all subjects, roles and active flags. 0005 does not widen
  that; it adds the subject-keyed path alongside it. A leaked *runtime*
  credential that can set the GUC could enumerate a tenant's membership, so
  the control is credential custody + revocability + RLS in depth, not RLS
  alone.
* **Subject-keyed reads.** Any database connection able to set `app.subject`
  can test whether a chosen subject has a membership and read that one row
  (`tenant_id`, `role`, `active`, `updated_at`). `app.subject` is a GUC, so it
  is a lookup key, not proof of identity - subjects who can be guessed can be
  probed. The five-column grant keeps the payload minimal, but enumeration is
  a real, stated limitation.
* **Connection limitations.** 0005 and the seeding runbook are proven only on
  a **direct/session 5432** connection. The application uses the transaction
  pooler (`prepare_threshold=None` is pinned), but **no `pgbouncer`
  transaction-mode run has executed**, so pooler behaviour for the
  transaction-local subject binding is unverified - expected to work, not
  evidenced. Free plan: 60 direct connections / 200 pooler clients, project
  pauses after ~1 week idle.
* **Seeding and removal are operator-only.** No runtime role holds
  INSERT/UPDATE/DELETE on `tenant_membership`; the API cannot grant itself or
  anyone a tenant. Deactivation by the owner is the reviewed removal verb.

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
  **audit-trail UPDATE/DELETE denial for both runtime roles with the seeded
  audit row byte-identical afterwards**, **no DELETE held
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
   the Supabase transaction pooler needs `prepare_threshold=None`.
   `postgres_store.open_store()` now passes exactly that, pinned by
   `tests/test_postgres_store_contract.py` - offline
   (`test_open_store_disables_prepared_statements`, the kwarg itself) and on
   real PostgreSQL (`test_open_store_opens_a_pooler_safe_connection`, the
   resulting connection). **What is still unverified is the Supabase pooler
   itself**: no test has run through `pgbouncer` in transaction mode, so no
   claim is made about a pooled connection until the deployment slice runs
   one.
2. **Durable worker.** Job PERSISTENCE is drafted (`postgres_v3_jobs.sql` /
   0003 + `postgres_job_store.py`) and the worker COMPOSITION now exists:
   `worker.py` claims a job and executes it through the app's own reviewed
   handler (`app.state.run_marking_job`), so claim/engine/register/settle are
   one implementation. What remains is operational: the Cloud Run Jobs
   command in `docs/BETA_DEPLOY_SETUP.md` §5.2 is **prepared, not run**, and
   no worker has ever executed a real job. Discovery is deliberately
   tenant-scoped and explicit (`AI_MARKER_WORKER_TENANTS`), so the worker
   holds no privileged cross-tenant read.
3. **Private storage adapter.** The adapter is drafted:
   `supabase_document_store.py` keeps bytes in the private bucket and only
   digest + key + size in `pdf_references`, with schema 0004 granting the
   runtime roles `SELECT, INSERT` only. Retention runs as the **separate
   elevated `marker_purge` role** (`SELECT, UPDATE` on `pdf_references` - the
   tombstone - and nothing else); widening `marker_app`/`marker_worker` stays
   rejected, and **no role holds any DELETE at all**. What
   is NOT done: the bucket and its service key are operator work (packet §4),
   no real storage round trip has ever run, and the purge task itself is not
   yet scheduled - `delete_document()` is the reviewed verb, its execution is
   an operational step.
4. **Membership resolution is now a PostgreSQL slice, documents are not
   "development only".** `postgres_membership_store.py` + 0005 give the
   production composition a subject-scoped resolver; the API selects it
   whenever `AI_MARKER_PG_DSN` is set and fails closed on a database that has
   not applied 0005. Documents already have 0004. What remains unproven is
   operational: no migration has been applied to the real project and no
   production membership row has ever been provisioned by a supported path
   (rows are still inserted by an operator).
5. **No live round trip** through Supabase, Cloud Run or Google has run.
