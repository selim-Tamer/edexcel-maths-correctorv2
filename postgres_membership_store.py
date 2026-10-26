"""PostgreSQL membership resolver: the production subject -> tenant control.

The development membership store is a local SQLite file; production needs the
verified subject's ACTIVE membership resolved from the managed database, with
the same fail-closed semantics the API already enforces:

* only a server-side row counts - there is no client-supplied tenant, no
  bypass flag, and no "first tenant wins" guess;
* inactive and absent collapse into one non-enumerating ``None``;
* MORE than one active tenant is ambiguous and refuses with a static detail
  instead of picking one;
* stale connections cannot leak a previous subject: the binding is
  transaction-local (``set_config(..., true)``), so the next transaction sees
  nothing unless it binds again.

Row-level security is a SECOND layer, not an authenticity boundary. The
``app.subject`` binding is set by this adapter from the API's verified token
subject; a compromised database credential could set the same GUC itself. The
tests assert that limit explicitly (``test_forged_subject_guc_is_a_known_limit``)
rather than claiming RLS makes a stolen credential harmless. What actually
keeps the control honest is that the credential is runtime-only, revocable,
and never handed to a client.

Differences from the development store, deliberate:

* **No DDL here.** ``schema/postgres_v5_membership_access.sql`` is applied by
  an explicit operator step (``membership_access_version = '1'``); without it
  every resolution fails closed as ``membership_unavailable``.
* **Column-level grant.** ``marker_app`` may read only the five columns the
  resolver needs; it holds no INSERT/UPDATE/DELETE on membership rows, so the
  API cannot grant itself a tenant.
"""
from __future__ import annotations

import os
from typing import Optional

from membership_store import Membership, MembershipError
from runtime_config import PG_DSN_ENV

SUBJECT_GUC = "app.subject"
SELECT_COLUMNS = "tenant_id, subject, role, active, updated_at"


def _dsn(explicit: Optional[str] = None) -> str:
    if explicit is not None:
        return explicit.strip() if isinstance(explicit, str) else ""
    return (os.environ.get(PG_DSN_ENV) or "").strip()


def configured() -> bool:
    """True when this process is composed with the production resolver."""
    return bool(_dsn())


def resolve(subject: str, *, dsn: Optional[str] = None) -> Optional[Membership]:
    """Resolve one verified subject to its single active membership.

    Returns ``None`` for absent/inactive membership; raises
    ``MembershipError`` with a static detail when the store cannot answer or
    the answer is ambiguous. Never returns a membership for a different
    subject than the one asked about.
    """
    cleaned = (subject or "").strip() if isinstance(subject, str) else ""
    if not cleaned:
        raise MembershipError("membership_inactive_or_absent")
    target = _dsn(dsn)
    if not target:
        raise MembershipError("membership_unavailable")
    try:
        import psycopg  # lazy: only the Postgres path needs the driver
    except ImportError as error:  # pragma: no cover - driver is in the manifest
        raise MembershipError("membership_unavailable") from error
    try:
        with psycopg.connect(target, prepare_threshold=None) as connection:
            with connection.transaction():
                # Transaction-local: a reused/pooled connection cannot carry
                # this subject into the next request.
                connection.execute("SELECT set_config(%s, %s, true)",
                                   (SUBJECT_GUC, cleaned))
                rows = connection.execute(
                    "SELECT " + SELECT_COLUMNS + " FROM tenant_membership"
                    " WHERE subject = %s AND active IS TRUE"
                    " ORDER BY tenant_id", (cleaned,)).fetchall()
    except Exception as error:
        raise MembershipError("membership_unavailable") from error
    if not rows:
        return None
    if len(rows) > 1:
        # Two active tenants is a provisioning conflict, not a preference.
        raise MembershipError("membership_ambiguous")
    tenant_id, row_subject, role, active, _updated = rows[0]
    if (row_subject != cleaned or active is not True
            or not isinstance(tenant_id, str) or not tenant_id.strip()):
        # Defensive: a row that does not match the binding is a store fault.
        raise MembershipError("membership_unavailable")
    return Membership(cleaned, tenant_id.strip(), role or "teacher")


def default_resolver():
    """Resolve from the process configuration, failing closed when absent."""
    return resolve


def open_store(dsn: Optional[str] = None):
    """Open and validate the v5 membership grant (never create it).

    Used by composition checks and tests: a database that has not applied
    0005 refuses here instead of failing later on every request.
    """
    target = _dsn(dsn)
    if not target:
        raise MembershipError("membership_unavailable")
    try:
        import psycopg

        connection = psycopg.connect(target, prepare_threshold=None)
    except Exception as error:
        raise MembershipError("membership_unavailable") from error
    try:
        with connection.transaction():
            row = connection.execute(
                "SELECT value FROM schema_meta"
                " WHERE key = 'membership_access_version'").fetchone()
            present = connection.execute(
                "SELECT to_regclass('public.tenant_membership')").fetchone()
    except Exception as error:
        try:
            connection.close()
        except Exception:
            pass
        raise MembershipError("membership_unavailable") from error
    if row is None or str(row[0]) != "1" or present is None or present[0] is None:
        try:
            connection.close()
        except Exception:
            pass
        raise MembershipError("membership_unavailable")
    return connection
