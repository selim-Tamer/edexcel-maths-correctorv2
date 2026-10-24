"""Server-side tenant membership (dev-only, fail-closed) for the beta API.

IR design conditions applied:
* membership-absent is a HARD reject - there is intentionally no owner-scope
  feature flag or bypass anywhere in this module;
* if beta needs a working path, exactly the rows seeded server-side here are
  honored - membership is never minted by any HTTP endpoint or client value;
* the store is development-only (explicit opt-in env pair, mirroring the
  audit-store discipline) and is labelled as such: SQLite here is NOT school
  storage. Consolidating this table into the approved Postgres/Supabase
  schema is later (B) work requiring separate user approval.

Schema (fresh, ``CREATE TABLE IF NOT EXISTS`` - no migration of other tables):

    tenant_membership(subject, tenant_id, role, active, updated_at)
    PRIMARY KEY (subject, tenant_id)

Resolution rules (all fail closed):
    no path configured / store cannot open -> MembershipError("membership_unavailable")
    zero rows for the subject             -> None  (caller maps to a single
                                                    non-enumerating detail)
    exactly one row with active=1         -> Membership(subject, tenant, role)
    active=0 (no active row)              -> None
    more than one active row (ambiguous)  -> MembershipError("membership_ambiguous")
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Callable, Optional

PATH_ENV = "AI_MARKER_MEMBERSHIP_DB"
ACK_ENV = "AI_MARKER_MEMBERSHIP_DEV_ONLY"


class MembershipError(Exception):
    """Fail-closed resolution failure with a static, envelope-safe detail."""

    def __init__(self, detail: str):
        self.detail = str(detail)
        super().__init__(self.detail)


class Membership:
    """One verified, active membership of the authenticated subject."""

    __slots__ = ("subject", "tenant_id", "role")

    def __init__(self, subject: str, tenant_id: str, role: str):
        self.subject = str(subject)
        self.tenant_id = str(tenant_id)
        self.role = str(role)

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"Membership(tenant_id={self.tenant_id!r}, role={self.role!r})"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenant_membership (
    subject   TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    role      TEXT NOT NULL DEFAULT 'teacher',
    active    INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (subject, tenant_id)
)
"""


def open_store(path: str) -> sqlite3.Connection:
    """Open (creating) the dev membership database."""
    if not isinstance(path, str) or not path.strip():
        raise MembershipError("membership_unavailable")
    try:
        connection = sqlite3.connect(path, timeout=10)
        connection.execute(_SCHEMA)
        connection.commit()
    except sqlite3.Error:
        raise MembershipError("membership_unavailable")
    return connection


def seed_membership(connection: sqlite3.Connection, *, subject: str,
                    tenant_id: str, role: str = "teacher",
                    active: bool = True) -> None:
    """Server-side seeding ONLY (CLI/setup, never an HTTP path)."""
    if not (isinstance(subject, str) and subject.strip()
            and isinstance(tenant_id, str) and tenant_id.strip()):
        raise MembershipError("membership_seed_invalid")
    connection.execute(
        "INSERT INTO tenant_membership (subject, tenant_id, role, active,"
        " updated_at) VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(subject, tenant_id) DO UPDATE SET"
        " role=excluded.role, active=excluded.active,"
        " updated_at=excluded.updated_at",
        (subject.strip(), tenant_id.strip(), str(role),
         1 if active else 0, f"{time.time():.0f}"))
    connection.commit()


def resolve_membership(connection: sqlite3.Connection,
                       subject: str) -> Optional[Membership]:
    """Return the subject's single ACTIVE membership or None; fail closed."""
    if not isinstance(subject, str) or not subject.strip():
        return None
    try:
        rows = connection.execute(
            "SELECT tenant_id, role, active FROM tenant_membership"
            " WHERE subject = ?", (subject.strip(),)).fetchall()
    except sqlite3.Error:
        raise MembershipError("membership_unavailable")
    active_rows = [row for row in rows if int(row[2]) == 1]
    if len(active_rows) > 1:
        # Two active tenants for one subject is ambiguous: never guess.
        raise MembershipError("membership_ambiguous")
    if not active_rows:
        return None
    tenant_id, role, _ = active_rows[0]
    if not isinstance(tenant_id, str) or not tenant_id:
        raise MembershipError("membership_ambiguous")
    return Membership(subject.strip(), tenant_id, role)


def default_resolver() -> Callable[[str], Optional[Membership]]:
    """Env-configured resolver used when the app builds its own default.

    Unconfigured or unusable storage is ``membership_unavailable`` (403,
    fail closed) - there is no fall-through mode.
    """
    cache = {"key": None, "connection": None}

    def _connection() -> sqlite3.Connection:
        path = (os.environ.get(PATH_ENV) or "").strip()
        acknowledged = (os.environ.get(ACK_ENV) or "").strip() == "1"
        if not path or not acknowledged:
            raise MembershipError("membership_unavailable")
        key = (path, acknowledged)
        if cache.get("key") == key and cache.get("connection") is not None:
            return cache["connection"]
        close_quietly(cache.get("connection"))
        connection = open_store(path)
        cache["key"] = key
        cache["connection"] = connection
        return connection

    def resolve(subject: str) -> Optional[Membership]:
        return resolve_membership(_connection(), subject)

    # Shutdown/test hook: release the cached SQLite file handle (Windows
    # locks the file while it is open).
    resolve.close = lambda: (close_quietly(cache.get("connection")),
                             cache.update({"key": None, "connection": None}))
    return resolve


def close_quietly(connection) -> None:
    if connection is not None:
        try:
            connection.close()
        except Exception:
            pass
