"""Session-local capability boundary for future server-side scheme approval.

Only trusted application code may construct an authority. Its verifier must
recheck authentication, teacher authorization and assignment access on EVERY
call, returning a server-derived principal and login-session nonce. Neither
browser values nor an OAuth subject alone establish teacher authorization.
This module does not authenticate users or expose an endpoint. C6-B must wire
the verifier and revoke the authority at logout/account switch. No persistence.
"""
from dataclasses import dataclass


class AuthorizationError(ValueError):
    """Content-free authorization failure."""


def identifier(value):
    return (type(value) is str and 0 < len(value) <= 256
            and value == value.strip()
            and not any(char.isspace() or ord(char) < 33 or ord(char) == 127 for char in value))


@dataclass(frozen=True)
class ServerIdentity:
    owner: str
    session_nonce: str
    assignment_revision: str | None = None


@dataclass(frozen=True, eq=False, repr=False)
class AuthorizationContext:
    _authority: object
    _identity: ServerIdentity
    assignment_id: str | None


class ApprovalAuthority:
    """Bounded session registry; never serialize it into a review/browser.

    ``verify(assignment_id)`` returns ServerIdentity or None. None assignment
    means an explicitly authorized manual-review scope, not a fabricated ID.
    A fresh login must use a fresh nonce even for the same account.
    """

    def __init__(self, verify):
        if not callable(verify):
            raise AuthorizationError("trusted_verifier_required")
        self._verify = verify
        self._contexts = []
        self._reviews = {}
        self._revoked = False

    def _identity_for(self, assignment_id):
        if self._revoked or (assignment_id is not None
                             and not identifier(assignment_id)):
            raise AuthorizationError("authorization_unavailable")
        try:
            identity = self._verify(assignment_id)
        except Exception:
            raise AuthorizationError("authorization_unavailable") from None
        if (type(identity) is not ServerIdentity or not identifier(identity.owner)
                or not identifier(identity.session_nonce)):
            raise AuthorizationError("authenticated_teacher_required")
        if assignment_id is not None and not identifier(identity.assignment_revision):
            raise AuthorizationError("verified_assignment_revision_required")
        if assignment_id is None and identity.assignment_revision is not None:
            raise AuthorizationError("manual_scope_revision_mismatch")
        return identity

    def authorize(self, assignment_id):
        identity = self._identity_for(assignment_id)
        for context in self._contexts:
            if context._identity == identity and context.assignment_id == assignment_id:
                return context
        if len(self._contexts) >= 100:
            raise AuthorizationError("authorization_capacity_reached")
        context = AuthorizationContext(self, identity, assignment_id)
        self._contexts.append(context)
        return context

    def validate(self, context):
        if not any(item is context for item in self._contexts):
            raise AuthorizationError("issued_authorization_required")
        if self._identity_for(context.assignment_id) != context._identity:
            raise AuthorizationError("session_or_owner_changed")
        return context._identity.owner

    def revoke(self):
        self._revoked = True
        self._contexts.clear()
        self._reviews.clear()


def validate_context(context):
    if (type(context) is not AuthorizationContext
            or type(context._authority) is not ApprovalAuthority):
        raise AuthorizationError("issued_authorization_required")
    return context._authority.validate(context)
