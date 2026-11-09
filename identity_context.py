"""Explicit verified-identity mapping for result persistence (BR-02).

WHY THIS MODULE EXISTS
----------------------
The API carries three *different* identity representations and they must never
be conflated:

* the **verified Google subject** - the raw ``sub`` claim, obtained only after
  ``auth_oidc.OidcVerifier`` accepted the token;
* the **membership subject** - the same raw value, used by
  ``tenant_membership.subject`` and therefore by every membership foreign key
  (``results.owner_subject``, ``pdf_references.owner_subject``);
* the **canonical resource owner** - ``google:<verified subject>``, the value
  jobs, assignments and audit ``actor`` attribution already use.

A fourth value, the **tenant**, comes only from the verified subject's single
active membership. Nothing here reads a request body.

RULES (pinned)
--------------
* The canonical owner is formed by prefixing the verified subject. A subject
  that already begins with ``google:`` becomes ``google:google:...`` - the
  prefix is never stripped, because stripping a client-supplied prefix would
  let a client choose its owner.
* ``from_actor`` accepts the tuple ``api.app.actor`` already returns and
  refuses any owner that is not exactly ``google:`` + subject.
* ``from_trusted_owner`` is for the worker path: the owner is read from a
  persisted marking job (trusted storage, not a client body) and must match
  exactly one verified membership row for the tenant. It still never strips a
  prefix from an untrusted value.
* Membership subject and canonical owner are both recorded by the result
  context table, so a later reader never has to guess one from the other.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CANONICAL_OWNER_PREFIX = "google:"
IDENTITY_LIMIT = 512


class IdentityContextError(Exception):
    """Fail-closed identity mapping error with a static detail."""

    def __init__(self, detail: str):
        self.detail = str(detail)
        super().__init__(self.detail)


def _identity(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IdentityContextError(f"{name}_invalid")
    value = value.strip()
    if len(value) > IDENTITY_LIMIT:
        raise IdentityContextError(f"{name}_too_long")
    if any(ord(character) < 32 for character in value):
        raise IdentityContextError(f"{name}_invalid")
    return value


def _exact_identity(value: Any, name: str) -> str:
    """Like ``_identity`` but refuses any surrounding whitespace.

    Stored/derived identity values (canonical owner, membership subject) are
    compared exactly; silently trimming one could hide a mismatch. The raw
    verified subject may be normalised by ``_identity`` because it is formed
    from the token claim, not from storage or a request body.
    """
    if not isinstance(value, str) or value != value.strip():
        raise IdentityContextError(f"{name}_invalid")
    return _identity(value, name)


def canonical_owner(verified_subject: Any) -> str:
    """``google:`` + the verified subject; never a caller-chosen alias."""
    return CANONICAL_OWNER_PREFIX + _identity(verified_subject, "subject")


@dataclass(frozen=True)
class IdentityContext:
    """The four identity values one result operation is scoped by."""

    verified_subject: str
    membership_subject: str
    owner_subject: str
    tenant_id: str

    def __post_init__(self) -> None:
        subject = _identity(self.verified_subject, "subject")
        membership = _identity(self.membership_subject, "membership_subject")
        owner = _identity(self.owner_subject, "owner_subject")
        tenant = _identity(self.tenant_id, "tenant_id")
        if membership != subject:
            raise IdentityContextError("membership_subject_mismatch")
        if owner != CANONICAL_OWNER_PREFIX + subject:
            raise IdentityContextError("owner_subject_mismatch")
        object.__setattr__(self, "verified_subject", subject)
        object.__setattr__(self, "membership_subject", membership)
        object.__setattr__(self, "owner_subject", owner)
        object.__setattr__(self, "tenant_id", tenant)

    @property
    def canonical_owner(self) -> str:
        return self.owner_subject


def from_verified_subject(verified_subject: Any, tenant_id: Any) -> IdentityContext:
    """Map a VERIFIED raw subject + resolved tenant to all representations."""
    subject = _identity(verified_subject, "subject")
    tenant = _identity(tenant_id, "tenant_id")
    return IdentityContext(verified_subject=subject,
                           membership_subject=subject,
                           owner_subject=CANONICAL_OWNER_PREFIX + subject,
                           tenant_id=tenant)


def from_actor(verified_subject: Any, owner: Any, tenant_id: Any) -> IdentityContext:
    """Validate the tuple ``api.app.actor`` returned and map it.

    ``owner`` is server-derived there; this check is defence in depth against a
    future call site passing a different value through.
    """
    context = from_verified_subject(verified_subject, tenant_id)
    if _exact_identity(owner, "owner_subject") != context.owner_subject:
        raise IdentityContextError("owner_subject_mismatch")
    return context


def from_trusted_owner(owner: Any, tenant_id: Any,
                       membership_subject: Any) -> IdentityContext:
    """Map a TRUSTED stored canonical owner (worker/job path) + membership.

    ``owner`` comes from ``marking_jobs.owner_subject`` (server-written), and
    ``membership_subject`` from an active ``tenant_membership`` row for the
    same tenant. The equality check is exact: no prefix is stripped or added
    to an unverified value.
    """
    owner_value = _exact_identity(owner, "owner_subject")
    subject = _exact_identity(membership_subject, "membership_subject")
    tenant = _identity(tenant_id, "tenant_id")
    if owner_value != CANONICAL_OWNER_PREFIX + subject:
        raise IdentityContextError("owner_subject_mismatch")
    return IdentityContext(verified_subject=subject,
                           membership_subject=subject,
                           owner_subject=owner_value,
                           tenant_id=tenant)
